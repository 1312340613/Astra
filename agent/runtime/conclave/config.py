"""Conclave configuration — persistent settings with auto-save."""

from __future__ import annotations

from agent.runtime.paths import state_dir

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

CONFIG_DIR = state_dir()
CONFIG_PATH = CONFIG_DIR / "conclave.json"
LEGACY_CONFIG_PATH = Path.home() / ".config" / "hermes" / "conclave.json"


@dataclass
class ConclaveConfig:
    """Persistent Conclave settings.

    All changes are auto-saved to the project-local ``.astra`` state.
    """

    # Chairperson LLM: provider/model identifier
    # Format: "provider_name/model_name" e.g. "openai-compatible/gemma4"
    chairperson_model: str = "active"

    # Default expert selection mode: "auto" or comma-separated list
    experts: str = "auto"

    # Max sources per expert in the final report
    max_sources_per_expert: int = 5

    # Max tokens for chairperson synthesis
    max_tokens: int = 2048

    # Bounded expert workflow: two searches and one compact report per expert.
    max_search_rounds: int = 2
    expert_max_tokens: int = 1024
    max_parallel_experts: int = 4

    # Enable cross-discussion phase
    cross_discussion: bool = False

    @classmethod
    def load(cls) -> ConclaveConfig:
        """Load from disk, or return defaults if not found."""
        source = CONFIG_PATH
        if not source.exists() and LEGACY_CONFIG_PATH.exists():
            source = LEGACY_CONFIG_PATH
        if source.exists():
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                config = cls(**{
                    k: data[k]
                    for k in cls.__dataclass_fields__
                    if k in data
                })
                if source == LEGACY_CONFIG_PATH:
                    config.save()
                return config
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("corrupt conclave config, using defaults: %s", e)
        return cls()

    def save(self) -> Path:
        """Persist to disk. Returns the config file path."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(CONFIG_PATH)
        return CONFIG_PATH

    def update(self, **kwargs: Any) -> None:
        """Update fields and auto-save."""
        for key, value in kwargs.items():
            if key in self.__dataclass_fields__:
                setattr(self, key, value)
        self.save()

    def format(self) -> str:
        """Human-readable config dump."""
        lines = [
            "## Conclave 配置",
            f"  主席模型: {self.chairperson_model}",
            f"  专家选择: {self.experts}",
            f"  每位专家来源上限: {self.max_sources_per_expert}",
            f"  最大 token: {self.max_tokens}",
            f"  专家检索轮数: {self.max_search_rounds}",
            f"  专家报告 token: {self.expert_max_tokens}",
            f"  专家并发: {self.max_parallel_experts}",
            f"  交叉讨论: {'开' if self.cross_discussion else '关'}",
            f"  配置文件: {CONFIG_PATH}",
        ]
        return "\n".join(lines)
logger = logging.getLogger(__name__)
