"""Read skill provenance independently of skill text or catalog categories."""

from __future__ import annotations

import json
from pathlib import Path


AUTO_ORIGIN = "auto"


def learning_home(root: Path) -> Path:
    root = root.absolute()
    return root.parent / (root.name + "-learning")


def read_learning_state(root: Path) -> dict:
    path = learning_home(root) / "index.json"
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Learning state cannot use symlinks")
    if not path.exists():
        return {"schema": 1, "skills": {}, "cursor": "", "last_review": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("skills"), dict):
        raise ValueError("Invalid learning state; original files were preserved")
    return state


def automatic_names(state: dict) -> list[str]:
    # An unknown origin never grants permission to modify a user's files.
    return sorted(name for name, item in state["skills"].items()
                  if isinstance(item, dict) and item.get("origin") == AUTO_ORIGIN)
