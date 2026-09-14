import asyncio
from types import SimpleNamespace

from agent.runtime.context_compressor import (
    COMPRESSION_RECAP_PREFIX,
    ContextCompressor,
    SUMMARY_PREFIX,
)
from agent.runtime.hooks import HookRegistry
from agent.runtime.project_instructions import ProjectInstructions
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry


def test_project_instructions_layer_agents_and_match_path_rules(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "packages" / "api"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nested ordinary", encoding="utf-8")
    (nested / "AGENTS.override.md").write_text("nested override", encoding="utf-8")
    rules = tmp_path / ".astra" / "rules"
    rules.mkdir(parents=True)
    (rules / "python.md").write_text(
        "---\npaths:\n  - packages/api/**/*.py\n---\nRun API contract tests.",
        encoding="utf-8",
    )
    assert ProjectInstructions._parse_rule(rules / "python.md", 32_000) is not None

    instructions = ProjectInstructions(nested, trusted=True)

    assert "root rule" in instructions.base_prompt
    assert "nested override" in instructions.base_prompt
    assert "nested ordinary" not in instructions.base_prompt
    assert "Run API contract tests" in instructions.rules_for({"packages/api/routes/user.py"}), instructions.rules
    assert instructions.rules_for({"packages/web/page.tsx"}) == ""


def test_path_rule_keeps_leading_dot_directory_name(tmp_path):
    rules = tmp_path / ".astra" / "rules"
    rules.mkdir(parents=True)
    (rules / "github.md").write_text(
        "---\npaths: [.github/**/*.yml]\n---\nValidate workflows.",
        encoding="utf-8",
    )
    instructions = ProjectInstructions(tmp_path, trusted=True)
    assert "Validate workflows" in instructions.rules_for({".github/workflows/ci.yml"})


def test_project_instruction_budget_is_bounded(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("X" * 10_000, encoding="utf-8")
    instructions = ProjectInstructions(tmp_path, max_bytes=256, trusted=True)
    assert len(instructions.base_prompt.encode("utf-8")) < 512


def test_active_skill_contract_reloads_current_skill_file(tmp_path):
    store = SkillStore(tmp_path / "skills")
    store.create("review", "---\nname: review\ndescription: Review code\n---\nFirst contract")
    agent = ReActAgent(
        "agent",
        SimpleNamespace(config=SimpleNamespace(model="test")),
        ToolRegistry(),
        skill_store=store,
    )
    agent._active_skill_names.add("review")
    first = agent._active_skill_contract()
    store.patch("review", "First contract", "Updated contract")
    second = agent._active_skill_contract()
    assert "First contract" in first
    assert "Updated contract" in second
    assert first != second


def test_compaction_dispatches_pre_and_post_hooks():
    class LLM:
        async def chat_limited(self, *_args, **_kwargs):
            return {"content": "## Active Task\ncontinue\n## Critical Context\nnone"}

    hooks = HookRegistry()
    events = []
    hooks.on_pre_compact(lambda event: events.append(("pre", event)))
    hooks.on_post_compact(lambda event: events.append(("post", event)))
    compressor = ContextCompressor(LLM(), tail_token_budget=1, hooks=hooks)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old task that should be summarized"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current task that remains active"},
    ]
    compressed = asyncio.run(compressor.compress(messages, 1_000, force=True))
    assert not any(item.get("content") == "old answer" for item in compressed)
    assert sum(
        str(item.get("content", "")).startswith(SUMMARY_PREFIX)
        for item in compressed
    ) == 1
    assert sum(
        str(item.get("content", "")).startswith(COMPRESSION_RECAP_PREFIX)
        for item in compressed
    ) == 1
    assert [name for name, _event in events] == ["pre", "post"]
    assert events[1][1]["summary_chars"] > 0
    assert events[1][1]["messages_after"] == len(compressed)
