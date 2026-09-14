"""Logging setup for Agent System."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _non_negative_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


def setup_logging(name: str = "agent") -> None:
    root = logging.getLogger()
    if getattr(root, "_agent_logging_configured", False):
        return

    level_name = os.getenv("AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_dir = Path(os.getenv("AGENT_LOG_DIR", ".logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # Keep diagnostics useful over long-running desktop sessions without
    # relying on an external logrotate service (especially on Windows).
    # delay=True also avoids opening the file until the first record exists.
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_non_negative_int_env("AGENT_LOG_MAX_BYTES", 5 * 1024 * 1024),
        backupCount=_non_negative_int_env("AGENT_LOG_BACKUP_COUNT", 3),
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    root.setLevel(level)
    root.addHandler(file_handler)

    if os.getenv("AGENT_LOG_STDERR", "").lower() in {"1", "true", "yes"}:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.setLevel(level)
        root.addHandler(stderr_handler)

    setattr(root, "_agent_logging_configured", True)
