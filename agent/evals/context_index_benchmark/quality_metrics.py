"""Gold-document metrics, including omitted evidence and negative queries."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from statistics import mean
from collections.abc import Sequence


@dataclass(frozen=True)
class QualityObservation:
    case_id: str
    split: str
    kind: str
    expected: frozenset[str]
    displayed: tuple[str, ...]
    latency_ms: float = 0.0
    rendered_tokens: int = 0
    source_status: dict[str, str] = field(default_factory=dict)
    semantic_status: dict[str, str] = field(default_factory=dict)


def quality_metrics(rows: Sequence[QualityObservation], k: int = 4) -> dict[str, float | int]:
    positives = [row for row in rows if row.expected]
    negatives = [row for row in rows if not row.expected]
    recalls, ndcgs = [], []
    useful = displayed = 0
    for row in rows:
        top = row.displayed[:k]
        seen = set()
        hits = []
        for item in top:
            hits.append(item in row.expected and item not in seen)
            seen.add(item)
        useful += sum(hits)
        displayed += len(top)
        if row.expected:
            recalls.append(sum(hits) / len(row.expected))
            dcg = sum(hit / math.log2(rank + 2) for rank, hit in enumerate(hits))
            ideal = sum(1 / math.log2(rank + 2) for rank in range(min(k, len(row.expected))))
            ndcgs.append(dcg / ideal if ideal else 0.0)
    latencies = sorted(row.latency_ms for row in rows)
    return {
        "cases": len(rows), "positive_cases": len(positives), "negative_cases": len(negatives),
        "recall_at_4": round(mean(recalls), 4) if recalls else 0.0,
        "ndcg_at_4": round(mean(ndcgs), 4) if ndcgs else 0.0,
        "displayed_precision": round(useful / displayed, 4) if displayed else 0.0,
        "irrelevant_rows": displayed - useful,
        "empty_cases": sum(not row.displayed for row in rows),
        "source_failure_cases": sum(any(value in {"error", "timeout"} for value in row.source_status.values()) for row in rows),
        "semantic_failure_cases": sum(any(value in {"error", "timeout"} for value in row.semantic_status.values()) for row in rows),
        "semantic_cold_cases": sum("cold" in row.semantic_status.values() for row in rows),
        "negative_injection_rate": round(sum(bool(row.displayed) for row in negatives) / len(negatives), 4) if negatives else 0.0,
        "latency_p95_ms": round(latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)], 2) if latencies else 0.0,
        "mean_index_tokens": round(mean(row.rendered_tokens for row in rows), 2) if rows else 0.0,
    }
