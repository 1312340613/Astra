"""Relevance metrics for the Context Index benchmark — pure functions only.

No IO, no model calls, no source imports beyond stdlib: every number in the
report must be reproducible from stored labels alone.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Any, Mapping, Sequence

# Judge labels are graded 0..2; gains follow a mild exponential preference for
# top placement (0, 1, 3) as fixed in the design doc.
_GAINS = (0, 1, 3)


@dataclass(frozen=True)
class RowLabel:
    """One displayed panel row paired with its judged relevance label."""

    source: str
    slot: str
    reason: str
    position: int  # 1-based position within the rendered panel
    label: int     # 0 irrelevant, 1 marginal, 2 genuinely useful
    case_id: str = ""


def _gain(label: int) -> float:
    return float(_GAINS[label]) if 0 <= label <= 2 else 0.0


def _dcg(labels: Sequence[int]) -> float:
    return sum(_gain(label) / math.log2(index + 1) for index, label in enumerate(labels, start=1))


def _window(rows: Sequence[RowLabel], k: int) -> list[RowLabel]:
    inside = sorted((row for row in rows if 1 <= row.position <= k), key=lambda row: row.position)
    return inside


def ndcg_at_k(rows: Sequence[RowLabel], k: int = 6) -> float:
    """NDCG@k over displayed positions; an all-irrelevant panel scores 0.0."""

    labels = [row.label for row in _window(rows, k)]
    if not labels:
        return 0.0
    actual = _dcg(labels)
    ideal = _dcg(sorted(labels, reverse=True))
    if ideal <= 0.0:
        return 0.0
    return actual / ideal


def precision_at_k(rows: Sequence[RowLabel], k: int = 2, *, threshold: int = 1) -> float:
    """Share of the first k panel slots whose label reaches the threshold."""

    inside = _window(rows, k)
    if not inside:
        return 0.0
    return sum(1 for row in inside if row.label >= threshold) / len(inside)


def _group_stats(rows: Sequence[RowLabel]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "mean_label": round(mean(row.label for row in rows), 4) if rows else 0.0,
        "relevant_rate": round(sum(1 for row in rows if row.label >= 1) / len(rows), 4) if rows else 0.0,
    }


def summarize(cases: Mapping[str, Sequence[RowLabel]]) -> dict[str, Any]:
    """Aggregate per-case panels into the report shape fixed by the design doc."""

    case_ids = sorted(cases)
    ndcg_values = [ndcg_at_k(cases[case_id], k=6) for case_id in case_ids]
    precision_values = [precision_at_k(cases[case_id], k=2) for case_id in case_ids]
    zero_hits = [1 for case_id in case_ids if not any(row.label >= 1 for row in cases[case_id])]

    by_reason: dict[str, list[RowLabel]] = defaultdict(list)
    by_slot: dict[str, list[RowLabel]] = defaultdict(list)
    by_source: dict[str, list[RowLabel]] = defaultdict(list)
    for case_id in case_ids:
        for row in cases[case_id]:
            by_reason[row.reason].append(row)
            by_slot[row.slot].append(row)
            by_source[row.source].append(row)

    return {
        "case_count": len(case_ids),
        "row_count": sum(len(cases[case_id]) for case_id in case_ids),
        "ndcg_at_6": round(mean(ndcg_values), 4) if ndcg_values else 0.0,
        "precision_at_2": round(mean(precision_values), 4) if precision_values else 0.0,
        "zero_hit_rate": round(len(zero_hits) / len(case_ids), 4) if case_ids else 0.0,
        "by_reason": {reason: _group_stats(rows) for reason, rows in sorted(by_reason.items())},
        "by_slot": {slot: _group_stats(rows) for slot, rows in sorted(by_slot.items())},
        "by_source": {source: _group_stats(rows) for source, rows in sorted(by_source.items())},
    }
