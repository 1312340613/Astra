"""M1: recorder contract tests (golden synthetic fixtures, never real events).

阶段 3（docs/activity-history.md#activity-summary-catch-up-and-health）：Astra 原生
记录器必须逐字节兼容 CUAService 的桶格式，让现有 sync/store/FTS/向量全链路零改动。
本测试固化的形状来自 2026-09-05 对实桶的解剖（dissect_cua.py），文本内容全为合成。
"""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.runtime import activity_sync
from agent.runtime.activity_recorder import contract


def make_event(kind: str, event_id: int, **body) -> dict:
    event = {
        "id": event_id,
        "kind": kind,
        "timestamp": "2026-09-05T02:00:00Z",
        "app": {"bundleIdentifier": "com.apple.Terminal", "name": "终端"},
        "window": {"title": "example — astra"},
    }
    event.update(body)
    return event


GOLDEN: dict[str, dict] = {
    "selection.changed": make_event(
        "selection.changed", 1,
        selection={
            "selectedItems": [{"description": "alpha", "role": "AXButton", "title": "alpha"}],
            "target": {"identifier": "CandidateList", "role": "AXList", "title": "列表"},
        },
    ),
    "keyboard.text_input": make_event(
        "keyboard.text_input", 2,
        keyboard={"target": {"description": "Shell", "role": "AXTextArea", "value": "hello world"}},
    ),
    "keyboard.shortcut": make_event(
        "keyboard.shortcut", 3,
        keyboard={"keyEquivalent": "delete", "target": {"description": "Shell", "role": "AXTextArea", "title": "Shell"}},
    ),
    "mouse.click": make_event(
        "mouse.click", 4,
        mouse={"button": "left", "target": {"description": "Finder", "role": "AXMenuBarItem", "subrole": "AXMenuExtra"}},
    ),
    "window.changed": make_event(
        "window.changed", 5,
        ax={"mode": "fullTree", "text": "0 标准窗口 ChatGPT\n\t1 container\n"},
    ),
    "session.started": make_event("session.started", 6),
    "session.ended": make_event("session.ended", 7),
}


class TestValidateEvent:
    def test_golden_shapes_pass(self) -> None:
        for kind, payload in GOLDEN.items():
            assert contract.validate_event(payload) == [], kind

    def test_all_golden_kinds_covered(self) -> None:
        assert set(GOLDEN) == contract.EVENT_KINDS

    @pytest.mark.parametrize(
        "patch",
        [
            {"id": 0},
            {"kind": "screen.capture"},
            {"timestamp": "2026-09-05 02:00"},
            {"app": "Terminal"},
            {"app": {"name": "no bundle id"}},
        ],
    )
    def test_malformed_fail(self, patch) -> None:
        payload = {**GOLDEN["mouse.click"], **{"id": 4}, **patch}
        payload.update(patch)
        assert contract.validate_event(payload) != []

    def test_missing_kind_body_fails(self) -> None:
        assert contract.validate_event(make_event("mouse.click", 9)) != []  # 无 mouse 体
        assert contract.validate_event(make_event("window.changed", 10)) != []  # 无 ax 体


class TestNormalizeCompatibility:
    def test_every_golden_event_survives_production_normalize(self) -> None:
        for kind, payload in GOLDEN.items():
            event = activity_sync.normalize_event("2026-09-05T02-00-00Z", payload)
            assert event is not None, kind
            assert event.kind == kind


class TestBucketRoundtrip:
    def test_synthetic_bucket_imports_through_existing_sync(self, tmp_path: Path) -> None:
        from agent.runtime.activity_store import ActivityStore

        event_root = tmp_path / "history"
        bucket = event_root / "segments" / "2026-09-05T02-00-00Z"
        bucket.mkdir(parents=True)
        lines = GOLDEN.values()
        (bucket / "events.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in lines), encoding="utf-8"
        )
        contract.write_metadata(bucket, started=datetime(2026, 9, 5, 2, 0, tzinfo=UTC), events=len(list(lines)), suppressed=1)

        roots = activity_sync.SourceRoots(event_root, tmp_path / "summaries")
        store = ActivityStore(tmp_path / "activity.sqlite3")
        report = activity_sync.ActivitySynchronizer(
            store, roots, tmp_path / "lock"
        ).sync_events()
        if activity_sync.fcntl is None:
            # Windows records directly through the canonical store; the Mac
            # archive importer intentionally requires its POSIX locking backend.
            assert report.error == "unsupported_platform"
            store.close()
            return
        assert report.status == "ok", report.error
        assert report.events_imported == 7
