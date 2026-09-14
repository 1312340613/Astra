# 任务：让 LLMClient.chat 支持 max_tokens 透传

## 背景

`OpenAICompatibleProvider.chat()` 已经支持 `max_tokens` 参数（用于限制输出预算，
并对推理型模型保留 8192 的下限），但外层包装 `LLMClient.chat()` 还不接受这个关键字。

`agent/runtime/llm.py` 中，委派（delegate）任务调用 `LLMClient.chat(...)` 并传入
`max_tokens=16384`，当前代码会直接抛 `TypeError: chat() got an unexpected keyword
argument 'max_tokens'`。

## 要求

修改 `agent/runtime/llm.py` 中的 `LLMClient.chat()`：

1. 增加参数 `max_tokens: int | None = None`。
2. 当调用方传入 `max_tokens`（非 None）时，将其转发给底层
   `self.provider.chat(messages, tools, tool_choice, max_tokens=max_tokens)`。
3. 不传时保持现有行为不变（不向 provider 传递 max_tokens）。

注意：
- 这是最小修复，不需要改动 `OpenAICompatibleProvider.chat()`（它已经支持该参数）。
- 不要改动 `chat_limited` 或其他方法的语义。

## 验收

完成后运行仓库现有测试确认没有回归；修复的语义是：
- `LLMClient.chat(max_tokens=16384)` → provider 收到 `max_tokens=16384`
- `LLMClient.chat()` → provider 收到 `max_tokens=None`（默认）
