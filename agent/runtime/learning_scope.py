"""Dependency-free scope labels shared by learning and its native readers."""

import hashlib
import json
import os
import platform
from pathlib import Path


def learning_scope(workspace: str | Path | None = None) -> dict[str, str]:
    path = Path(workspace or Path.cwd()).resolve()
    # Reuse a Git checkout's scope when a task works in one of its subfolders.
    root = next((parent for parent in (path, *path.parents) if (parent / ".git").exists()), path)
    return {"workspace": os.path.normcase(str(root)), "platform": platform.system()}


def scope_key(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def learning_scope_matches(metadata: dict, workspace: str | Path | None = None) -> bool:
    proof = metadata.get("learning_verification")
    if proof is None:
        return True
    return isinstance(proof, dict) and proof.get("scope") == learning_scope(workspace)


def scoped_skill(content: str, scope: dict) -> str:
    if not content.startswith("---\n") or "\n---" not in content[4:]:
        raise ValueError("A scoped skill requires YAML frontmatter")
    end = content.find("\n---", 4)
    header = [line for line in content[4:end].splitlines() if not line.startswith("astra_learning_scope:")]
    header.append("astra_learning_scope: " + scope_key(scope))
    return "---\n" + "\n".join(header) + content[end:]
