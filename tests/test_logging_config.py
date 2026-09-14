import logging
from logging.handlers import RotatingFileHandler

from agent.logging_config import setup_logging


def _reset_agent_logging(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    if hasattr(root, "_agent_logging_configured"):
        delattr(root, "_agent_logging_configured")


def test_setup_logging_uses_delayed_bounded_rotation(tmp_path, monkeypatch):
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    _reset_agent_logging(root)
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_LOG_MAX_BYTES", "256")
    monkeypatch.setenv("AGENT_LOG_BACKUP_COUNT", "2")
    try:
        setup_logging("bounded")

        handler = next(
            item for item in root.handlers if isinstance(item, RotatingFileHandler)
        )
        assert handler.maxBytes == 256
        assert handler.backupCount == 2
        assert handler.stream is None
        assert not (tmp_path / "bounded.log").exists()

        for index in range(20):
            logging.getLogger("rotation-test").info("record-%02d %s", index, "x" * 40)

        handler.flush()
        assert (tmp_path / "bounded.log").is_file()
        assert (tmp_path / "bounded.log.1").is_file()
        assert not (tmp_path / "bounded.log.3").exists()
    finally:
        _reset_agent_logging(root)
        root.setLevel(original_level)
        for handler in original_handlers:
            root.addHandler(handler)


def test_setup_logging_invalid_rotation_values_use_defaults(tmp_path, monkeypatch):
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    _reset_agent_logging(root)
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_LOG_MAX_BYTES", "invalid")
    monkeypatch.setenv("AGENT_LOG_BACKUP_COUNT", "-1")
    try:
        setup_logging("defaults")
        handler = next(
            item for item in root.handlers if isinstance(item, RotatingFileHandler)
        )
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
    finally:
        _reset_agent_logging(root)
        root.setLevel(original_level)
        for handler in original_handlers:
            root.addHandler(handler)
