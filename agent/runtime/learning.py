"""Bounded learning discovery and explicit user maintenance commands."""

from __future__ import annotations

from agent.runtime.paths import state_path

import asyncio
import difflib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .context_compressor import _is_synthetic_user_turn
from .memory import CORE_SCOPES, MemoryStore
from .provider_errors import format_provider_error
from .skills import SkillStore

LEARNING_MODES = ("off", "review")
PROPOSAL_KINDS = ("memory", "observation", "skill_create", "skill_patch", "skill_write_file")
SKILL_CATEGORIES = ("coding", "creative", "research", "operations", "personal")
_SECRET = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*\S+")
logger = logging.getLogger(__name__)


class LearningProviderError(RuntimeError):
    """Safe, already-formatted failure from the learning model boundary."""


class LearningReviewResultError(ValueError):
    """A structured-output failure containing no provider-controlled text."""

    def __init__(self, code: str, *, response_chars: int, attempt: int):
        self.code = code if code in {"truncated_response", "invalid_json", "invalid_shape"} else "invalid_shape"
        self.response_chars = max(0, response_chars)
        self.attempt = attempt
        super().__init__(
            f"Learning review returned an invalid result "
            f"[code={self.code}, attempt={attempt}, response_chars={self.response_chars}]; "
            "evidence remains unprocessed"
        )


def _normalize_skill_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    value = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return value if value and len(value) <= 64 else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _nonnegative_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


def _skill_content_with_frontmatter(name: str, content: str, reason: str) -> str:
    """Repair the small structural contract required by SkillStore.create."""
    text = str(content).replace("\r\n", "\n").strip()
    description = " ".join(str(reason).split()) or f"Reusable workflow for {name}"
    description = description.replace('"', "'")[:240]
    required = {"name": name, "description": description}

    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            header_lines = text[4:end].splitlines()
            keys = set()
            repaired_lines = []
            for line in header_lines:
                key = line.split(":", 1)[0].strip() if ":" in line else ""
                keys.add(key)
                repaired_lines.append(f'name: "{name}"' if key == "name" else line)
            additions = [
                f'{key}: "{value}"'
                for key, value in required.items()
                if key not in keys
            ]
            repaired_header = "\n".join([*repaired_lines, *additions])
            return f"---\n{repaired_header}\n---{text[end + 4:]}"

    header = "\n".join(f'{key}: "{value}"' for key, value in required.items())
    return f"---\n{header}\n---\n\n{text}\n"


_SKILL_PROVENANCE_MARKERS = (
    "对话中",
    "此次对话",
    "本次对话",
    "用户刚",
    "刚确立",
    "本会话",
    "最新一轮",
    "最新一张",
    "此次演示",
    "未来再遇",
    "避免重复试错",
    "现有技能目录",
    "无法安全 patch",
    "the conversation demonstrated",
    "this session",
    "current session",
    "user just established",
    "added as a new skill",
    "avoid repeating",
)


def _skill_quality_issue(content: str) -> str | None:
    text = str(content).strip()
    lowered = text.casefold()
    if any(marker.casefold() in lowered for marker in _SKILL_PROVENANCE_MARKERS):
        return "Skill content must describe a reusable workflow, not its source conversation or creation history"
    if not re.search(r"(?m)^#{1,6}\s+\S+", text):
        return "Skill content must include a heading"
    if not re.search(r"(?m)^\s*(?:[-*]\s+|\d+[.)]\s+)", text):
        return "Skill content must include actionable steps"
    return None


def _infer_skill_category(name: str, content: str) -> str:
    text = f"{name} {content}".casefold()
    groups = {
        "coding": ("code", "debug", "test", "workflow", "plan", "review", "agent-dev", "delegate"),
        "creative": ("image", "prompt", "qwen-mm", "video", "tts"),
        "research": ("research", "search", "pdf", "knowledge", "kb-", "citation", "paper"),
        "operations": ("triage", "network", "mcp", "wsl", "huggingface", "deploy", "server", "computer"),
        "personal": ("email", "fitness", "singapore", "dbs", "sillytavern", "session", "hermes"),
    }
    scores = {
        category: sum(1 for token in tokens if token.casefold() in text)
        for category, tokens in groups.items()
    }
    return max(scores, key=lambda category: scores[category]) if max(scores.values(), default=0) else ""


def default_learning_path() -> Path:
    override = os.getenv("AGENT_LEARNING_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return state_path("learning.db")


class LearningStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_learning_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS learning_proposals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    before_json TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_proposals_status_created
                    ON learning_proposals(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS learning_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_reviews_created
                    ON learning_reviews(created_at DESC, id DESC);
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mode(self) -> str:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT value FROM learning_settings WHERE key='mode'").fetchone()
        value = row["value"] if row else os.getenv("LEARNING_REVIEW_MODE", "review")
        return value if value in LEARNING_MODES else "review"

    @contextmanager
    def read_connection(self, *, timeout: float = 0.05) -> Iterator[sqlite3.Connection]:
        """A short, read-only lookup for optional task-related discovery."""
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def count(self, status: str = "pending") -> int:
        with self._connection() as db:
            return db.execute("SELECT count(*) FROM learning_proposals WHERE status=?", (status,)).fetchone()[0]

    def set_mode(self, mode: str) -> str:
        value = str(mode).strip().lower()
        if value not in LEARNING_MODES:
            raise ValueError(f"Learning mode must be one of: {', '.join(LEARNING_MODES)}")
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO learning_settings(key, value) VALUES ('mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )
        return value

    @staticmethod
    def _counter_key(session_id: str | None = None) -> str:
        session = str(session_id or "").strip()
        return f"turn_counter:{session}" if session else "turn_counter"

    def advance_review_counter(self, interval: int, session_id: str | None = None) -> bool:
        interval = max(1, interval)
        key = self._counter_key(session_id)
        with self._lock, self._connection() as db:
            row = db.execute("SELECT value FROM learning_settings WHERE key=?", (key,)).fetchone()
            count = int(row["value"]) + 1 if row and str(row["value"]).isdigit() else 1
            trigger = count >= interval
            next_value = "0" if trigger else str(count)
            db.execute(
                "INSERT INTO learning_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, next_value),
            )
        return trigger

    def reset_review_counter(self, session_id: str | None = None) -> None:
        key = self._counter_key(session_id)
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO learning_settings(key, value) VALUES (?, '0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'",
                (key,),
            )

    def record_review_summary(self, session_id: str, summary: str) -> None:
        """Keep a bounded, non-authoritative digest for later pattern recognition."""
        text = " ".join(_SECRET.sub(r"\1=[REDACTED]", str(summary)).split())[:4000]
        if not text:
            return
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO learning_reviews(session_id, summary, created_at) VALUES (?, ?, ?)",
                (str(session_id), text, _now()),
            )

    def recent_review_summaries(self, limit: int | None = None) -> list[dict[str, str]]:
        """Return recent digests as context, never as authoritative memory evidence."""
        bounded = max(
            1,
            min(
                int(limit or _positive_int_env("LEARNING_REVIEW_HISTORY_ITEMS", 8)),
                50,
            ),
        )
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT session_id, summary, created_at FROM learning_reviews "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def stage(self, session_id: str, kind: str, payload: dict[str, Any], reason: str, *, deduplicate: bool = True) -> dict[str, Any]:
        if kind not in PROPOSAL_KINDS:
            raise ValueError(f"Unknown learning proposal kind: {kind}")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(encoded) > 120_000:
            raise ValueError("Learning proposal is too large")
        with self._lock, self._connection() as db:
            existing = db.execute(
                "SELECT * FROM learning_proposals WHERE status='pending' AND kind=? AND payload_json=?",
                (kind, encoded),
            ).fetchone()
            if existing and deduplicate:
                return self._decode(existing)
            proposal_id = f"lr_{uuid.uuid4().hex[:8]}"
            now = _now()
            db.execute(
                "INSERT INTO learning_proposals VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)",
                (proposal_id, session_id, kind, encoded, str(reason).strip()[:1000], now, now),
            )
        result = self.get(proposal_id)
        assert result is not None
        return result

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM learning_proposals WHERE id=?", (proposal_id,)).fetchone()
        return self._decode(row) if row else None

    def list(self, status: str | None = "pending", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM learning_proposals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, min(limit, 10_000))),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM learning_proposals ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 10_000)),),
                ).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for source, target in (("payload_json", "payload"), ("before_json", "before"), ("result_json", "result")):
            raw = result.pop(source)
            result[target] = json.loads(raw) if raw else None
        return result

    def reject(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Only pending proposals can be rejected")
        self._set_status(proposal_id, "rejected")
        result = self.get(proposal_id)
        assert result is not None
        return result

    def defer(self, proposal_id: str, note: str) -> dict[str, Any]:
        """Keep a proposal actionable and persist why automatic application paused."""
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Only pending proposals can be deferred")
        detail = " ".join(str(note).split())[:400]
        reason = str(proposal.get("reason") or "")
        if detail and detail not in reason:
            reason = f"{reason} [Pending: {detail}]".strip()[:1000]
            with self._lock, self._connection() as db:
                db.execute(
                    "UPDATE learning_proposals SET reason=?, updated_at=? WHERE id=?",
                    (reason, _now(), proposal_id),
                )
        result = self.get(proposal_id)
        assert result is not None
        return result

    def record_repair_failure(
        self,
        proposal_id: str,
        repair_id: str,
        error: str,
    ) -> dict[str, Any]:
        """Keep a structured audit trail when a repair attempt fails."""
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Only pending proposals can record repair failures")
        detail = " ".join(str(error).split())[:500] or "Unknown repair failure"
        stored_result = proposal.get("result")
        prior_result = stored_result if isinstance(stored_result, dict) else {}
        attempts = list(prior_result.get("repair_attempts") or [])
        attempts.append(
            {
                "repair_id": str(repair_id),
                "error": detail,
                "at": _now(),
            }
        )
        self._set_status(
            proposal_id,
            "pending",
            result={
                **prior_result,
                "last_repair_id": str(repair_id),
                "last_repair_error": detail,
                "repair_attempts": attempts[-10:],
            },
        )
        return self.defer(
            proposal_id,
            f"Repair failed; use /learn show {proposal_id} for details",
        )

    def supersede(self, proposal_id: str, replacement_id: str) -> dict[str, Any]:
        """Close a pending proposal after a repaired replacement was applied."""
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Only pending proposals can be superseded")
        self._set_status(
            proposal_id,
            "superseded",
            result={"replacement_id": str(replacement_id)},
        )
        result = self.get(proposal_id)
        assert result is not None
        return result

    def approve(self, proposal_id: str, memory: MemoryStore, skills: SkillStore, *, verification: dict | None = None) -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Only pending proposals can be approved")
        payload = proposal["payload"]
        kind = proposal["kind"]
        before: dict[str, Any] | None = None
        if kind == "memory":
            scope = str(payload.get("scope", ""))
            if scope not in CORE_SCOPES:
                raise ValueError("Memory proposal has an invalid scope")
            result = memory.add_core(scope, str(payload.get("content", "")))
            before = {"memory_id": result["id"]}
        elif kind == "observation":
            content = str(payload.get("content", ""))
            session_id = str(proposal.get("session_id", ""))
            existing = memory.find_active_record("observation", content)
            if existing is not None and verification is not None:
                # Reusing an existing fact is not new corroboration. Trial
                # receipts remain on the learning item; do not inflate another
                # memory record's confidence or independent-evidence count.
                result = {"record_id": existing.record_id, "action": "reused"}
                before = {"record_id": existing.record_id, "created": False}
            elif existing is not None:
                record = memory.confirm_record(
                    existing.record_id,
                    confidence=0.98,
                    source_session_id=session_id,
                )
                result = {"record_id": record.record_id, "action": "confirmed"}
                before = {"record_id": record.record_id, "created": False}
            else:
                valid_until = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
                record = memory.add_record(
                    kind="observation",
                    content=content,
                    source_session_id=session_id,
                    valid_until=valid_until,
                    confidence=0.8 if verification is not None else 0.98,
                    salience=0.7,
                    tags=tuple(payload.get("tags") or ()),
                    metadata={
                        "retained_by": "learning-reviewer",
                        "evidence": str(payload.get("evidence", "")),
                        "evidence_role": str(payload.get("evidence_role", "")),
                        "evidence_count": 1,
                        "evidence_refs": [session_id] if session_id else [],
                        "ttl_days": 90,
                        "maturity": "confirmed",
                        **({"learning_verification": verification, "confidence_basis": "heuristic; scoped runtime checks, not a calibrated probability"} if verification is not None else {}),
                    },
                )
                result = {"record_id": record.record_id, "action": "created"}
                before = {"record_id": record.record_id, "created": True}
        elif kind == "skill_create":
            name = str(payload.get("name", ""))
            before = {"name": name, "file_path": "SKILL.md", "content": skills.raw_file(name, "SKILL.md")}
            result = skills.create(
                name,
                str(payload.get("content", "")),
                str(payload.get("category", "")) or None,
            )
        elif kind == "skill_patch":
            name = str(payload.get("name", ""))
            file_path = str(payload.get("file_path", "SKILL.md"))
            before = {"name": name, "file_path": file_path, "content": skills.raw_file(name, file_path)}
            result = skills.patch(
                name,
                str(payload.get("old_string", "")),
                str(payload.get("new_string", "")),
                file_path,
            )
        else:
            name = str(payload.get("name", ""))
            file_path = str(payload.get("file_path", ""))
            before = {"name": name, "file_path": file_path, "content": skills.raw_file(name, file_path)}
            result = skills.write_file(name, file_path, str(payload.get("content", "")))
        self._set_status(proposal_id, "applied", before=before, result=result)
        applied = self.get(proposal_id)
        assert applied is not None
        return applied

    def requires_approval(self, proposal: dict[str, Any], skills: SkillStore) -> bool:
        """Reserve approval for collisions, deletion, and major reduction."""
        kind = proposal["kind"]
        payload = proposal["payload"]
        if kind in {"memory", "observation"}:
            # LearningReviewer accepts Core memory only with an exact user
            # quote and observations only with exact user/tool evidence, so
            # these auditable additive entries need no second approval step.
            return False
        if kind == "skill_create":
            return skills.raw_file(str(payload.get("name", "")), "SKILL.md") is not None
        if kind == "skill_patch":
            old = str(payload.get("old_string", ""))
            new = str(payload.get("new_string", ""))
            # Exact rewrites and moderate simplification are reversible through
            # the stored snapshot. Empty or >50% reductions still need review.
            return not old.strip() or not new.strip() or len(new.strip()) * 2 < len(old.strip())
        if kind == "skill_write_file":
            current = skills.raw_file(
                str(payload.get("name", "")),
                str(payload.get("file_path", "")),
            )
            new_content = str(payload.get("content", ""))
            if current is None:
                return False
            return (
                not new_content.strip()
                or len(new_content.strip()) * 2 < len(current.strip())
            )
        return True

    def apply_additive(self, proposal_id: str, memory: MemoryStore, skills: SkillStore) -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Only pending proposals can be auto-applied")
        from .learning_lifecycle import LearningLifecycle

        lifecycle = LearningLifecycle(self, memory, skills)
        if lifecycle.managed(proposal_id):
            # Both automatic entry points must use the same proof gate.
            if lifecycle.activation_issue(proposal_id):
                return proposal
            return lifecycle.activate(proposal_id)
        if self.requires_approval(proposal, skills):
            return proposal
        return self.approve(proposal_id, memory, skills)

    def rollback(self, proposal_id: str, memory: MemoryStore, skills: SkillStore) -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if not proposal or proposal["status"] != "applied":
            raise ValueError("Only applied proposals can be rolled back")
        before = proposal.get("before") or {}
        if proposal["kind"] == "memory":
            if not memory.remove_core(str(before.get("memory_id", ""))):
                raise ValueError("Applied memory entry is no longer present")
        elif proposal["kind"] == "observation":
            if before.get("created") is not True:
                pass
            elif not memory.forget_record(str(before.get("record_id", ""))):
                raise ValueError("Applied observation is no longer active")
        else:
            skills.restore_file(str(before.get("name", "")), str(before.get("file_path", "")), before.get("content"))
        self._set_status(proposal_id, "rolled_back")
        result = self.get(proposal_id)
        assert result is not None
        return result

    def _set_status(
        self,
        proposal_id: str,
        status: str,
        *,
        before: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE learning_proposals SET status=?, before_json=COALESCE(?, before_json), "
                "result_json=COALESCE(?, result_json), updated_at=? WHERE id=?",
                (
                    status,
                    json.dumps(before, ensure_ascii=False) if before is not None else None,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    _now(),
                    proposal_id,
                ),
            )

    def format_list(self, status: str = "pending") -> str:
        items = self.list(status)
        if not items:
            return f"No {status} learning proposals."
        lines = [f"Learning proposals ({status}, {len(items)}):"]
        for item in items:
            target = item["payload"].get("name") or item["payload"].get("scope") or ""
            lines.append(f"  {item['id']}  {item['kind']:<16} {target} — {item['reason']}")
        return "\n".join(lines)

    def format_detail(self, proposal_id: str, skills: SkillStore) -> str:
        item = self.get(proposal_id)
        if not item:
            raise ValueError(f"Unknown learning proposal: {proposal_id}")
        payload = item["payload"]
        lines = [
            f"Proposal {item['id']}",
            f"Status: {item['status']}",
            f"Kind: {item['kind']}",
            f"Reason: {item['reason']}",
        ]
        with self._connection() as db:
            managed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_candidates'").fetchone()
            candidate = db.execute("SELECT revision, metadata_json FROM learning_candidates WHERE proposal_id=?", (proposal_id,)).fetchone() if managed else None
        if candidate is not None:
            metadata = json.loads(candidate["metadata_json"])
            lines.extend([
                f"Candidate revision: {candidate['revision']} (pending means unverified)",
                "Contract: " + json.dumps(metadata["contract"], ensure_ascii=False),
                "Verification scope: " + json.dumps(metadata["scope"], ensure_ascii=False),
                "Automatic activation requires runtime-checked validation; /learn approve is an explicit user override.",
            ])
            with self._connection() as db:
                trials = db.execute("SELECT id, revision, status, trial_json FROM learning_trials WHERE proposal_id=? ORDER BY rowid DESC LIMIT 8", (proposal_id,)).fetchall()
            lines.append("Recent trials:")
            for trial in trials:
                detail = json.loads(trial["trial_json"])
                lines.append(f"  {trial['id']} · revision {trial['revision']} · {detail['variant']} · {detail['phase']} · {trial['status']} · {detail['case']}")
            if not trials:
                lines.append("  None; this candidate has no runtime validation.")
            proof = (item.get("result") or {}).get("learning_verification")
            if proof:
                lines.append("Activation proof: " + json.dumps(proof, ensure_ascii=False))
        stored_result = item.get("result")
        result = stored_result if isinstance(stored_result, dict) else {}
        repair_attempts = result.get("repair_attempts", [])
        if repair_attempts:
            lines.append("Repair attempts:")
            for attempt in repair_attempts:
                repair_id = attempt.get("repair_id") or "(no replacement staged)"
                lines.append(
                    f"  {attempt.get('at', '')}  {repair_id} — {attempt.get('error', '')}"
                )
        if item["status"] == "superseded" and result.get("replacement_id"):
            lines.append(f"Replacement: {result['replacement_id']}")
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
            return "\n".join(lines)
        if item["kind"] == "memory":
            lines.extend([f"Scope: {payload.get('scope')}", f"Content: {payload.get('content')}"])
        elif item["kind"] == "observation":
            lines.extend([
                f"Content: {payload.get('content')}",
                f"Evidence ({payload.get('evidence_role')}): {payload.get('evidence')}",
            ])
        elif item["kind"] == "skill_patch":
            name = str(payload.get("name", ""))
            file_path = str(payload.get("file_path", "SKILL.md"))
            try:
                current = skills.raw_file(name, file_path) or ""
            except ValueError as exc:
                lines.append(f"Stored target is invalid: {exc}")
                lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
                return "\n".join(lines)
            old = str(payload.get("old_string", ""))
            updated = current.replace(old, str(payload.get("new_string", "")), 1) if current.count(old) == 1 else current
            diff = difflib.unified_diff(
                current.splitlines(), updated.splitlines(),
                fromfile=f"{name}/{file_path}", tofile=f"{name}/{file_path} (proposed)", lineterm="",
            )
            lines.extend(diff)
        else:
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
        return "\n".join(lines)


def format_review_outcome(outcome: dict[str, Any]) -> str:
    proposals = list(outcome.get("proposals") or [])
    applied = [item for item in proposals if item.get("status") == "applied"]
    pending = [item for item in proposals if item.get("status") == "pending"]
    rejected = [item for item in proposals if item.get("status") == "rejected"]
    lines = []
    summary = str(outcome.get("summary") or "").strip()
    if summary and outcome.get("manual", True):
        lines.append(f"Learning summary: {summary}")
    lines.append(
        "Learning layers: "
        f"Core memory: {sum(item.get('kind') == 'memory' for item in applied)} applied · "
        f"Observations: {sum(item.get('kind') == 'observation' for item in applied)} applied · "
        f"Skills: {sum(str(item.get('kind', '')).startswith('skill_') for item in applied)} applied"
    )
    if applied:
        lines.append(
            f"Automatically saved {len(applied)} additive learning item(s): "
            + ", ".join(item["id"] for item in applied)
        )
    if pending:
        lines.append(f"Saved {len(pending)} unverified learning candidate(s): " + ", ".join(item["id"] for item in pending))
        lines.append("Kept for relevant-task trials; nothing was installed. Inspect with /learn show <id>.")
    if rejected:
        lines.append(
            f"Automatically rejected {len(rejected)} unsafe or inferred learning item(s): "
            + ", ".join(item["id"] for item in rejected)
        )
    if not proposals:
        lines.append("Nothing durable was worth recording.")
    return "\n".join(lines)


class LearningReviewer:
    def __init__(self, llm: Any, store: LearningStore, memory: MemoryStore, skills: SkillStore):
        self.llm = llm
        self.store = store
        self.memory = memory
        self.skills = skills
        from .learning_lifecycle import LearningLifecycle

        self.lifecycle = LearningLifecycle(store, memory, skills)

    @staticmethod
    def request_timeout() -> float:
        return _positive_float_env("LEARNING_REVIEW_TIMEOUT", 360.0)

    @staticmethod
    def max_retries() -> int:
        return _nonnegative_int_env("LEARNING_REVIEW_MAX_RETRIES", 1)

    async def _await_provider(self, awaitable: Any) -> Any:
        """Await one review-model call and expose only its safe summary."""
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary is default-deny
            message = format_provider_error(
                exc,
                component="learning-review",
                timeout=self.request_timeout(),
            )
            raise LearningProviderError(message) from None

    async def review(
        self,
        messages: list[dict],
        session_id: str,
        *,
        manual: bool = False,
    ) -> list[dict[str, Any]]:
        outcome = await self.review_outcome(messages, session_id, manual=manual)
        return outcome["proposals"]

    async def _review_response(self, prompt: list[dict]) -> dict[str, Any]:
        """Repair one malformed result within a shared, cancellable deadline."""
        timeout = self.request_timeout()
        deadline = asyncio.get_running_loop().time() + timeout
        max_tokens = max(256, _positive_int_env("LEARNING_REVIEW_MAX_TOKENS", 2048))
        limited_chat = getattr(self.llm, "chat_limited", None)
        async with asyncio.timeout(timeout):
            for attempt in (1, 2):
                if limited_chat is not None:
                    response = await self._await_provider(limited_chat(
                        prompt,
                        max_tokens=max_tokens,
                        temperature=0.1,
                        disable_thinking=os.getenv("LEARNING_REVIEW_DISABLE_THINKING", "1").lower()
                        in {"1", "true", "yes", "on"},
                        request_timeout=timeout if attempt == 1 else max(
                            0.001, deadline - asyncio.get_running_loop().time(),
                        ),
                        max_retries=self.max_retries(),
                    ))
                else:
                    response = await self._await_provider(self.llm.chat(prompt))
                content = response.get("content", "") if isinstance(response, dict) else ""
                content = content if isinstance(content, str) else ""
                parsed = self._parse_json(content)
                truncated = isinstance(response, dict) and response.get("finish_reason") in ("length", "max_tokens")
                code = "truncated_response" if truncated else (
                    "invalid_json" if not parsed else "invalid_shape"
                )
                if not truncated and isinstance(parsed, dict) and isinstance(parsed.get("proposals"), list):
                    return parsed
                failure = LearningReviewResultError(code, response_chars=len(content), attempt=attempt)
                logger.warning(
                    "learning review result rejected code=%s attempt=%s response_chars=%s component=learning-review",
                    failure.code, attempt, failure.response_chars,
                )
                if attempt == 2:
                    raise failure
                correction = (
                    f"\n\nResult validation failed: {code}. Return one complete JSON object with a string "
                    "summary and a proposals array. Keep the summary and proposal content concise so the "
                    "entire JSON fits the output budget. Use proposals: [] when no durable learning is "
                    "supported. Do not include prose outside the JSON or fabricate evidence."
                )
                prompt = [*prompt[:-1], {**prompt[-1], "content": prompt[-1]["content"] + correction}]
                if truncated:
                    max_tokens = max(max_tokens, min(max_tokens * 2, 8192))
        raise AssertionError("review attempts must return or raise")

    async def repair(self, proposal_id: str) -> dict[str, Any]:
        """Ask the active model to repair one malformed pending proposal."""
        original = self.store.get(proposal_id)
        if not original or original["status"] != "pending":
            raise ValueError("Only pending proposals can be repaired")

        payload = original["payload"]
        is_record_proposal = original["kind"] in {"memory", "observation"}
        stored_name = str(payload.get("name", ""))
        name = (
            stored_name
            if is_record_proposal
            else _normalize_skill_name(stored_name)
        )
        invalid_skill_name = not is_record_proposal and not name
        file_path = str(payload.get("file_path", "SKILL.md"))
        memory_scope = str(payload.get("scope", ""))
        memory_evidence = str(payload.get("evidence", "")).strip()
        memory_max_content_chars: int | None = None
        memory_rule = ""
        if original["kind"] == "memory" and memory_scope in CORE_SCOPES:
            usage = self.memory.core_usage(memory_scope)
            memory_max_content_chars = int(usage["available_chars"])
            memory_rule = (
                f" This memory repair is locked to scope={memory_scope!r}; preserve the exact evidence "
                f"quote {memory_evidence!r}. Core memory has room for at most "
                f"{memory_max_content_chars} content characters, so return a concise durable fact within "
                "that limit. Omit implementation file lists, test counts, and narrative detail before "
                "dropping the core safety contract."
            )
        target_content = ""
        root_content = ""
        if name:
            try:
                target_content = self.skills.raw_file(name, file_path) or ""
            except ValueError:
                target_content = ""
            try:
                root_content = self.skills.raw_file(name, "SKILL.md") or ""
            except ValueError:
                root_content = ""

        prompt = [
            {
                "role": "system",
                "content": (
                    "Repair one pending learning proposal without changing its semantic intent or inventing facts. "
                    "Return JSON only as {\"message\":string,\"proposal\":{...}}. The message is a concise, natural "
                    "assistant reply explaining to the user what you repaired and why; match the language visible in "
                    "the proposal when practical, and never expose internal JSON or hidden reasoning. Preserve memory "
                    "evidence exactly. Memory cannot become "
                    "a skill and skills cannot become memory. Skill proposals may change among skill_create, skill_patch, "
                    "and skill_write_file to satisfy storage rules. For skill_patch, file_path must be SKILL.md or start "
                    "with references/, templates/, scripts/, or assets/; old_string must be a non-empty exact substring "
                    "of the supplied current file and new_string must include the intended update. To append to SKILL.md, "
                    "use a unique existing anchor as old_string and return that anchor plus the addition as new_string. "
                    "For a new supporting document, use skill_write_file under references/. Never emit secrets or paths "
                    "outside the named skill. Skill names must use lowercase ASCII letters, digits, and hyphens only, "
                    "with no more than 64 characters."
                    + memory_rule
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original proposal:\n{json.dumps(original, ensure_ascii=False)}\n\n"
                    f"Locked skill target after normalization: {name or '(invalid)'}\n\n"
                    f"Current requested target ({file_path}):\n{target_content[:20_000]}\n\n"
                    f"Current {name}/SKILL.md:\n{root_content[:30_000]}"
                ),
            },
        ]
        repaired: dict[str, Any] | None = None
        try:
            if invalid_skill_name:
                raise ValueError(
                    f"Original skill name {stored_name!r} cannot be normalized to a valid skill name"
                )
            if memory_max_content_chars is not None and memory_max_content_chars <= 0:
                raise ValueError(
                    f"Core {memory_scope} memory has no free capacity; consolidate or remove an "
                    "existing entry before repairing this proposal"
                )
            attempt_prompt = prompt
            normalized: dict[str, Any] | None = None
            repair_message = ""
            last_issue = "missing proposal JSON"
            last_shape = "content_chars=0, reasoning_chars=0, finish_reason=unknown"
            for attempt in range(2):
                limited_chat = getattr(self.llm, "chat_limited", None)
                if limited_chat is not None:
                    response = await self._await_provider(
                        limited_chat(
                            attempt_prompt,
                            max_tokens=max(
                                512,
                                _positive_int_env("LEARNING_REVIEW_MAX_TOKENS", 2048),
                            ),
                            temperature=0.1,
                            disable_thinking=os.getenv(
                                "LEARNING_REVIEW_DISABLE_THINKING",
                                "1",
                            ).lower()
                            in {"1", "true", "yes", "on"},
                            reasoning_effort="low",
                        )
                    )
                else:
                    response = await self._await_provider(
                        self.llm.chat(attempt_prompt)
                    )
                content = str(response.get("content", ""))
                reasoning = str(response.get("reasoning_content", ""))
                last_shape = (
                    f"content_chars={len(content)}, reasoning_chars={len(reasoning)}, "
                    f"finish_reason={response.get('finish_reason') or 'unknown'}"
                )
                raw: dict[str, Any] | None = None
                candidate_message = ""
                for candidate in (content, reasoning):
                    parsed = self._parse_json(candidate)
                    value = parsed.get("proposal") if isinstance(parsed, dict) else None
                    if not isinstance(value, dict):
                        proposals = parsed.get("proposals", []) if isinstance(parsed, dict) else []
                        value = proposals[0] if isinstance(proposals, list) and proposals else None
                    if isinstance(value, dict):
                        raw = value
                        candidate_message = " ".join(
                            str(parsed.get("message", "")).split()
                        )[:2000]
                        break
                if is_record_proposal and isinstance(raw, dict):
                    nested = raw.get("payload")
                    locked = dict(nested) if isinstance(nested, dict) else dict(raw)
                    locked["evidence"] = memory_evidence
                    if original["kind"] == "memory":
                        locked["scope"] = memory_scope
                    else:
                        locked["evidence_role"] = str(payload.get("evidence_role", ""))
                        locked["tags"] = list(payload.get("tags") or ())
                    raw = {**raw, "kind": original["kind"], "payload": locked}
                normalized = self._normalize(
                    raw,
                    user_messages=(memory_evidence,) if payload.get("evidence_role", "user") == "user" else (),
                    tool_messages=(memory_evidence,) if payload.get("evidence_role") == "tool" else (),
                )
                if normalized is None:
                    last_issue = "missing or invalid proposal fields"
                elif original["kind"] == "memory" and normalized["kind"] != "memory":
                    last_issue = "memory repair changed proposal kind"
                    normalized = None
                elif is_record_proposal and normalized["kind"] != original["kind"]:
                    last_issue = "record repair changed proposal kind"
                    normalized = None
                elif not is_record_proposal and normalized["kind"] in {"memory", "observation"}:
                    last_issue = "skill repair changed into a memory record"
                    normalized = None
                elif (
                    memory_max_content_chars is not None
                    and len(str(normalized["payload"].get("content", "")))
                    > memory_max_content_chars
                ):
                    last_issue = (
                        f"memory content exceeded {memory_max_content_chars} characters"
                    )
                    normalized = None
                else:
                    repair_message = candidate_message
                    break
                if attempt == 0:
                    retry_rule = (
                        f" The memory content must be at most {memory_max_content_chars} characters; "
                        f"scope must be {memory_scope!r}; evidence must remain exactly "
                        f"{memory_evidence!r}."
                        if original["kind"] == "memory"
                        else " Keep the original skill and return one valid skill proposal."
                    )
                    attempt_prompt = [
                        *prompt,
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response was unusable: {last_issue}. "
                                "Correct it now and return JSON only as "
                                "{\"message\":string,\"proposal\":{...}}."
                                + retry_rule
                            ),
                        },
                    ]
            if normalized is None:
                raise ValueError(
                    f"Model did not return a valid repair proposal after 2 attempts "
                    f"({last_issue}; {last_shape})"
                )
            if not is_record_proposal:
                if not name:
                    raise ValueError("Original skill proposal has no skill name")
                # The model may repair the operation and target file, but it must
                # never move a pending change into a different skill.
                normalized["payload"]["name"] = name
                if normalized["kind"] == "skill_create":
                    normalized["payload"]["content"] = _skill_content_with_frontmatter(
                        name,
                        str(normalized["payload"].get("content", "")),
                        normalized["reason"],
                    )

            repaired = self.store.stage(
                original["session_id"],
                normalized["kind"],
                normalized["payload"],
                f"Repair of {proposal_id}: {normalized['reason']}",
            )
            if repaired["id"] == proposal_id:
                raise ValueError("Model returned the unchanged proposal")
            applied = self.store.approve(repaired["id"], self.memory, self.skills)
        except Exception as exc:
            repair_id = repaired["id"] if repaired else ""
            if repaired:
                current = self.store.get(repair_id) or repaired
                if current.get("status") == "pending":
                    self.store.defer(repair_id, f"Repair application failed: {exc}")
                    self.store.reject(repair_id)
            try:
                self.store.record_repair_failure(proposal_id, repair_id, str(exc))
            except (OSError, UnicodeError, ValueError):
                pass
            if isinstance(exc, LearningProviderError):
                raise LearningProviderError(str(exc)) from None
            if repaired:
                raise ValueError(
                    f"Repaired proposal {repair_id} could not be applied: {exc}"
                ) from exc
            raise ValueError(f"Repair failed before a replacement was staged: {exc}") from exc
        superseded = self.store.supersede(proposal_id, applied["id"])
        if not repair_message:
            repair_message = (
                "I condensed the memory proposal to fit the available Core Memory space while "
                "preserving its original evidence."
                if original["kind"] == "memory"
                else "I repaired the proposal so it satisfies the current skill storage rules "
                "without changing its original intent."
            )
        if not is_record_proposal and stored_name != name:
            repair_message = (
                f"{repair_message} Normalized skill target `{stored_name}` to `{name}`."
            )
        return {
            "original": superseded,
            "replacement": applied,
            "message": repair_message,
        }

    async def review_outcome(
        self,
        messages: list[dict],
        session_id: str,
        *,
        manual: bool = False,
    ) -> dict[str, Any]:
        if self.store.mode() == "off" and not manual:
            return {"summary": "", "proposals": []}
        sources = self.lifecycle.new_review_sources(session_id, messages)
        if not sources and not manual:
            return {"summary": "", "proposals": [], "skipped": "no-new-evidence"}
        # Old/retrieved text may give context, but only new original sources
        # can be cited. Explicit /learn summarize may reread at the user's request.
        evidence = sources if sources else messages
        transcript = self._transcript(evidence)
        if not self._message_texts(evidence, "user") and not self._message_texts(evidence, "tool"):
            return {"summary": "", "proposals": []}
        review_history = self.store.recent_review_summaries()
        catalog = json.dumps(self.skills.list(), ensure_ascii=False)
        core = {
            scope: [item["content"] for item in self.memory.list_core(scope)]
            for scope in CORE_SCOPES
        }
        prompt = [
            {
                "role": "system",
                "content": (
                    "You are a conservative learning reviewer. Return JSON only with shape "
                    "{\"summary\":string,\"proposals\":[...]}. Proposals may be: "
                    "memory {kind,scope(memory|user),content,evidence,reason}; skill_create {kind,name,content,reason}; "
                    "observation {kind,content,evidence,evidence_role(user|tool),tags?,reason}; "
                    "skill_patch {kind,name,file_path,old_string,new_string,reason}; skill_write_file "
                    "{kind,name,file_path,content,reason}. Core memory is durable user identity or preference; "
                    "observations are verified project facts, environment state, decisions, or outcomes; skills are reusable how-to. "
                    "Use judgment rather than a fixed repetition count. Notice recurring user goals, repeated operating patterns, "
                    "and workflows likely to save effort in future sessions, including patterns visible only when recent learning "
                    "summaries are considered together. Do not force a proposal merely because a topic recurred. Distinguish a "
                    "durable workflow from coincidental repetition and explain that judgment in reason. Recent learning summaries "
                    "are non-authoritative pattern hints only: they may establish recurrence, but cannot serve as exact evidence "
                    "for memory or observations or supply unverified procedural details. Do not save secrets, guesses, transient "
                    "failures, negative claims about tools, or one-off narratives. "
                    "A skill must generalize a reusable workflow with a trigger, actionable steps, and verification or failure handling; "
                    "never mention the source conversation, a just-established preference, or why a new skill was created. "
                    f"For skill_create, choose at most one category from {', '.join(SKILL_CATEGORIES)}; omit it only when no category fits. "
                    "Skill names must use lowercase ASCII letters, digits, and hyphens only, with no more than 64 characters. "
                    "Only propose memory for a durable fact or preference explicitly stated by the user. For every memory "
                    "proposal, evidence must be a short exact quote copied from a USER message; never cite assistant or tool text. "
                    "For every observation, evidence must be a short exact quote copied from the declared USER or TOOL role; "
                    "never use assistant-only claims. Observations expire after 90 days unless reconfirmed. "
                    "Prefer no proposal over low-value sediment. Never propose a skill patch unless the exact old text "
                    "was visible in the supplied conversation. All automatic proposals remain isolated candidates until verified. "
                    "An exact quote proves provenance, not that your conclusion follows from it. A user request is not evidence of completion. "
                    "An injection event does not prove that injection caused a behavior change. Retain uncertain explanations as hypotheses. "
                    "For each proposal include learning={claim_type: fact|procedure|hypothesis|improvement, trigger, benefit, verification_plan}. "
                    "Use fact only for a literal tool-observed fact, procedure for an executable workflow, and improvement only for a testable comparison. "
                    "A fact's content must be one short literal observation copied from the tool evidence, not a paraphrased "
                    "summary or a combination of claims. Put explanations in reason. Its verification_plan must name a "
                    "normal independent observation that could produce that literal again, never echo the claim itself. "
                    "For user preferences use memory; for operational advice use a procedure; uncertain conclusions stay hypotheses. "
                    "The model may later revise or trial a candidate using the learning tool within the relevant task's existing budget and permissions. "
                    "Do not manufacture experiments, validation results or learning quotas. Prefer extending an existing candidate over duplication: "
                    "include candidate_id to revise that existing pending candidate (or propose a replacement for an applied one). "
                    "Avoid creating a new skill when the catalog already describes the same workflow. The summary should mention "
                    "potentially reusable patterns even when evidence is not yet strong enough for a proposal, so a later review "
                    "can recognize continuity. Maximum 5 proposals."
                    + (
                        " The user explicitly requested a summary-and-record pass now. Return a concise useful summary even "
                        "when there is nothing durable to save."
                        if manual else ""
                    )
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Existing core memory: {json.dumps(core, ensure_ascii=False)}\n"
                    f"Existing skill catalog: {catalog}\n"
                    f"Existing learning candidates: {json.dumps(self.lifecycle.search(limit=20), ensure_ascii=False)}\n"
                    "Recent learning summaries (pattern hints only; not evidence): "
                    f"{json.dumps(review_history, ensure_ascii=False)}\n\n"
                    f"Conversation:\n{transcript}"
                ),
            },
        ]
        parsed = await self._review_response(prompt)
        proposals = parsed["proposals"]
        summary = (
            " ".join(_SECRET.sub(r"\1=[REDACTED]", str(parsed.get("summary", ""))).split())[:2000]
            if isinstance(parsed, dict)
            else ""
        )
        self.store.record_review_summary(session_id, summary)
        user_messages = self._message_texts(evidence, "user")
        tool_messages = self._message_texts(evidence, "tool")
        processed = []
        unchanged = []
        for raw in proposals[:5] if isinstance(proposals, list) else []:
            candidate_id = str(raw.get("candidate_id") or "") if isinstance(raw, dict) else ""
            prior = self.store.get(candidate_id) if candidate_id and self.lifecycle.managed(candidate_id) else None
            normalized = self._normalize(
                raw,
                user_messages=user_messages,
                tool_messages=tool_messages,
                prior=prior,
            )
            if normalized is None:
                continue
            contract = raw.get("learning") if isinstance(raw, dict) else None
            if candidate_id:
                staged = self.lifecycle.revise(
                    candidate_id, normalized, contract=contract if isinstance(contract, dict) else {},
                    messages=messages, session_id=session_id, manual=manual,
                )
            else:
                staged = self.lifecycle.propose(
                    normalized, session_id, contract=contract if isinstance(contract, dict) else None,
                    messages=messages, origin="review", manual=manual,
                )
            (unchanged if staged.get("operation") == "unchanged" else processed).append(staged)
        self.lifecycle.mark_reviewed(session_id, evidence)
        return {"summary": summary, "proposals": processed, "unchanged": unchanged, "manual": manual}

    @staticmethod
    def _transcript(messages: list[dict]) -> str:
        """Build broad context without letting tool traffic hide user intent."""
        budget = _positive_int_env("LEARNING_REVIEW_CONTEXT_CHARS", 64_000)
        user_budget = max(4_000, budget // 2)
        user_lines: list[str] = []
        trace_lines: list[str] = []
        for message in messages:
            role = str(message.get("role", ""))
            if role not in {"user", "assistant", "tool"}:
                continue
            if role == "user" and _is_synthetic_user_turn(message):
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
            text = _SECRET.sub(r"\1=[REDACTED]", str(content)).strip()
            if text:
                clipped = text[:4000]
                trace_lines.append(f"{role.upper()}: {clipped}")
                if role == "user":
                    user_lines.append(f"USER: {clipped}")
        user_context = "\n".join(user_lines)
        if len(user_context) > user_budget:
            user_context = user_context[-user_budget:]
        heading = "[User-authored requests across the reviewed session]\n"
        trace_heading = "\n\n[Recent execution trace]\n"
        trace_budget = max(0, budget - len(heading) - len(user_context) - len(trace_heading))
        trace = "\n".join(trace_lines)
        if len(trace) > trace_budget:
            trace = trace[-trace_budget:]
        return f"{heading}{user_context}{trace_heading}{trace}".strip()

    @staticmethod
    def _message_texts(messages: list[dict], role: str) -> tuple[str, ...]:
        texts: list[str] = []
        for message in messages:
            if str(message.get("role", "")) != role:
                continue
            if role == "user" and _is_synthetic_user_turn(message):
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict)
                )
            text = str(content).strip()
            if text:
                texts.append(text)
        return tuple(texts)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        value = text.strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", value)
            if not match:
                return {}
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}

    @staticmethod
    def _normalize(
        raw: Any,
        *,
        user_messages: tuple[str, ...] = (),
        tool_messages: tuple[str, ...] = (),
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        # A managed candidate's original exact quote remains valid provenance
        # when the source turn has left the active context. Reusing it permits
        # revision; it never supplies a fresh runtime receipt or corroboration.
        if prior is not None:
            previous = prior.get("payload") or {}
            quote = str(previous.get("evidence") or "")
            role = "user" if prior.get("kind") == "memory" else previous.get("evidence_role")
            if quote and role == "user":
                user_messages = (*user_messages, quote)
            elif quote and role == "tool":
                tool_messages = (*tool_messages, quote)
        if not isinstance(raw, dict):
            return None
        nested_payload = raw.get("payload")
        fields = nested_payload if isinstance(nested_payload, dict) else raw
        kind = str(raw.get("kind") or fields.get("kind", ""))
        reason = " ".join(str(raw.get("reason") or fields.get("reason", "")).split())[:1000]
        if kind == "memory":
            scope = str(fields.get("scope", ""))
            content = " ".join(str(fields.get("content", "")).split())
            evidence = " ".join(str(fields.get("evidence", "")).split())
            evidence_is_user_quote = bool(evidence) and any(
                evidence in " ".join(message.split())
                for message in user_messages
            )
            if (
                scope not in CORE_SCOPES
                or not content
                or _SECRET.search(content)
                or not evidence_is_user_quote
            ):
                return None
            payload = {"scope": scope, "content": content, "evidence": evidence}
        elif kind == "observation":
            content = " ".join(str(fields.get("content", "")).split())[:4000]
            evidence = " ".join(str(fields.get("evidence", "")).split())[:1000]
            evidence_role = str(fields.get("evidence_role", "")).strip().lower()
            evidence_messages = user_messages if evidence_role == "user" else tool_messages
            evidence_is_exact = bool(evidence) and any(
                evidence in " ".join(message.split())
                for message in evidence_messages
            )
            raw_tags = fields.get("tags", ())
            tags = []
            if isinstance(raw_tags, (list, tuple)):
                for raw_tag in raw_tags[:12]:
                    tag = " ".join(str(raw_tag).split())[:80]
                    if tag and tag not in tags:
                        tags.append(tag)
            if (
                evidence_role not in {"user", "tool"}
                or not content
                or not evidence_is_exact
                or _SECRET.search(content)
                or _SECRET.search(evidence)
            ):
                return None
            payload = {
                "content": content,
                "evidence": evidence,
                "evidence_role": evidence_role,
                "tags": tags,
            }
        elif kind == "skill_create":
            name = _normalize_skill_name(fields.get("name", ""))
            content = str(fields.get("content", ""))
            if not name or not content.strip():
                return None
            if _skill_quality_issue(content):
                return None
            category = str(fields.get("category", "")).strip().lower()
            if category not in SKILL_CATEGORIES:
                category = _infer_skill_category(name, content)
            payload = {
                "name": name,
                "content": _skill_content_with_frontmatter(name, content, reason),
                "category": category,
            }
        elif kind == "skill_patch":
            name = _normalize_skill_name(fields.get("name", ""))
            if not name:
                return None
            payload = {
                "name": name,
                "file_path": str(fields.get("file_path", "SKILL.md")),
                "old_string": str(fields.get("old_string", "")),
                "new_string": str(fields.get("new_string", "")),
            }
        elif kind == "skill_write_file":
            name = _normalize_skill_name(fields.get("name", ""))
            if not name:
                return None
            payload = {
                "name": name,
                "file_path": str(fields.get("file_path", "")),
                "content": str(fields.get("content", "")),
            }
        else:
            return None
        if any(_SECRET.search(str(value)) for value in payload.values()):
            return None
        return {"kind": kind, "payload": payload, "reason": reason or "Reusable learning from the reviewed turn"}
