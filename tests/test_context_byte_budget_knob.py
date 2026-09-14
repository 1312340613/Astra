"""`CONTEXT_MAX_PROMPT_TOKENS` 让压缩阈值能按传输层的真实限制调小。

Astra 的上下文预算以 **token** 计，而 Aliyun token-plan 网关限的是**请求体字节**：超过约 1MB 就
不再排水，客户端表现为 write timeout（伪装成 `TimeoutError`）。工具密集会话里 token 预算远未触顶、
字节却早已越墙 —— 实测本会话最近 200 条文本记录 783 KB，再叠一张 Computer Use 截图（重编码后约
320 KB）就越线，所以"总在 snapshot 之后超时"。修它需要能把预算调小，而这件事不该改代码才行。
"""

from agent.runtime.context import AgentContext


def test_env_lowers_prompt_token_budget(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_MAX_PROMPT_TOKENS", "45000")
    assert AgentContext(system_prompt="s").max_prompt_tokens == 45000


def test_env_must_be_positive_integer_otherwise_keep_default(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_MAX_PROMPT_TOKENS", "0")
    default = AgentContext(system_prompt="s").max_prompt_tokens
    assert default == 100_000
    monkeypatch.setenv("CONTEXT_MAX_PROMPT_TOKENS", "abc")
    assert AgentContext(system_prompt="s").max_prompt_tokens == default


def test_explicit_argument_beats_env(monkeypatch) -> None:
    # 调用方显式传值时必须赢过环境：评估回放、隔离 profile 和单测都靠显式小预算跑，
    # 若 env 覆盖一切，它们会被悄悄放大成真实预算（或反之被压平）。
    monkeypatch.setenv("CONTEXT_MAX_PROMPT_TOKENS", "45000")
    assert AgentContext(system_prompt="s", max_prompt_tokens=4096).max_prompt_tokens == 4096
