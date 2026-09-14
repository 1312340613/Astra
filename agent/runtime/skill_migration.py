"""Explicit, reversible import of legacy skill candidates into automatic skills.

The SQLite source remains history. Observations are never promoted to skills or
memory. A migration ledger prevents repeated imports, including after archiving.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path

from .learning import LearningStore
from .learning_scope import learning_scope, scope_key
from .skill_learning import MAX_TREE_CHARS, LearnedSkills, _digest
from .skill_provenance import AUTO_ORIGIN


def _source_files(learned: LearnedSkills, name: str) -> dict[str, str]:
    directory = learned.skills._existing_skill_dir(name)
    if directory is None:
        raise ValueError(f"Original patch target is missing: {name}")
    files = {}
    total = 0
    for path in sorted(directory.rglob("*")):
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("Legacy import does not follow symlinks")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            learned.skills._target(name, relative)
            if path.stat().st_size > MAX_TREE_CHARS * 4:
                raise ValueError("Legacy skill is too large")
            files[relative] = path.read_text(encoding="utf-8")
            total += len(files[relative])
            if total > MAX_TREE_CHARS:
                raise ValueError("Legacy skill is too large")
    return files


def migrate_legacy(store: LearningStore, learned: LearnedSkills) -> dict:
    with learned.locked():
        state = learned._state()
        ledger = state.setdefault("legacy_migrations", {})
        if not isinstance(ledger, dict):
            raise ValueError("Invalid legacy migration ledger")
        proposals = store.list("pending", limit=10_000)
        pending = [row for row in proposals if row["id"] not in ledger]
        if not pending:
            return {"imported": 0, "history_only": 0, "remaining": 0, "blocked": [], "run_id": None}
        # A consistent, private backup includes the source's committed WAL data.
        backup = learned._state_path(learned.home / "legacy-backups" / (uuid.uuid4().hex + ".db"))
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
                target.row_factory = sqlite3.Row
                pending = [LearningStore._decode(row) for row in target.execute(
                    "SELECT * FROM learning_proposals WHERE status='pending' ORDER BY id")
                    if row["id"] not in ledger]
        batch = pending[:100]
        after: dict[str, dict[str, str]] = {}
        actions = []
        blocked = []
        imported = history_only = 0
        for row in batch:
            payload = row["payload"]
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if row["kind"] not in {"skill_create", "skill_patch"}:
                ledger[row["id"]] = {"destination": "history", "payload_sha256": fingerprint}
                actions.append({"action": "preserve_history", "candidate": row["id"], "reason": "Standalone observation/fact; not a procedural skill."})
                history_only += 1
                continue
            try:
                original = learned.skills._validate_name(payload["name"])
                name = original
                if learned.skills._existing_skill_dir(name) is not None or name in after or learned._path(name).parent.exists():
                    name = "legacy-" + original[:47] + "-" + row["id"][-8:]
                if learned.skills._existing_skill_dir(name) is not None or name in after or learned._path(name).parent.exists():
                    raise ValueError(f"Migration name is already occupied: {name}")
                if row["kind"] == "skill_patch":
                    files = _source_files(learned, original)
                    relative = Path(str(payload.get("file_path") or "SKILL.md").replace("\\", "/")).as_posix()
                    old = str(payload.get("old_string") or "")
                    if not old or files.get(relative, "").count(old) != 1:
                        raise ValueError("Historical patch no longer matches exactly; original target was preserved")
                    files[relative] = files[relative].replace(old, str(payload.get("new_string") or ""), 1)
                else:
                    files = {"SKILL.md": str(payload.get("content") or "").replace("\r\n", "\n").strip() + "\n"}
                header = learned.skills._frontmatter(files["SKILL.md"])
                if learned.skills._validate_name(header["name"]) != original:
                    raise ValueError("Candidate name does not match its content")
                files["SKILL.md"] = re.sub(r"(?m)^[ \t]*name[ \t]*:.*$", f"name: {name}", files["SKILL.md"], count=1)
                files["SKILL.md"] = learned.validate(name, files["SKILL.md"])
                if sum(map(len, files.values())) > MAX_TREE_CHARS:
                    raise ValueError("Legacy skill is too large")
                state["skills"][name] = {
                    "origin": AUTO_ORIGIN, "digest": _digest(files), "scope": scope_key(learning_scope()),
                    "sources": [{"legacy_id": row["id"], "session_id": row["session_id"], "reason": row["reason"],
                                 "evidence": payload.get("evidence", ""), "original_name": original,
                                 "notice": "Imported historical automatic summary; content has not been re-executed or independently verified."}],
                }
                after[name] = files
                ledger[row["id"]] = {"destination": "auto", "name": name, "payload_sha256": fingerprint}
                actions.append({"action": "import", "candidate": row["id"], "name": name, "reason": "Imported automatic procedure; existing user skills preserved."})
                imported += 1
            except (KeyError, ValueError, OSError, UnicodeError) as exc:
                blocked.append({"candidate": row["id"], "reason": str(exc)})
        for row in batch:
            if store.get(row["id"]) != row:
                raise ValueError("Legacy source changed during import; no migration was applied")
        if imported:
            state["cursor"] = ""
        result = {"imported": imported, "history_only": history_only, "blocked": blocked,
                  "remaining": len(pending) - imported - history_only, "backup": str(backup)}
        record = learned.commit(kind="migration", state=state, after=after, actions=actions,
                                source={"migration": result}, expected={name: {} for name in after})
        return {**result, "run_id": record["id"]}


def format_migration(result: dict) -> str:
    lines = [f"Legacy migration: {result['imported']} automatic skill(s), {result['history_only']} history-only record(s), {result['remaining']} remaining.",
             "Original candidates and user-added skills were preserved. /learn review checks the automatic skills only."]
    lines.extend(f"Not imported: {item['candidate']} — {item['reason']}" for item in result["blocked"])
    if result["run_id"]:
        lines += [f"Backup: {result['backup']}", f"Record: /learn history {result['run_id']}",
                  f"Undo migration: /learn undo {result['run_id']}"]
    return "\n".join(lines)
