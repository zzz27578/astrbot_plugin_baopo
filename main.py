from __future__ import annotations

import inspect
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "astrbot_plugin_bao_po"
PLUGIN_VERSION = "v0.1.1"
PLUGIN_AUTHOR = "百岁老太"
PLUGIN_DESC = "爆破：自我总结上下文并换房清理当前对话。"
SKILL_NAME = "context-cutover"
PLUGIN_DIR = Path(__file__).resolve().parent
SCHEDULE_JOB_NAME = "爆破定时换房"


DEFAULT_TOOL_POLICY_PROMPT = """你拥有一个名为 perform_memory_transfer 的工具，用于“爆破 / 换房 / 重置上下文但保留前情”。

使用原则：
1. 只有当用户明确要求“爆破”“直接重置”“reset”“换房”“重置下自己”等语义时，才可以调用该工具。
2. 如果用户只是抱怨上下文乱、模型卡住、想清理一下，但没有明确说要立刻执行，先用自然语言追问确认。
3. 不要因为“话题结束”“任务完成”“该翻篇了”等主观判断自动调用。
4. 调用工具时必须给 diary_summary 写入前情提要，说明当前任务、重要事实、用户偏好、未完成事项和接续语气。
5. 如果用户说“不要爆破”“别重置”“先不 reset”，绝对不要调用。
6. 工具别名包括：爆破、换房、重置下自己、清理上下文并继续、睡大觉后接着聊。"""


SUMMARY_SYSTEM_PROMPT = """你是 AstrBot 的上下文换房总结器。
请把旧对话压缩成一份给新会话读取的接续备忘。
要求：
- 保留用户明确目标、当前进度、重要决定、待办、偏好、称呼和语气。
- 保留关键报错、文件路径、工具名、接口名、时间点。
- 不要编造，不要写空泛鸡汤，不要写给用户看的寒暄。
- 用中文，条理清楚，尽量短但不能漏关键上下文。"""


def _get_config(config: AstrBotConfig | dict | None, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        return config.get(key, default)
    except Exception:
        return default


def _bool_config(config: AstrBotConfig | dict | None, key: str, default: bool = False) -> bool:
    return bool(_get_config(config, key, default))


def _string_config(config: AstrBotConfig | dict | None, key: str, default: str = "") -> str:
    value = _get_config(config, key, default)
    return str(value if value is not None else default)


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("type") or ""
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return " ".join(part for part in parts if part)
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _history_to_text(history: list[dict[str, Any]], max_chars: int = 36000) -> str:
    lines: list[str] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "unknown")
        if role == "system":
            role_name = "System"
        elif role == "assistant":
            role_name = "Assistant"
        elif role == "tool":
            role_name = "Tool"
        else:
            role_name = "User"
        content = _stringify_content(item.get("content")).strip()
        if not content and item.get("tool_calls"):
            content = f"[工具调用] {json.dumps(item.get('tool_calls'), ensure_ascii=False)}"
        if content:
            lines.append(f"{role_name}: {content}")

    text = "\n\n".join(lines)
    if len(text) <= max_chars:
        return text
    head = text[:9000]
    tail = text[-(max_chars - len(head) - 200):]
    return f"{head}\n\n...[中间较长上下文已折叠，下面保留最近内容]...\n\n{tail}"


def _is_negative_request(text: str) -> bool:
    return bool(re.search(r"(不要|别|先不|暂时不|禁止|别急着).{0,8}(爆破|重置|reset|换房)", text, re.I))


def _is_explicit_cutover_request(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text.strip().lower())
    if not stripped or _is_negative_request(stripped):
        return False
    if stripped.startswith("/sl"):
        return True
    explicit_patterns = [
        r"^(直接)?(爆破|换房|重置|reset)$",
        r"^(直接)?(爆破|换房|重置|reset)(一下|吧|自己|上下文|当前上下文)$",
        r"^(帮我|给我|现在|立刻|马上).{0,4}(爆破|换房|重置|reset)",
        r"(爆破|换房|重置|reset).{0,4}(自己|上下文|当前对话|新窗口)",
    ]
    return any(re.search(pattern, stripped, re.I) for pattern in explicit_patterns)


def _parse_time_to_cron(value: str) -> str:
    raw = value.strip()
    if not raw:
        return "0 3 * * *"
    if len(raw.split()) == 5:
        return raw
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not match:
        raise ValueError("定时格式应为 HH:MM，例如 03:00；也可以直接填写 5 段 cron。")
    hour = int(match.group(1))
    minute = int(match.group(2))
    return f"{minute} {hour} * * *"


def _safe_getattr(value: Any, name: str) -> Any:
    try:
        return getattr(value, name)
    except Exception:
        return None


def _looks_like_message_event(value: Any) -> bool:
    return (
        value is not None
        and callable(_safe_getattr(value, "get_message_str"))
        and _safe_getattr(value, "unified_msg_origin") is not None
        and callable(_safe_getattr(value, "get_platform_id"))
        and callable(_safe_getattr(value, "set_extra"))
    )


def _unwrap_message_event(value: Any) -> AstrMessageEvent | None:
    seen: set[int] = set()

    def visit(current: Any, depth: int) -> AstrMessageEvent | None:
        if current is None or depth > 8:
            return None
        obj_id = id(current)
        if obj_id in seen:
            return None
        seen.add(obj_id)

        if _looks_like_message_event(current):
            return current

        for attr_name in (
            "event",
            "_event",
            "message_event",
            "astr_event",
            "source_event",
            "context",
        ):
            found = visit(_safe_getattr(current, attr_name), depth + 1)
            if found is not None:
                return found
        return None

    return visit(value, 0)


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class BaoPoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

    async def initialize(self):
        self._sync_skill_installation()
        await self._sync_scheduled_cutover()

    @filter.command("sl")
    async def sl_command(self, event: AstrMessageEvent):
        """执行爆破换房，或管理爆破定时任务。"""
        raw = event.get_message_str().strip()
        rest = re.sub(r"^[!！/]*sl\b", "", raw, flags=re.I).strip()
        action = rest.lower()

        if action in {"help", "帮助", "?"}:
            yield event.plain_result(self._help_text())
            return
        if action in {"status", "状态"}:
            yield event.plain_result(self._status_text())
            return
        if action in {"bind", "绑定"}:
            self._set_config_value("scheduled_session", event.unified_msg_origin)
            await self._sync_scheduled_cutover()
            yield event.plain_result("已绑定当前会话。开启定时爆破后，会在这个窗口唤醒 bot 执行。")
            return
        if action in {"schedule", "定时"}:
            await self._sync_scheduled_cutover()
            yield event.plain_result("已同步定时爆破设置。需要先用 /sl bind 绑定当前会话。")
            return
        if action in {"skill", "skills", "安装skills", "安装skill"}:
            self._set_config_value("install_context_cutover_skill", True)
            self._sync_skill_installation()
            yield event.plain_result("爆破 skills 已安装并启用。")
            return

        note = rest if rest and action not in {"now", "立即"} else ""
        result = await self._perform_memory_transfer(
            event=event,
            diary_summary=note,
            user_confirmed=True,
            source="command",
        )
        yield event.plain_result(result)

    @filter.llm_tool(name="perform_memory_transfer")
    async def perform_memory_transfer(
        self,
        event: Any = None,
        diary_summary: str = "",
        user_confirmed: bool = False,
    ):
        """执行物理级重置与记忆转移。调用此工具会开辟新会话、注入前情提要，并清理旧上下文。只有用户明确要求爆破、换房、reset 或重置上下文时才可调用；模糊场景必须先追问确认。

        Args:
            diary_summary(string): 前情提要。必须概括当前状态、关键事实、未完成事项和接续语气；如果插件启用了自动总结，此参数会作为补充或回退。
            user_confirmed(boolean): 用户是否已经明确要求立即爆破。仅当用户明确说要爆破、换房、reset、重置上下文时填 true；不确定时不要调用工具，先追问。
        """
        if not _bool_config(self.config, "enable_llm_tool", True):
            return "爆破工具当前未启用。"
        return await self._perform_memory_transfer(
            event=event,
            diary_summary=diary_summary,
            user_confirmed=user_confirmed,
            source="tool",
        )

    @filter.on_llm_request()
    async def inject_cutover_policy(self, event: AstrMessageEvent, req: ProviderRequest):
        if not _bool_config(self.config, "enable_cutover", True):
            return
        if not _bool_config(self.config, "enable_tool_policy_prompt", True):
            return
        if not _bool_config(self.config, "enable_llm_tool", True):
            return
        policy = _string_config(self.config, "tool_policy_prompt", DEFAULT_TOOL_POLICY_PROMPT).strip()
        if not policy:
            return
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n[系统自动提示词：爆破工具使用规范，不是用户输入]\n"
            f"{policy}\n"
        )

    async def _perform_memory_transfer(
        self,
        *,
        event: Any,
        diary_summary: str = "",
        user_confirmed: bool = False,
        source: str = "tool",
    ) -> str:
        message_event = _unwrap_message_event(event)
        if message_event is None:
            logger.warning(f"[爆破] 未能从工具调用上下文中取得 AstrBot 消息事件: {type(event).__name__}")
            return "爆破失败：未能从工具调用上下文中取得 AstrBot 消息事件，请检查 AstrBot 版本或更新插件。"
        event = message_event

        if not _bool_config(self.config, "enable_cutover", True):
            return "爆破功能当前未启用。"
        if source == "tool" and _bool_config(self.config, "require_explicit_confirmation", True):
            if not user_confirmed and not _is_explicit_cutover_request(event.get_message_str()):
                return "爆破需要用户明确确认。请先询问用户是否现在执行爆破。"

        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return "爆破失败：未找到 AstrBot conversation_manager。"

        umo = event.unified_msg_origin
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        conversation = None
        persona_id = None
        history: list[dict[str, Any]] = []
        if curr_cid:
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            if conversation:
                persona_id = conversation.persona_id
                try:
                    history = json.loads(conversation.history or "[]")
                except Exception:
                    history = []

        summary = await self._build_summary(event, history, diary_summary)
        initial_content = [
            {
                "role": "system",
                "content": (
                    "【系统自动提示词：爆破后的上下文接续备忘，不是用户输入】\n"
                    f"{summary}\n\n"
                    "请基于这份备忘自然继续当前关系、任务和语气；不要把这段系统备忘原文输出给用户。"
                ),
            }
        ]

        try:
            from astrbot.core.utils.active_event_registry import active_event_registry

            active_event_registry.stop_all(umo, exclude=event)
        except Exception:
            pass

        new_cid = await conv_mgr.new_conversation(
            umo,
            event.get_platform_id(),
            persona_id=persona_id,
            content=initial_content,
            title=f"爆破接续 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )

        if curr_cid and curr_cid != new_cid and _bool_config(self.config, "delete_old_conversation", True):
            await conv_mgr.delete_conversation(umo, curr_cid)

        event.set_extra("_clean_ltm_session", False)
        return "爆破完成：旧上下文已清理，前情提要已注入新会话。"

    async def _build_summary(
        self,
        event: AstrMessageEvent,
        history: list[dict[str, Any]],
        provided_summary: str,
    ) -> str:
        provided_summary = (provided_summary or "").strip()
        if not _bool_config(self.config, "use_current_provider_for_summary", True):
            return provided_summary or self._fallback_summary(history)

        prompt = (
            "请总结下面这段旧会话，生成爆破换房后的接续备忘。\n\n"
            f"{_history_to_text(history)}\n\n"
            "如果上面内容不足，就基于用户本轮要求和已有信息给出最小可用备忘。"
        )
        if provided_summary:
            prompt += f"\n\n用户或工具补充的前情提要：\n{provided_summary}"

        provider_ids = []
        try:
            provider_ids.append(await self.context.get_current_chat_provider_id(event.unified_msg_origin))
        except Exception:
            pass
        fallback_provider = _string_config(self.config, "fallback_summary_provider_id", "").strip()
        if fallback_provider and fallback_provider not in provider_ids:
            provider_ids.append(fallback_provider)

        for provider_id in provider_ids:
            if not provider_id:
                continue
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=SUMMARY_SYSTEM_PROMPT,
                )
                summary = (getattr(resp, "completion_text", "") or "").strip()
                if summary:
                    return summary
            except Exception as exc:
                logger.warning(f"[爆破] 上下文总结失败 provider={provider_id}: {exc}")

        return provided_summary or self._fallback_summary(history)

    def _fallback_summary(self, history: list[dict[str, Any]]) -> str:
        if not history:
            return "当前没有可迁移的旧上下文；请在新会话中自然继续回应用户。"
        readable = _history_to_text(history, max_chars=2200)
        return (
            "自动总结接口不可用，以下是旧会话最近内容摘录，请据此继续：\n"
            f"{readable}"
        )

    def _sync_skill_installation(self) -> None:
        if not _bool_config(self.config, "install_context_cutover_skill", True):
            try:
                from astrbot.core.skills.skill_manager import SkillManager

                SkillManager().set_skill_active(SKILL_NAME, False)
            except Exception:
                pass
            return

        try:
            from astrbot.core.skills.skill_manager import SkillManager
            from astrbot.core.utils.astrbot_path import get_astrbot_skills_path

            src = PLUGIN_DIR / "skills" / SKILL_NAME
            dst = Path(get_astrbot_skills_path()) / SKILL_NAME
            if src.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            SkillManager().set_skill_active(SKILL_NAME, True)
        except Exception as exc:
            logger.warning(f"[爆破] 安装 context-cutover skill 失败: {exc}")

    async def _sync_scheduled_cutover(self) -> None:
        cron_mgr = getattr(self.context, "cron_manager", None)
        if cron_mgr is None:
            return
        jobs = []
        try:
            jobs = await cron_mgr.list_jobs("active_agent")
        except Exception:
            return
        managed_jobs = [
            job
            for job in jobs
            if job.name == SCHEDULE_JOB_NAME
            or (isinstance(job.payload, dict) and job.payload.get("plugin") == PLUGIN_NAME)
        ]

        enabled = _bool_config(self.config, "enable_scheduled_cutover", False)
        session = _string_config(self.config, "scheduled_session", "").strip()
        if not enabled or not session:
            for job in managed_jobs:
                await cron_mgr.delete_job(job.job_id)
            return

        try:
            cron_expr = _parse_time_to_cron(_string_config(self.config, "scheduled_time", "03:00"))
        except ValueError as exc:
            logger.warning(f"[爆破] 定时爆破时间格式错误，已跳过同步: {exc}")
            return
        timezone = _string_config(self.config, "scheduled_timezone", "Asia/Shanghai") or "Asia/Shanghai"
        note = _string_config(
            self.config,
            "scheduled_note",
            "这是定时爆破任务。请先根据需要写入小窝日记，然后直接调用 perform_memory_transfer 完成爆破换房。",
        )
        payload = {
            "session": session,
            "note": note,
            "plugin": PLUGIN_NAME,
            "origin": "plugin",
        }
        description = "爆破插件定时换房。更推荐由 bot 自主创建定时任务：先写小窝日记，再调用爆破睡大觉。"
        if managed_jobs:
            keep = managed_jobs[0]
            await cron_mgr.update_job(
                keep.job_id,
                name=SCHEDULE_JOB_NAME,
                cron_expression=cron_expr,
                timezone=timezone,
                payload=payload,
                description=description,
                enabled=True,
                persistent=True,
            )
            for job in managed_jobs[1:]:
                await cron_mgr.delete_job(job.job_id)
        else:
            await cron_mgr.add_active_job(
                name=SCHEDULE_JOB_NAME,
                cron_expression=cron_expr,
                timezone=timezone,
                payload=payload,
                description=description,
                enabled=True,
                persistent=True,
            )

    def _set_config_value(self, key: str, value: Any) -> None:
        try:
            self.config[key] = value
        except Exception:
            return
        save = getattr(self.config, "save_config", None)
        if callable(save):
            result = save()
            if inspect.isawaitable(result):
                logger.warning("[爆破] 配置保存返回了协程，请在 WebUI 中确认配置是否已保存。")

    def _status_text(self) -> str:
        return (
            "爆破状态：\n"
            f"- 功能：{'开启' if _bool_config(self.config, 'enable_cutover', True) else '关闭'}\n"
            f"- LLM 工具：{'开启' if _bool_config(self.config, 'enable_llm_tool', True) else '关闭'}\n"
            f"- 自动总结：{'开启' if _bool_config(self.config, 'use_current_provider_for_summary', True) else '关闭'}\n"
            f"- 定时爆破：{'开启' if _bool_config(self.config, 'enable_scheduled_cutover', False) else '关闭'}\n"
            f"- 绑定会话：{_string_config(self.config, 'scheduled_session', '') or '未绑定'}"
        )

    def _help_text(self) -> str:
        return (
            "爆破指令：\n"
            "/sl：立即总结当前上下文并换房。\n"
            "/sl bind：绑定当前会话，用于定时爆破。\n"
            "/sl status：查看状态。\n"
            "/sl skill：安装并启用爆破 skills。"
        )
