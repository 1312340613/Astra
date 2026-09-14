"""LLM-as-judge relevance labelling for the Context Index benchmark.

The judge runs exclusively against the local oMLX server (privacy: panel text
never leaves the machine). Parsing is total — any malformed model output
degrades to ``None`` so the runner can count the request as unjudged instead of
silently trusting garbage.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["build_judge_prompt", "judge_case", "parse_judge_response"]

_SYSTEM = (
    "你是检索质量评审员。给定用户请求与若干候选历史上下文条目，"
    "为每个条目打相关性分：0=与请求无关；1=主题沾边但对该请求帮助有限；"
    "2=正是完成该请求需要回看的内容。"
    "只输出一个 json 对象，键为给定的 handle、值为 0/1/2 整数，"
    "不得输出其它文本、不得虚构 handle。"
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_judge_prompt(query: str, rows: Sequence[tuple[str, str, str]]) -> tuple[str, str]:
    """Assemble (system, user) prompts; rows are (handle, source, description)."""

    lines = [f"- {handle} [{source}] {description}" for handle, source, description in rows]
    user = "用户请求：\n" + query.strip()[:2000] + "\n\n候选条目：\n" + "\n".join(lines)
    return _SYSTEM, user


def _coerce_label(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        label = value
    elif isinstance(value, float) and value.is_integer():
        label = int(value)
    else:
        return None
    return max(0, min(2, label))


def parse_judge_response(text: str, valid_handles: set[str]) -> dict[str, int] | None:
    """Extract the first {handle: label} object; fail closed on anything else."""

    candidate = _FENCE_RE.search(text) or _BRACE_RE.search(text)
    if candidate is None:
        return None
    try:
        raw = json.loads(candidate.group(1) if candidate.re is _FENCE_RE else candidate.group(0))
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    labels: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in valid_handles:
            continue
        label = _coerce_label(value)
        if label is not None:
            labels[key] = label
    return labels or None


Transport = Callable[[str, str], str | None]


class OMLXTransport:
    """Minimal OpenAI-compatible chat client for the local oMLX server."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", model: str = "", *,
                 api_key: str = "", timeout_s: float = 180.0, max_tokens: int = 1600) -> None:
        # max_tokens 必须覆盖 thinking 模型（gemma-4 实测 reasoning ~1.3k tokens）
        # 之后再输出 JSON 标签；512 会被 thinking 吃光导致 content 空、全 case unjudged。
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens

    def __call__(self, system: str, user: str) -> str | None:
        body: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": self._max_tokens,
        }
        if self._model:
            body["model"] = self._model
        request = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self._api_key}"} if self._api_key else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            return None


def judge_case(query: str, rows: Sequence[tuple[str, str, str]], *,
               transport: Transport) -> dict[str, int] | None:
    """Label one case's candidates; None means unjudged (error/malformed)."""

    if not rows:
        return {}
    system, user = build_judge_prompt(query, rows)
    raw = transport(system, user)
    if raw is None:
        return None
    return parse_judge_response(raw, {handle for handle, _source, _description in rows})
