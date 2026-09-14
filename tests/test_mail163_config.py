from pathlib import Path

import pytest

from agent.mail163.config import (
    MailConfigurationError,
    decode_mailbox_name,
    encode_mailbox_name,
    load_mail_config,
    resolve_folder,
)


def test_process_environment_overrides_project_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ASTRA_163_EMAIL=file@163.com\nASTRA_163_AUTH_CODE=file-secret\n",
        encoding="utf-8",
    )
    config = load_mail_config(
        tmp_path,
        environ={"ASTRA_163_EMAIL": "env@163.com", "ASTRA_163_AUTH_CODE": "env-secret"},
    )
    assert config.account == "env@163.com"
    assert config.auth_code == "env-secret"
    assert config.database_path == tmp_path / ".astra" / "mail" / "163.sqlite3"
    assert config.default_folders == ("INBOX", "已发送")
    assert config.max_body_bytes == 5 * 1024 * 1024


def test_missing_credentials_does_not_create_local_state(tmp_path: Path) -> None:
    with pytest.raises(MailConfigurationError, match="ASTRA_163_EMAIL"):
        load_mail_config(tmp_path, environ={})
    assert not (tmp_path / ".astra").exists()


@pytest.mark.parametrize(
    ("source", "encoded"),
    [("INBOX", b"INBOX"), ("A&B", b"A&-B"), ("已发送", b"&XfJT0ZAB-")],
)
def test_modified_utf7_round_trip(source: str, encoded: bytes) -> None:
    assert encode_mailbox_name(source) == encoded
    assert decode_mailbox_name(encoded) == source


def test_folder_aliases_are_stable() -> None:
    assert resolve_folder("sent") == "已发送"
    assert resolve_folder("收件箱") == "INBOX"
