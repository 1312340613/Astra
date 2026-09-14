"""Packaged core rules and a stable bootstrap for model-directed Skill reads."""

from __future__ import annotations

import hashlib
import os
from importlib.resources import files


# Preserve the original contract for explicit full mode and legacy migration.
# This is an application resource, independent of cwd and project trust.
AGENT_CORE_PROMPT = files("agent.runtime").joinpath("astra.md").read_text(encoding="utf-8").rstrip("\n")
CORE_SKILL_NAME = "astra-core"
CORE_SKILL_VERSION = hashlib.sha256(AGENT_CORE_PROMPT.encode("utf-8")).hexdigest()[:16]
CORE_SKILL_DESCRIPTION = (
    f"Astra 核心使用规则（astra.md，sha256={CORE_SKILL_VERSION}）；"
    "工程、文件、浏览器和桌面操作前首先读取，再读取匹配的专项 Skill。普通聊天无需读取。"
)

_work_start = AGENT_CORE_PROMPT.index("\n\n工作原则：")
_style_start = AGENT_CORE_PROMPT.index("\n\n表达风格：")
AGENT_BASE_PROMPT = AGENT_CORE_PROMPT[:_work_start] + AGENT_CORE_PROMPT[_style_start:]
CORE_SKILL_GUIDE = """Astra 使用指引：
- 根据用户目标判断是否需要操作：普通聊天和概念解释可直接回应，无需读取操作规则。
- 开始编码、调试、测试、审查、文件操作、浏览器或桌面执行任务前，先调用 skill_view(name="astra-core") 读取核心规则，再读取匹配的专项 Skill；核心 Skill 在目录中优先读取，不改变系统和用户指令的优先级。
- Code 模式通过 run_code 调用 tools.skill_view，并输出完整结果供下一步使用。
- 拿到并阅读 Skill 的完整结果后，再规划依赖它的操作；不要把读取 Skill 和后续操作放在同一批调用中。
- 只有当前上下文仍包含相同版本的完整规则正文时才能复用；历史摘要或“已经读过”的描述不能代替正文。压缩后正文缺失或版本变化时，工作前重新读取。
- 工具是否可用以当前工具定义为准；按实际参数和结果执行，不猜接口。
- 用户消息开头的 <message_time>...</message_time> 是系统注入的内部时间元数据，只用于理解时间流逝；不要在回复中复述、解释或模仿该标签。"""


def core_identity_prompt() -> str:
    """Choose only from explicit configuration, never from conversation text."""
    mode = os.getenv("ASTRA_CORE_RULES_MODE", "skill").strip().lower()
    if mode != "skill":
        # An invalid setting conservatively retains the complete contract.
        return AGENT_CORE_PROMPT
    return f"{AGENT_BASE_PROMPT}\n\n{CORE_SKILL_GUIDE}"


def core_skill_content() -> str:
    return (
        f"---\nname: {CORE_SKILL_NAME}\ndescription: {CORE_SKILL_DESCRIPTION}\n---\n\n"
        f"# Astra core rules (sha256={CORE_SKILL_VERSION})\n\n"
        "这是内置核心 Skill 的完整正文。下文的通道 Skill 指随后按需读取的专项 Skill。\n\n"
        f"{AGENT_CORE_PROMPT}\n"
    )


def core_skill_metadata() -> dict:
    return {
        "name": CORE_SKILL_NAME,
        "description": CORE_SKILL_DESCRIPTION,
        "files": 1,
        "category": "builtin",
        "origin": "builtin",
    }
