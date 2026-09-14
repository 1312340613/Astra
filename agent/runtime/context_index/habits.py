"""Ephemeral local-time habits inferred from cached Activity events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent.runtime.activity_store import _bounded_payload, _redact_text, sanitize_url

from .models import EvidenceResult, RecommendationCandidate, SourceLocator
from .workspace import WorkspaceIdentity

_HORIZON = timedelta(weeks=6)
_MIN_SUPPORT_DAYS = 3
_MAX_CACHE_ENTRIES = 32
_MAX_DATES = 5
_MAX_EXAMPLES = 3
_MAX_EVIDENCE_ITEM_CHARS = 500
_MAX_TITLE_TOKENS = 6
_MAX_TITLE_SIGNATURE_CHARS = 96
_MAX_EVENTS_PER_BUCKET = 16


@dataclass(frozen=True)
class _Example:
    timestamp: datetime
    local_date: date
    kind: str
    app_name: str
    window_title: str
    url: str
    order_key: tuple[str, int]


@dataclass
class _Group:
    key: tuple[tuple[str, str], ...]
    display_parts: tuple[str, ...]
    workspace_tier: int
    best_row_key: tuple[Any, ...]
    dates: set[date] = field(default_factory=set)
    weeks: set[tuple[int, int]] = field(default_factory=set)
    latest: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    examples: list[_Example] = field(default_factory=list)


@dataclass(frozen=True)
class _Aggregate:
    cache_key: CacheKey
    locator_id: str
    identity: str
    topic_key: str
    description: str
    workspace_tier: int
    project_label: str
    support_days: int
    support_weeks: int
    latest: datetime
    dates: tuple[date, ...]
    examples: tuple[_Example, ...]


CacheKey = tuple[str, Hashable, str, int, int, float | None]


@dataclass(frozen=True)
class _CacheEntry:
    valid_through: datetime | None
    aggregate: _Aggregate | None


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(_redact_text(str(value or "")).split())[:limit]


def _normalized_tokens(value: Any) -> str:
    folded = str(value or "").casefold()
    return " ".join("".join(character if character.isalnum() else " " for character in folded).split())


def _workspace_fts_queries(workspace: WorkspaceIdentity) -> tuple[str, ...]:
    terms = [term for term in _normalized_tokens(workspace.label).split() if len(term) >= 3]
    # _workspace_tier treats any meaningful label token as inferred affinity.
    # Query each token separately so a broad global stream cannot suppress a
    # later inferred row and so supplements preserve that exact semantics.
    return tuple(f'"{term}"' for term in dict.fromkeys(terms))


def _title_signature(value: Any) -> str:
    tokens = _normalized_tokens(_bounded_text(value, 240)).split()
    return " ".join(tokens[:_MAX_TITLE_TOKENS])[:_MAX_TITLE_SIGNATURE_CHARS]


def _domain(value: Any) -> str:
    try:
        return (urlsplit(sanitize_url(str(value or ""))).hostname or "").casefold()[:120]
    except ValueError:
        return ""


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _database_identity(connection: sqlite3.Connection) -> str:
    for row in connection.execute("PRAGMA database_list"):
        if str(row[1]) != "main":
            continue
        filename = str(row[2] or "")
        if filename:
            return str(Path(filename).resolve())
    return f":memory:{id(connection)}"


def _digest(*values: Any) -> str:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _workspace_tier(workspace: WorkspaceIdentity, *values: Any) -> int:
    label = _normalized_tokens(workspace.label)
    if not label:
        return 2
    searchable = f" {_normalized_tokens(' '.join(str(value or '') for value in values))} "
    if f" {label} " in searchable:
        return 0
    meaningful = [token for token in label.split() if len(token) >= 3]
    if any(f" {token} " in searchable for token in meaningful):
        return 1
    return 2


def _now_local(value: float | datetime, zone: tzinfo | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=zone).astimezone() if zone is None else value.replace(tzinfo=zone)
        return value.astimezone(zone)
    instant = datetime.fromtimestamp(float(value), tz=UTC)
    return instant.astimezone(zone)


def _localize(value: datetime, zone: tzinfo | None) -> datetime:
    return value.astimezone(zone)


def _timestamp_microseconds(value: datetime) -> int:
    """Return an integer UTC instant without relying on SQLite date coercion."""

    instant = value.astimezone(UTC)
    return int(instant.timestamp() * 1_000_000)


def _bucket_ranges(local_now: datetime) -> tuple[tuple[int, int], ...]:
    """Return UTC ranges for the matching local weekday/two-hour buckets.

    Filtering each eligible local bucket before materializing rows keeps the
    virtual six-week aggregate bounded by the Activity Store's occurred-at
    index.  The final Python check below remains authoritative for offsets and
    DST transitions.
    """

    horizon_start = local_now.astimezone(UTC) - _HORIZON
    first_day = horizon_start.astimezone(local_now.tzinfo).date()
    last_day = local_now.date()
    bucket_hour = (local_now.hour // 2) * 2
    ranges: list[tuple[int, int]] = []
    day = first_day
    while day <= last_day:
        if day.weekday() == local_now.weekday():
            start = local_now.replace(
                year=day.year,
                month=day.month,
                day=day.day,
                hour=bucket_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            end = start + timedelta(hours=2)
            ranges.append((_timestamp_microseconds(start), _timestamp_microseconds(end)))
        day += timedelta(days=1)
    return tuple(ranges)


def _event_payload(example: _Example) -> dict[str, Any]:
    return {
        "evidence_type": "raw_event",
        "source": "computer_history",
        "untrusted_observation": True,
        "occurred_at": example.timestamp.isoformat(),
        "kind": example.kind,
        "app_name": example.app_name,
        "window_title": example.window_title,
        "url": example.url,
    }


def _evidence_item(payload: dict[str, Any]) -> str:
    bounded = _bounded_payload(payload, _MAX_EVIDENCE_ITEM_CHARS)
    return json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))


class HabitAggregator:
    """Compute and cache virtual habits without creating durable user state."""

    def __init__(self, local_zone: tzinfo | None = None):
        self._local_zone = local_zone
        self._cache: OrderedDict[CacheKey, _CacheEntry] = OrderedDict()

    def candidate(
        self,
        connection: sqlite3.Connection,
        workspace: WorkspaceIdentity,
        now: float | datetime,
        data_version: Hashable,
        *, as_of: float | None = None,
    ) -> RecommendationCandidate | None:
        local_now = _now_local(now, self._local_zone)
        database_identity = _database_identity(connection)
        cache_key = (
            database_identity,
            data_version,
            workspace.key,
            local_now.weekday(),
            local_now.hour // 2,
            as_of,
        )
        self._invalidate_database_version(database_identity, data_version)
        event_columns = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in connection.execute("PRAGMA table_info(activity_events)")
        }
        if "occurred_at_us" not in event_columns:
            return None
        if cache_key in self._cache:
            entry = self._cache.pop(cache_key)
            now_utc = local_now.astimezone(UTC)
            if entry.valid_through is None or now_utc <= entry.valid_through:
                self._cache[cache_key] = entry
                aggregate = entry.aggregate
            else:
                aggregate, valid_through = self._aggregate(connection, workspace, local_now, cache_key)
                self._cache[cache_key] = _CacheEntry(valid_through, aggregate)
        else:
            aggregate, valid_through = self._aggregate(connection, workspace, local_now, cache_key)
            self._cache[cache_key] = _CacheEntry(valid_through, aggregate)
            while len(self._cache) > _MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        if aggregate is None:
            return None
        return RecommendationCandidate(
            source="habit",
            identity=aggregate.identity,
            description=aggregate.description,
            timestamp=aggregate.latest.timestamp(),
            workspace_tier=aggregate.workspace_tier,
            native_query_rank=0.0,
            topic_key=aggregate.topic_key,
            trust_label="inferred_pattern",
            locator=SourceLocator("habit", aggregate.locator_id),
            project_label=aggregate.project_label,
            support_days=aggregate.support_days,
            support_weeks=aggregate.support_weeks,
            slot="A3",
            reason=(
                f"inferred from {aggregate.support_days} distinct local dates across {aggregate.support_weeks} weeks"
            ),
        )

    def _invalidate_database_version(self, database_identity: str, data_version: Hashable) -> None:
        stale = [key for key in self._cache if key[0] == database_identity and key[1] != data_version]
        for key in stale:
            del self._cache[key]

    def _aggregate(
        self,
        connection: sqlite3.Connection,
        workspace: WorkspaceIdentity,
        local_now: datetime,
        cache_key: CacheKey,
    ) -> tuple[_Aggregate | None, datetime | None]:
        now_utc = local_now.astimezone(UTC)
        horizon_start = now_utc - _HORIZON
        ranges = _bucket_ranges(local_now)
        if not ranges:
            return None, None
        rows_by_identity: dict[tuple[str, int], sqlite3.Row] = {}
        workspace_queries = _workspace_fts_queries(workspace)
        as_of = cache_key[-1]
        cutoff_args = () if as_of is None else (int(as_of * 1_000_000), as_of)
        cutoff_sql = "" if as_of is None else (
            " AND occurred_at_us <= ? AND context_activity_timestamp(imported_at) <= ? "
        )
        workspace_cutoff_sql = "" if as_of is None else (
            " AND e.occurred_at_us <= ? AND context_activity_timestamp(e.imported_at) <= ? "
        )
        for start, end in ranges:
            # Sample each eligible local bucket independently so one busy day
            # cannot consume the six-week evidence budget. The canonical cache
            # stays untouched; this is a virtual, bounded recommendation view.
            query_ranges: tuple[tuple[str, tuple[object, ...], str], ...] = (
                ("occurred_at_us >= ? AND occurred_at_us < ?", (start, end), "occurred_at_us DESC, rowid DESC"),
            )
            for time_predicate, time_args, order in query_ranges:
                generic = connection.execute(
                    """SELECT segment_id, event_id, occurred_at, kind, app_name,
                          window_title, url, url_search_text, searchable_text
                   FROM activity_events
                   WHERE """ + time_predicate + cutoff_sql + """
                   ORDER BY """ + order + """
                   LIMIT ?""",
                    (*time_args, *cutoff_args, _MAX_EVENTS_PER_BUCKET),
                ).fetchall()
                for row in generic:
                    rows_by_identity[(str(row[0]), int(row[1]))] = row
                for workspace_query in workspace_queries:
                    workspace_predicate = "e.occurred_at_us >= ? AND e.occurred_at_us < ?"
                    workspace_order = "e.occurred_at_us DESC, e.rowid DESC"
                    workspace_rows = connection.execute(
                    """SELECT e.segment_id, e.event_id, e.occurred_at, e.kind, e.app_name,
                              e.window_title, e.url, e.url_search_text, e.searchable_text
                       FROM activity_events_fts
                       JOIN activity_events AS e ON e.rowid = activity_events_fts.rowid
                       WHERE activity_events_fts MATCH ?
                         AND """ + workspace_predicate + workspace_cutoff_sql + """
                       ORDER BY """ + workspace_order + """
                       LIMIT ?""",
                        (workspace_query, *time_args, *cutoff_args, _MAX_EVENTS_PER_BUCKET),
                    ).fetchall()
                    for row in workspace_rows:
                        rows_by_identity[(str(row[0]), int(row[1]))] = row
        groups: dict[tuple[tuple[str, str], ...], _Group] = {}
        valid_through: datetime | None = None
        for row in rows_by_identity.values():
            (
                segment_id,
                event_id,
                occurred_at,
                kind,
                raw_app_name,
                raw_window_title,
                raw_url,
                url_search_text,
                searchable_text,
            ) = row
            timestamp = _parse_timestamp(occurred_at)
            if timestamp is None or not horizon_start <= timestamp <= now_utc:
                continue
            local = _localize(timestamp, self._local_zone)
            if local.weekday() != local_now.weekday() or local.hour // 2 != local_now.hour // 2:
                continue
            expires_at = timestamp + _HORIZON
            valid_through = expires_at if valid_through is None else min(valid_through, expires_at)
            app_name = _bounded_text(raw_app_name, 80)
            title = _bounded_text(raw_window_title, 160)
            title_signature = _title_signature(title)
            domain = _domain(raw_url)
            tier = _workspace_tier(
                workspace,
                app_name,
                title_signature,
                domain,
                _bounded_text(url_search_text, 180),
                _bounded_text(searchable_text, 180),
            )
            workspace_label = _bounded_text(workspace.label, 80) if tier < 2 else ""
            key_parts = tuple(
                (name, normalized)
                for name, value in (
                    ("workspace", workspace_label),
                    ("app", app_name),
                    ("domain", domain),
                    ("title", title_signature),
                )
                if (normalized := _normalized_tokens(value))
            )
            if not any(name != "workspace" for name, _value in key_parts):
                continue
            display_parts = tuple(value for value in (workspace_label, app_name, domain, title_signature) if value)
            best_row_key = (
                tier,
                tuple((part.casefold(), part) for part in display_parts),
            )
            group = groups.get(key_parts)
            if group is None:
                group = _Group(
                    key=key_parts,
                    display_parts=display_parts,
                    workspace_tier=tier,
                    best_row_key=best_row_key,
                )
                groups[key_parts] = group
            elif best_row_key < group.best_row_key:
                group.display_parts = display_parts
                group.workspace_tier = tier
                group.best_row_key = best_row_key
            group.dates.add(local.date())
            iso_year, iso_week, _weekday = local.date().isocalendar()
            group.weeks.add((iso_year, iso_week))
            group.latest = max(group.latest, timestamp)
            group.examples.append(
                _Example(
                    timestamp=timestamp,
                    local_date=local.date(),
                    kind=_bounded_text(kind, 40),
                    app_name=app_name,
                    window_title=title,
                    url=sanitize_url(str(raw_url or "")),
                    order_key=(str(segment_id), int(event_id)),
                )
            )
        eligible = [group for group in groups.values() if len(group.dates) >= _MIN_SUPPORT_DAYS]
        if not eligible:
            return None, valid_through
        eligible.sort(
            key=lambda group: (
                group.workspace_tier,
                -len(group.dates),
                -len(group.weeks),
                -group.latest.timestamp(),
                group.key,
            )
        )
        best = eligible[0]
        safe_label = " · ".join(best.display_parts)[:180] or "cached activity"
        description = (f"Inferred pattern. Around this time, activity often shifts to {safe_label}.")[:240]
        locator_id = _digest(cache_key, best.key)
        examples = tuple(
            sorted(
                best.examples,
                key=lambda example: (
                    -example.timestamp.timestamp(),
                    example.order_key,
                ),
            )[:_MAX_EXAMPLES]
        )
        return (
            _Aggregate(
                cache_key=cache_key,
                locator_id=locator_id,
                identity=_digest("habit", best.key),
                topic_key=_digest("habit-topic", best.key),
                description=description,
                workspace_tier=best.workspace_tier,
                project_label=workspace.label if best.workspace_tier < 2 else "",
                support_days=len(best.dates),
                support_weeks=len(best.weeks),
                latest=best.latest,
                dates=tuple(sorted(best.dates, reverse=True)),
                examples=examples,
            ),
            valid_through,
        )

    def invalidate_database(self, database_identity: str) -> None:
        stale = [key for key in self._cache if key[0] == database_identity]
        for key in stale:
            del self._cache[key]

    def open(
        self,
        locator: SourceLocator,
        *,
        database_identity: str | None = None,
        data_version: Hashable | None = None,
    ) -> EvidenceResult:
        empty = EvidenceResult(
            source="habit",
            trust_label="inferred_pattern",
            title="Inferred Activity habit unavailable",
            items=(),
        )
        if locator.kind != "habit" or not locator.primary or locator.secondary != 0:
            return empty
        aggregate = None
        matched_key = None
        for cache_key, entry in reversed(self._cache.items()):
            if entry.aggregate is None or entry.aggregate.locator_id != locator.primary:
                continue
            matched_key = cache_key
            aggregate = entry.aggregate
            break
        if aggregate is None:
            return empty
        if database_identity is not None and (
            aggregate.cache_key[0] != database_identity or aggregate.cache_key[1] != data_version
        ):
            if matched_key is not None:
                del self._cache[matched_key]
            return empty
        support = {
            "evidence_type": "habit_support",
            "source": "computer_history",
            "inferred_pattern": True,
            "support_days": aggregate.support_days,
            "represented_weeks": aggregate.support_weeks,
            "example_local_dates": [value.isoformat() for value in aggregate.dates[:_MAX_DATES]],
        }
        return EvidenceResult(
            source="habit",
            trust_label="inferred_pattern",
            title="Inferred Activity habit evidence",
            items=(
                _evidence_item(support),
                *(_evidence_item(_event_payload(example)) for example in aggregate.examples),
            ),
        )
