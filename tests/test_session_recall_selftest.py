from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime import session_recall

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = [("-m", "agent.runtime.session_recall")]


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("configured", ["explicit", "blank", "unset"])
def test_selftest_never_reads_or_changes_user_archives(tmp_path, entry_point, configured):
    state = tmp_path / "user-state"
    explicit = tmp_path / "external" / "sessions.db"
    default = state / "sessions.db"
    for path in (explicit, default):
        recall = session_recall.SessionRecall(path)
        try:
            recall.init_db()
            sid = recall.create_session(title="untouched-user-session")
            recall.log_message(sid, "user", "USER_ARCHIVE_SENTINEL")
        finally:
            recall.close()
    before = {path: path.read_bytes() for path in (explicit, default)}
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    env = {
        **os.environ, "PYTHONPATH": str(ROOT), "ASTRA_HOME": str(state),
        "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
    }
    if configured == "explicit":
        env["ASTRA_SESSION_RECALL_DB"] = str(explicit)
    elif configured == "blank":
        env["ASTRA_SESSION_RECALL_DB"] = "   "
    else:
        env.pop("ASTRA_SESSION_RECALL_DB", None)
    result = subprocess.run(
        [sys.executable, *entry_point], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic" in result.stdout
    assert "USER_ARCHIVE_SENTINEL" not in result.stdout
    assert "untouched-user-session" not in result.stdout
    for path, content in before.items():
        assert path.read_bytes() == content
        assert set(path.parent.iterdir()) == {path}
    assert list(temporary.iterdir()) == []


def test_selftest_closes_and_removes_temporary_archive_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(session_recall.tempfile, "tempdir", str(tmp_path))
    opened = []

    def fail_on_write(self, *args, **kwargs):
        opened.append((self._db_path, self._get_conn()))
        raise RuntimeError("fixture write failure")

    monkeypatch.setattr(session_recall.SessionRecall, "log_message", fail_on_write)
    with pytest.raises(RuntimeError, match="fixture write failure"):
        session_recall._test()
    assert len(opened) == 1
    database, connection = opened[0]
    assert not database.parent.exists()
    with pytest.raises(session_recall.sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
