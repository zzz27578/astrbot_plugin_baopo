# 更新日志

## v0.1.2

- 对照 AstrBot 最新 `de572e3` 工具调用链，收窄并优化消息事件兼容解析逻辑。
- 优先支持官方新版 `ContextWrapper.context.event` 路径，同时保留旧版 `AstrMessageEvent` 直传兼容。
- 保留少量旧壳子字段兼容，避免递归扫描任意对象属性导致误判。

## v0.1.1

- 修复 AstrBot 新版工具调用传入 `ContextWrapper` 后，爆破工具无法读取真实消息事件的问题。
- 兼容旧版直接传入 `AstrMessageEvent` 与新版 `ctx.context.event` 的调用路径。
- 工具无法解析事件时返回明确错误，避免继续触发缺属性异常。

## v0.1.0

- 新增 `perform_memory_transfer` 爆破工具。
- 新增 `/sl` 指令。
- 新增当前模型总结、备用模型总结、隐藏工具规范注入。
- 新增 `context-cutover` skill。
- 新增定时爆破会话绑定和主动任务同步。
