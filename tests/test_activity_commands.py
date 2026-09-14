from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli import activity_commands
from agent.cli.activity_commands import execute_activity_command
from agent.runtime import activity_sync
from agent.runtime.activity_store import ActivityEvent, ActivityStore, SourceCursor
from agent.runtime.activity_sync import SourceDiscoveryError, SourceRoots


@pytest.fixture
def activity_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SourceRoots:
    event_root = tmp_path / "computer-history"
    summary_root = tmp_path / "summaries"
    event_root.mkdir()
    summary_root.mkdir()
    (event_root / "metadata.json").write_text("{}", encoding="utf-8")
    roots = SourceRoots(event_root=event_root, summary_root=summary_root)
    monkeypatch.setenv("ASTRA_ACTIVITY_DB", str(tmp_path / "activity.sqlite3"))
    monkeypatch.setattr(activity_commands, "resolve_source_roots", lambda: roots)
    monkeypatch.setattr(
        activity_commands,
        "launch_agent_status",
        lambda: {"scheduler_status": "unloaded"},
    )
    monkeypatch.setattr(
        activity_commands,
        "install_launch_agent",
        lambda **_kwargs: {"scheduler_status": "loaded", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(
        activity_commands,
        "uninstall_launch_agent",
        lambda: {"scheduler_status": "unloaded", "plist_path": "/tmp/test.plist"},
    )
    return roots


def _run_command(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = execute_activity_command(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_activity_status_does_not_require_model_api_key(tmp_path: Path):
    env = os.environ.copy()
    env.update(
        {
            "ASTRA_ACTIVITY_DB": str(tmp_path / "activity.sqlite3"),
            "ASTRA_COMPUTER_HISTORY_ROOT": str(tmp_path / "missing-source"),
        }
    )
    env.pop("DEEPSEEK_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-m", "agent.cli.main", "activity", "status"],
        cwd=Path(__file__).parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["source_status"] == "unavailable"
    assert payload["source_override"] == "ASTRA_COMPUTER_HISTORY_ROOT"
    assert "API_KEY" not in completed.stderr


def test_scheduled_sync_noops_before_store_open_when_inactive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    roots = SourceRoots(tmp_path / "events", tmp_path / "summaries")
    opened: list[bool] = []
    monkeypatch.setattr(activity_commands, "resolve_source_roots", lambda: roots)
    monkeypatch.setattr(activity_commands, "source_is_recent", lambda _roots: False)
    monkeypatch.setenv("ASTRA_ACTIVITY_LOG_DIR", str(tmp_path / "logs"))

    code = execute_activity_command(
        ["sync", "--scheduled"],
        store_factory=lambda: opened.append(True),  # type: ignore[arg-type]
    )

    assert code == 0
    assert opened == []
    assert capsys.readouterr().out == ""


def test_scheduled_sync_noops_before_store_open_when_source_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    opened: list[bool] = []
    monkeypatch.setattr(
        activity_commands,
        "resolve_source_roots",
        lambda: (_ for _ in ()).throw(SourceDiscoveryError("fixture secret")),
    )
    monkeypatch.setenv("ASTRA_ACTIVITY_LOG_DIR", str(tmp_path / "logs"))

    code = execute_activity_command(
        ["sync", "--scheduled"],
        store_factory=lambda: opened.append(True),  # type: ignore[arg-type]
    )

    assert code == 0
    assert opened == []
    assert capsys.readouterr().out == ""


def test_scheduled_sync_logs_missing_explicit_root_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    roots = SourceRoots(tmp_path / "missing-events", tmp_path / "summaries")
    reports: list[activity_commands.SyncReport] = []
    monkeypatch.setattr(activity_commands, "resolve_source_roots", lambda: roots)
    monkeypatch.setattr(activity_commands, "log_scheduled_report", lambda _logger, report: reports.append(report))
    monkeypatch.setenv("ASTRA_ACTIVITY_LOG_DIR", str(tmp_path / "logs"))

    code = execute_activity_command(
        ["sync", "--scheduled"],
        store_factory=lambda: pytest.fail("scheduled source gate opened the database"),
    )

    assert code == 0
    assert [(report.status, report.error) for report in reports] == [("source_unavailable", "event_root_unavailable")]
    assert capsys.readouterr().out == ""


def test_clear_requires_explicit_scope(capsys: pytest.CaptureFixture[str]):
    assert execute_activity_command(["clear"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "action": "clear",
        "status": "error",
        "error_type": "ArgumentError",
    }
    assert captured.out.count("\n") == 1
    assert captured.err == ""


@pytest.mark.parametrize(
    ("argv", "action"),
    [
        (["sync"], "sync"),
        (["status"], "status"),
        (["install"], "install"),
        (["uninstall"], "uninstall"),
        (["rebuild-index"], "rebuild-index"),
        (["clear", "--before", "2026-08-26T07:00:00Z"], "clear"),
        (["clear", "--all"], "clear"),
    ],
)
def test_activity_commands_emit_one_json_document(
    activity_environment: SourceRoots,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    action: str,
):
    code, stdout, _stderr = _run_command(capsys, argv)
    assert code == 0
    payload = json.loads(stdout)
    assert payload["action"] == action
    assert stdout.count("\n") == 1
    assert len(stdout) <= 12_000


def test_uninstall_preserves_local_database(
    activity_environment: SourceRoots, capsys: pytest.CaptureFixture[str]
):
    database = Path(os.environ["ASTRA_ACTIVITY_DB"])
    with ActivityStore(database):
        pass

    code, stdout, _stderr = _run_command(capsys, ["uninstall"])

    assert code == 0
    assert json.loads(stdout)["status"] == "ok"
    assert database.exists()


def test_manual_sync_forces_scan(
    activity_environment: SourceRoots,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    calls: list[bool] = []

    class FakeSynchronizer:
        def __init__(self, _store, _roots, _lock_path):
            pass

        def sync(self, force: bool = False):
            calls.append(force)
            return activity_commands.SyncReport(status="ok")

    monkeypatch.setattr(activity_commands, "ActivitySynchronizer", FakeSynchronizer)
    assert execute_activity_command(["sync"]) == 0
    assert calls == [True]
    capsys.readouterr()


def test_status_never_contains_activity_text(activity_environment: SourceRoots, capsys: pytest.CaptureFixture[str]):
    secret = "FIXTURE WINDOW TITLE https://example.test/?token=secret"
    private_error_text = "PrivateWindowTitle"
    with ActivityStore() as store:
        store.add_event_batch(
            [
                ActivityEvent(
                    segment_id="segment",
                    event_id=1,
                    occurred_at="2026-08-26T07:00:00Z",
                    kind="window",
                    app_name="Fixture",
                    bundle_id="test.fixture",
                    window_title=secret,
                    url="https://example.test/?token=secret",
                    selection_text=secret,
                    searchable_text=secret,
                    raw_json=json.dumps({"content": secret}),
                    imported_at="2026-08-26T07:00:00Z",
                )
            ],
            SourceCursor(
                source_path="/tmp/events.jsonl",
                source_kind="events",
                source_identity="1:1",
                byte_offset=1,
                observed_size=1,
                observed_mtime_ns=1,
                last_success_at="2026-08-26T07:00:00Z",
                last_error=private_error_text,
            ),
        )

    code, stdout, _stderr = _run_command(capsys, ["status"])
    payload = json.loads(stdout)
    assert code == 0
    assert payload["counts"]["events"] == 1
    assert secret not in stdout
    assert private_error_text not in stdout
    assert payload["last_error_type"] == "sync_error"
    assert "window_title" not in stdout
    assert "raw_json" not in stdout


def test_integrity_failure_returns_one_with_error_type_only(
    activity_environment: SourceRoots, capsys: pytest.CaptureFixture[str]
):
    class BrokenStore:
        path = Path("/tmp/activity.sqlite3")

        def rebuild_fts(self):
            raise sqlite3.DatabaseError("FIXTURE PRIVATE CONTENT")

        def close(self):
            pass

    code = execute_activity_command(["rebuild-index"], store_factory=BrokenStore)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1
    assert payload == {
        "action": "rebuild-index",
        "status": "error",
        "error_type": "DatabaseError",
    }
    assert "FIXTURE PRIVATE CONTENT" not in captured.out


def test_status_reports_damaged_fts_and_exits_one(
    activity_environment: SourceRoots, capsys: pytest.CaptureFixture[str]
):
    store = ActivityStore()
    store.connection.execute("DELETE FROM activity_events_fts")
    store.connection.execute(
        """INSERT INTO activity_events(
               segment_id, event_id, occurred_at, kind, app_name, bundle_id,
               window_title, url, url_search_text, selection_text, searchable_text,
               raw_json, imported_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "segment",
            1,
            "2026-08-26T07:00:00Z",
            "window",
            "Fixture",
            "test.fixture",
            "PrivateWindowTitle",
            "",
            "",
            "",
            "PrivateWindowTitle",
            "{}",
            "2026-08-26T07:00:00Z",
        ),
    )
    store.connection.commit()
    store.connection.execute("DELETE FROM activity_events_fts")
    store.connection.commit()

    code = execute_activity_command(["status"], store_factory=lambda: store)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1
    assert payload["action"] == "status"
    assert payload["status"] == "error"
    assert payload["integrity_status"] == "ok"
    assert payload["fts_status"] == "error"
    assert captured.out.count("\n") == 1
    assert "PrivateWindowTitle" not in captured.out


def test_status_exits_one_when_canonical_integrity_reports_error(
    activity_environment: SourceRoots,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    store = ActivityStore()
    monkeypatch.setattr(
        store,
        "integrity_status",
        lambda: {"integrity_status": "error", "fts_status": "ok"},
    )

    code = execute_activity_command(["status"], store_factory=lambda: store)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1
    assert payload["status"] == "error"
    assert payload["integrity_status"] == "error"
    assert payload["fts_status"] == "ok"
    assert captured.out.count("\n") == 1


@pytest.mark.parametrize("scheduled", (False, True))
def test_sync_integrity_failure_exits_one_with_metadata_only_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scheduled: bool,
):
    lock_operations = []
    monkeypatch.setattr(activity_sync, "fcntl", SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, LOCK_UN=4,
        flock=lambda _fd, operation: lock_operations.append(operation),
    ))
    roots = SourceRoots(tmp_path / "events", tmp_path / "summaries")
    roots.event_root.mkdir()
    roots.summary_root.mkdir()
    monkeypatch.setattr(activity_commands, "resolve_source_roots", lambda: roots)
    monkeypatch.setattr(activity_commands, "source_is_recent", lambda _roots: True)
    monkeypatch.setenv("ASTRA_ACTIVITY_LOG_DIR", str(tmp_path / "logs"))

    class UnhealthyStore:
        path = tmp_path / "activity.sqlite3"

        def integrity_status(self):
            return {"integrity_status": "error", "fts_status": "ok"}

        def close(self):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"PRIVATE CONTENT write path reached: {name}")

    argv = ["sync", "--scheduled"] if scheduled else ["sync"]
    code = execute_activity_command(argv, store_factory=UnhealthyStore)
    captured = capsys.readouterr()

    assert code == 1
    assert lock_operations == [3, 4]
    if scheduled:
        assert captured.out == ""
        logged = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "logs").glob("*"))
        assert '"error_type":"integrity_error"' in logged
        assert "PRIVATE CONTENT" not in logged
    else:
        payload = json.loads(captured.out)
        assert payload["status"] == "integrity_error"
        assert payload["error_type"] == "integrity_error"
        assert "PRIVATE CONTENT" not in captured.out


def test_rebuild_index_remains_available_when_integrity_is_unhealthy(
    capsys: pytest.CaptureFixture[str],
):
    class RepairStore:
        path = Path("/tmp/activity.sqlite3")

        def integrity_status(self):
            pytest.fail("rebuild-index must not be integrity-gated")

        def rebuild_fts(self):
            return {"events": 2, "summaries": 1}

        def close(self):
            pass

    code = execute_activity_command(["rebuild-index"], store_factory=RepairStore)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload == {
        "action": "rebuild-index",
        "status": "ok",
        "counts": {"events": 2, "summaries": 1},
    }
