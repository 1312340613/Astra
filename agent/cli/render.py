"""Terminal rendering — markdown → ANSI, tool cards, stream renderer, status bar"""

import re


def render_text(text: str) -> str:
    text = re.sub(r'`([^`]+)`', r'\033[36m\1\033[0m', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\033[1m\1\033[0m', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\033[3m\1\033[0m', text)
    text = re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'\033[3m\1\033[0m', text)
    return text


def render_reasoning(text: str) -> str:
    """渲染思维链内容——灰色斜体，带 ▸ 前缀（用于非流式场景）"""
    lines = text.split("\n")
    rendered = []
    for line in lines:
        if line.strip():
            rendered.append(f"  \033[90m▸ {line}\033[0m")
        else:
            rendered.append("")
    return "\n".join(rendered)


class ReasoningRenderer:
    """流式思维链渲染器——只在完整行上加 ▸ 前缀，避免逐字碎片问题"""

    def __init__(self):
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        output = []

        while "\n" in self._buffer:
            idx = self._buffer.index("\n")
            line = self._buffer[:idx]
            self._buffer = self._buffer[idx + 1:]
            if line.strip():
                output.append(f"  \033[90m▸ {line}\033[0m")
            else:
                output.append("")

        return "\n".join(output) + ("\n" if output else "")

    def flush(self) -> str:
        if not self._buffer:
            return ""
        line = self._buffer.rstrip()
        self._buffer = ""
        if line:
            return f"  \033[90m▸ {line}\033[0m"
        return ""


def render_tool_block(name: str, code: str = "", output: str = "", error: str = "",
                      duration_ms: int = 0) -> str:
    width = 58

    # Status icon + duration
    if error:
        status = "\033[31m✗ FAILED\033[0m"
    elif output or code:
        status = "\033[32m✓ OK\033[0m"
    else:
        status = "\033[90m—\033[0m"

    duration_str = f" \033[90m{duration_ms}ms\033[0m" if duration_ms else ""
    header = f" {status} \033[1m{name}\033[0m{duration_str}"
    lines = [f"  \033[90m╭{'─' * width}╮\033[0m"]
    lines.append(f"  \033[90m│\033[0m {header:<{width + len(header) - len(strip_ansi(header))}} \033[90m│\033[0m")

    if code:
        lines.append(f"  \033[90m├─ code {'─' * max(0, width - 5)}┤\033[0m")
        for line in code.strip().split("\n"):
            lines.append(f"  \033[90m│\033[0m \033[36m{line[:width]:<{width}}\033[0m")

    sections = ([(output, False)] if output else []) + ([(error, True)] if error else [])
    for i, (content, is_error) in enumerate(sections):
        label = "stderr" if is_error else "output"
        lines.append(f"  \033[90m├─ {label} {'─' * max(0, width - len(label) - 1)}┤\033[0m")
        for line in content.strip().split("\n"):
            display = line[:width]
            if is_error:
                lines.append(f"  \033[90m│\033[0m \033[31m{display:<{width}}\033[0m")
            else:
                lines.append(f"  \033[90m│\033[0m {display:<{width}}")

    lines.append(f"  \033[90m╰{'─' * width}╯\033[0m")
    return "\n".join(lines)


def strip_ansi(text: str) -> str:
    return re.sub(r'\033\[[0-9;]*m', '', text)


class StreamRenderer:
    """流式渲染器——安全处理跨 chunk 的 markdown 标记"""

    def __init__(self, max_buffer: int = 240):
        self._buffer = ""
        self._max_buffer = max_buffer

    def feed(self, chunk: str) -> str:
        self._buffer += chunk

        flush_at = max(self._buffer.rfind("\n"), self._buffer.rfind(" "))
        if len(self._buffer) >= self._max_buffer and flush_at > 0:
            ready = self._buffer[:flush_at + 1]
            self._buffer = self._buffer[flush_at + 1:]
            return render_text(_strip_unbalanced_markers(ready))

        bc = self._buffer.count("**")
        tc = self._buffer.count("`")

        if bc % 2 == 0 and tc % 2 == 0:
            result = render_text(self._buffer)
            self._buffer = ""
            return result

        return ""

    def flush(self) -> str:
        if not self._buffer:
            return ""
        cleaned = _strip_unbalanced_markers(self._buffer)
        result = render_text(cleaned)
        self._buffer = ""
        return result


def _strip_unbalanced_markers(text: str) -> str:
    cleaned = text
    if cleaned.count("**") % 2 != 0:
        last = cleaned.rfind("**")
        if last != -1:
            cleaned = cleaned[:last] + cleaned[last + 2:]
    if cleaned.count("`") % 2 != 0:
        last = cleaned.rfind("`")
        if last != -1:
            cleaned = cleaned[:last] + cleaned[last + 1:]
    return cleaned


# ── Status Bar ────────────────────────────────────────────────────

def render_status_bar(
    status: str,        # "ready" | "thinking"
    model: str,
    context_used: int = 0,       # 当前 prompt token 估计值
    context_limit: int = 100_000, # 上下文窗口 token 上限
    context_used_pct: float = 0,  # 当前上下文占用百分比
    session_tokens: int = 0,      # 会话累计总 token
) -> str:
    """渲染底部状态栏，显示模型、上下文用量和会话总 token。

    输出形如：
      ═══ ● ready ═══ model ═══ ctx 18.5K/128K 14% ═══ Σ 117.1K ═══
    """
    status_display = f"\033[32m● {status}\033[0m" if status == "ready" else f"\033[33m◐ {status}\033[0m"

    used_str = _fmt_token(context_used)
    limit_str = _fmt_token(context_limit)
    session_str = _fmt_token(session_tokens)

    if context_used_pct > 85:
        pct_color = "\033[31m"   # red
    elif context_used_pct > 60:
        pct_color = "\033[33m"   # yellow
    else:
        pct_color = "\033[32m"   # green

    pct_str = f"{pct_color}{context_used_pct:.0f}%\033[0m" if context_used_pct > 0 else "-"

    bar = (
        f"  \033[90m═══ {status_display} \033[90m═══"
        f" \033[36m{model}\033[0m"
        f" \033[90m═══\033[0m"
        f" ctx {used_str}\033[90m/{limit_str}\033[0m {pct_str}"
        f" \033[90m═══ Σ {session_str}\033[0m"
        f" \033[90m═══\033[0m"
    )
    return bar


def _fmt_token(n: int) -> str:
    """格式化 token 数：1,234 或 12.3K 或 1.2M"""
    if n < 10_000:
        return f"{n:,}"
    elif n < 1_000_000:
        return f"{n / 1000:.1f}K"
    else:
        return f"{n / 1_000_000:.1f}M"
