"""Mechanical memory maintenance: explicit expiry and exact scoped deduplication.

No sentence extraction, fuzzy merging, confidence promotion or TTL extension.
Original records remain available for inspection after deduplication.
"""

from __future__ import annotations

import json
import logging

from .memory_records import MemoryRecord, MemoryRecordRepository, _now

logger = logging.getLogger(__name__)


def _duplicate_key(record: MemoryRecord) -> tuple:
    # Unknown metadata may describe scope or validity. Keep it in the identity
    # rather than maintaining a fragile allowlist of presumed semantic fields.
    metadata = {key: value for key, value in record.metadata.items()
                if key not in {"consolidation", "merged_record_ids", "merged_into"}}
    return (record.content, record.source_session_id, record.source_message_id,
            "immediate" if record.valid_from == record.created_at else record.valid_from,
            record.valid_until, record.tags, record.confidence, record.salience,
            record.supersedes_id, json.dumps(metadata, sort_keys=True, ensure_ascii=False))


class MemoryConsolidator:
    def __init__(self, repo: MemoryRecordRepository):
        self._repo = repo

    def expire_stale(self, *, now: str = "") -> int:
        """Mark active records whose valid_until has passed as expired.

        Only touches records with an explicit non-empty valid_until.
        Records without valid_until (permanent facts) are never expired.
        """
        current = now or _now()
        expired_count = 0
        with self._repo._lock, self._repo._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM memory_records "
                "WHERE status='active' AND valid_until != '' AND valid_until <= ?",
                (current,),
            ).fetchall()
            for row in rows:
                record = self._repo._from_row(row)
                metadata = dict(record.metadata)
                events = [item for item in metadata.get("lifecycle_events", []) if isinstance(item, dict)]
                events.append({"at": current, "event": "expired", "reason": "valid_until"})
                metadata["lifecycle_events"] = events[-30:]
                metadata["expired_at"] = current
                db.execute(
                    "UPDATE memory_records SET status='expired', metadata_json=? "
                    "WHERE id=? AND status='active'",
                    (json.dumps(metadata, ensure_ascii=False, sort_keys=True), record.record_id),
                )
                expired_count += 1
        if expired_count:
            logger.info("Consolidation: expired %d stale record(s)", expired_count)
        return expired_count

    def merge_duplicate_observations(self) -> int:
        """Retire exact duplicates within the same provenance, scope and lifetime.

        The read and all writes share one SQLite write transaction so another
        process cannot correct or retire a record between selection and merge.
        No duplicate adds confidence or counts as independent evidence.
        """
        now = _now()
        merged = 0
        with self._repo._lock, self._repo._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM memory_records WHERE kind='observation' AND status='active' "
                "AND valid_from<=? AND (valid_until='' OR valid_until>?) "
                "ORDER BY created_at DESC, id DESC LIMIT 500", (now, now),
            ).fetchall()
            keepers: dict[tuple, MemoryRecord] = {}
            merged_ids: dict[str, list[str]] = {}
            # Maintain a bounded recent window; the oldest selected copy wins.
            for row in reversed(rows):
                record = self._repo._from_row(row)
                if record.metadata.get("pinned") or record.metadata.get("conflict_state") == "pending":
                    continue
                key = _duplicate_key(record)
                keeper = keepers.get(key)
                if keeper is None:
                    keepers[key] = record
                    continue
                metadata = {**record.metadata, "consolidation": "merged_exact_duplicate",
                            "merged_into": keeper.record_id}
                db.execute("UPDATE memory_records SET status='superseded', metadata_json=? WHERE id=?",
                           (json.dumps(metadata, ensure_ascii=False, sort_keys=True), record.record_id))
                merged_ids.setdefault(keeper.record_id, []).append(record.record_id)
                merged += 1
            for keeper in keepers.values():
                ids = merged_ids.get(keeper.record_id)
                if not ids:
                    continue
                metadata = dict(keeper.metadata)
                metadata["consolidation"] = "exact_duplicate_keeper"
                metadata["merged_record_ids"] = list(dict.fromkeys(
                    [*(metadata.get("merged_record_ids") or []), *ids]))[-500:]
                db.execute("UPDATE memory_records SET metadata_json=? WHERE id=?",
                           (json.dumps(metadata, ensure_ascii=False, sort_keys=True), keeper.record_id))
        return merged

    def consolidate_episodes(self, *, min_age_days: float = 7) -> int:
        """Compatibility no-op: episodes require explicit semantic review."""
        return 0

    def run_all(self, *, min_episode_age_days: float = 7) -> dict[str, int]:
        return {"expired": self.expire_stale(),
                "merged_observations": self.merge_duplicate_observations(),
                "extracted_observations": 0}
