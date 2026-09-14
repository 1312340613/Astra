import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from agent.mail163.models import AttachmentMeta, RemoteMessage
from agent.mail163.store import MailStore

ACCOUNT = "user@163.com"


def message(
    uid: int,
    *,
    folder: str = "INBOX",
    uidvalidity: int = 10,
    flags: tuple[str, ...] = (),
    subject: str = "Subject",
    sender: str = "sender@example.com",
    recipients: str = ACCOUNT,
    received_at: float | None = None,
    body_text: str | None = None,
    attachments: tuple[AttachmentMeta, ...] | None = None,
) -> RemoteMessage:
    return RemoteMessage(
        folder=folder,
        uidvalidity=uidvalidity,
        uid=uid,
        message_id=f"<{uid}@example>",
        sender=sender,
        recipients=recipients,
        subject=subject,
        sent_at="Thu, 21 Aug 2026 10:00:00 +0800",
        received_at=100.0 + uid if received_at is None else received_at,
        flags=flags,
        size=120,
        body_text=f"body {uid}" if body_text is None else body_text,
        body_truncated=False,
        attachments=(AttachmentMeta("2", "report.pdf", "application/pdf", 42),) if attachments is None else attachments,
        warnings=(),
    )


def test_schema_and_owner_only_permissions(tmp_path: Path) -> None:
    database = tmp_path / "private" / "mail.db"
    store = MailStore(database)

    assert store.exists()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone() == ("1",)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"schema_meta", "folders", "messages", "attachments"} <= tables
    assert {"idx_messages_recent", "idx_messages_subject"} <= indexes

    if os.name == "posix":
        assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_missing_read_only_cache_is_not_created(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "mail.db"

    store = MailStore(database, read_only=True)

    assert not store.exists()
    assert not database.parent.exists()


def test_folder_sync_is_idempotent_and_marks_remote_removal(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    assert store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1, 2}, [message(1), message(2)], {}, 100.0) == (2, 0, 0)
    assert store.apply_folder_sync(ACCOUNT, "INBOX", 10, {2}, [], {2: ("\\Seen",)}, 200.0) == (0, 1, 1)
    assert store.apply_folder_sync(ACCOUNT, "INBOX", 10, {2}, [], {2: ("\\Seen",)}, 300.0) == (0, 0, 0)

    rows = store.recent(ACCOUNT, folder="INBOX", limit=10, include_removed=True)
    assert [(row["uid"], row["remote_removed"]) for row in rows] == [(2, False), (1, True)]
    assert rows[0]["unread"] is False
    assert store.known_uids(ACCOUNT, "INBOX", 10) == {1, 2}
    with sqlite3.connect(tmp_path / "mail.db") as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_folder_sync_handles_remote_uid_sets_above_sqlite_bind_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = MailStore(tmp_path / "mail.db")
    initial = [message(uid, attachments=()) for uid in range(1, 151)]
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, set(range(1, 151)), initial, {}, 100.0)
    real_connect = store._connect

    def limited_connect() -> sqlite3.Connection:
        database = real_connect()
        database.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
        return database

    monkeypatch.setattr(store, "_connect", limited_connect)

    result = store.apply_folder_sync(
        ACCOUNT,
        "INBOX",
        10,
        set(range(51, 151)),
        [],
        {},
        200.0,
    )

    assert result == (0, 0, 50)
    rows = store.recent(ACCOUNT, folder="INBOX", limit=200, include_removed=True)
    assert {row["uid"] for row in rows if row["remote_removed"]} == set(range(1, 51))
    assert {row["uid"] for row in rows if not row["remote_removed"]} == set(range(51, 151))
    state = store.folder_state(ACCOUNT, "INBOX")
    assert state is not None and state.last_success == 200.0


def test_uidvalidity_change_retains_old_generation(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1}, [message(1)], {}, 100.0)
    replacement = replace(message(1), uidvalidity=11, subject="new generation")

    assert store.apply_folder_sync(ACCOUNT, "INBOX", 11, {1}, [replacement], {}, 200.0) == (1, 0, 1)

    rows = store.search(ACCOUNT, "generation", folder="INBOX", limit=10, include_removed=True)
    assert rows[0]["uidvalidity"] == 11
    assert store.known_uids(ACCOUNT, "INBOX", 10) == {1}
    assert store.known_uids(ACCOUNT, "INBOX", 11) == {1}
    assert store.folder_state(ACCOUNT, "INBOX").uidvalidity == 11  # type: ignore[union-attr]
    with sqlite3.connect(tmp_path / "mail.db") as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert db.execute("SELECT remote_removed FROM messages WHERE uidvalidity = 10").fetchone()[0] == 1


def test_folder_transaction_rolls_back_on_attachment_failure(monkeypatch, tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    real = store._replace_attachments
    monkeypatch.setattr(
        store,
        "_replace_attachments",
        lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )

    with pytest.raises(sqlite3.OperationalError, match="boom"):
        store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1}, [message(1)], {}, 100.0)

    monkeypatch.setattr(store, "_replace_attachments", real)
    assert store.exists()
    assert store.recent(ACCOUNT, folder="INBOX", limit=10) == []
    assert store.folder_state(ACCOUNT, "INBOX") is None


def test_search_escapes_like_metacharacters_and_orders_deterministically(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    messages = [
        message(1, subject="100% literal", received_at=500.0),
        message(2, subject="100x literal", received_at=500.0),
        message(3, subject="under_score", received_at=500.0),
        message(4, subject="underXscore", received_at=500.0),
    ]
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1, 2, 3, 4}, messages, {}, 100.0)

    assert [row["uid"] for row in store.search(ACCOUNT, "%", folder="INBOX", limit=10)] == [1]
    assert [row["uid"] for row in store.search(ACCOUNT, "_", folder="INBOX", limit=10)] == [3]
    assert [row["uid"] for row in store.recent(ACCOUNT, folder="INBOX", limit=10)] == [4, 3, 2, 1]


def test_search_combines_filters_and_applies_window_before_filtering(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    messages = [
        message(1, subject="needle", sender="alice@example.com", received_at=100.0),
        message(2, subject="needle", sender="bob@example.com", received_at=200.0),
        message(3, subject="noise", sender="alice@example.com", received_at=300.0),
    ]
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1, 2, 3}, messages, {}, 100.0)

    rows = store.search(ACCOUNT, "needle", sender="alice", folder="INBOX", limit=10)
    assert [row["uid"] for row in rows] == [1]
    assert store.search(ACCOUNT, "needle", sender="alice", folder="INBOX", limit=10, window=2) == []


def test_read_and_attachment_are_scoped_by_generation_uid_and_part(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    first = message(1, attachments=(AttachmentMeta("2", "old.pdf", "application/pdf", 42),))
    second = replace(
        first,
        uidvalidity=11,
        subject="replacement",
        attachments=(AttachmentMeta("3", "new.txt", "text/plain", 7),),
    )
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1}, [first], {}, 100.0)
    store.apply_folder_sync(ACCOUNT, "INBOX", 11, {1}, [second], {}, 200.0)

    assert store.read(ACCOUNT, "INBOX", 10, 1)["subject"] == "Subject"  # type: ignore[index]
    assert store.read(ACCOUNT, "INBOX", 11, 1)["subject"] == "replacement"  # type: ignore[index]
    assert store.read(ACCOUNT, "INBOX", 12, 1) is None
    assert store.attachment(ACCOUNT, "INBOX", 10, 1, "2")["filename"] == "old.pdf"  # type: ignore[index]
    assert store.attachment(ACCOUNT, "INBOX", 11, 1, "2") is None

    downloaded = tmp_path / "downloads" / "new.txt"
    store.mark_attachment_downloaded(ACCOUNT, "INBOX", 11, 1, "3", downloaded)
    assert store.attachment(ACCOUNT, "INBOX", 11, 1, "3")["downloaded_path"] == str(downloaded)  # type: ignore[index]


def test_message_resync_preserves_matching_downloaded_attachment_path(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    original = message(1)
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1}, [original], {}, 100.0)
    downloaded = tmp_path / "downloads" / "report.pdf"
    store.mark_attachment_downloaded(ACCOUNT, "INBOX", 10, 1, "2", downloaded)

    refreshed = replace(
        original,
        subject="refreshed metadata",
        attachments=(AttachmentMeta("2", "renamed.pdf", "application/pdf", 84),),
    )
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1}, [refreshed], {}, 200.0)

    attachment = store.attachment(ACCOUNT, "INBOX", 10, 1, "2")
    assert attachment is not None
    assert attachment["filename"] == "renamed.pdf"
    assert attachment["size"] == 84
    assert attachment["downloaded_path"] == str(downloaded)


def test_folder_errors_and_states_preserve_success_then_clear_on_sync(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1}, [message(1)], {}, 100.0)
    store.record_folder_error(ACCOUNT, "INBOX", "network", "timed out")

    state = store.folder_state(ACCOUNT, "INBOX")
    assert state is not None
    assert (state.uidvalidity, state.last_success, state.last_error_code, state.last_error_message) == (
        10,
        100.0,
        "network",
        "timed out",
    )
    assert store.folder_states(ACCOUNT) == [
        {
            "folder": "INBOX",
            "uidvalidity": 10,
            "last_success": 100.0,
            "last_error_code": "network",
            "last_error_message": "timed out",
        }
    ]

    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1}, [], {}, 200.0)
    cleared = store.folder_state(ACCOUNT, "INBOX")
    assert cleared is not None
    assert (cleared.last_success, cleared.last_error_code, cleared.last_error_message) == (200.0, "", "")


def test_first_sync_error_is_visible_without_a_successful_folder_row(tmp_path: Path) -> None:
    store = MailStore(tmp_path / "mail.db")

    store.record_folder_error(ACCOUNT, "已发送", "folder_unavailable", "select failed")

    state = store.folder_state(ACCOUNT, "已发送")
    assert state is not None
    assert (state.uidvalidity, state.last_success, state.last_error_code, state.last_error_message) == (
        0,
        None,
        "folder_unavailable",
        "select failed",
    )
    assert store.folder_states(ACCOUNT) == [
        {
            "folder": "已发送",
            "uidvalidity": 0,
            "last_success": None,
            "last_error_code": "folder_unavailable",
            "last_error_message": "select failed",
        }
    ]
