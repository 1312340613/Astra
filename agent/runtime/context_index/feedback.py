"""Small, version-scoped utility statistics owned entirely by Astra."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .models import RecommendationCandidate
from .sqlite_reader import open_readonly

OUTCOMES = {"useful": 1, "irrelevant": -1, "outdated": -1}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def item_key(item: RecommendationCandidate) -> str:
    revision = item.revision or _digest(item.private_text or item.description)
    return _digest(f"{item.source}:{item.locator.kind}:{item.locator.primary}:{item.locator.secondary}:{revision}")


def scope_key(workspace: str, intent: str) -> str:
    return _digest(f"{workspace}:{intent}")


class FeedbackStore:
    """Explicit feedback only. Builds, submissions and opens never add rewards."""

    def __init__(self, path: Path):
        self.path = path

    def adjustments(self, scope: str, keys: tuple[str, ...]) -> dict[str, float]:
        keys = tuple(dict.fromkeys(keys))[:120]
        if not keys or not self.path.is_file():
            return {}
        try:
            with open_readonly(self.path) as db:
                rows = db.execute(
                    "SELECT item_key, SUM(value) AS total, COUNT(*) AS n FROM feedback "
                    f"WHERE scope = ? AND item_key IN ({','.join('?' for _ in keys)}) GROUP BY item_key",
                    (scope, *keys),
                ).fetchall()
            return {str(row["item_key"]): 0.1 * float(row["total"]) / (int(row["n"]) + 5) for row in rows}
        except (OSError, sqlite3.DatabaseError):
            return {}

    def record(self, scope: str, key: str, event_id: str, outcome: str) -> bool:
        if outcome not in OUTCOMES:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=0.05)
            try:
                with db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS feedback (event_id TEXT PRIMARY KEY, scope TEXT NOT NULL, item_key TEXT NOT NULL, value INTEGER NOT NULL)"
                    )
                    db.execute("CREATE INDEX IF NOT EXISTS feedback_scope_item ON feedback(scope, item_key)")
                    db.execute(
                        "INSERT INTO feedback VALUES (?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET value=excluded.value",
                        (_digest(event_id), scope, key, OUTCOMES[outcome]),
                    )
                    db.execute(
                        "DELETE FROM feedback WHERE rowid NOT IN (SELECT rowid FROM feedback ORDER BY rowid DESC LIMIT 5000)"
                    )
            finally:
                db.close()
            return True
        except (OSError, sqlite3.DatabaseError):
            return False
