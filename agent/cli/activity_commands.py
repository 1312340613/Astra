"""Management commands for the local-only computer activity archive."""

from __future__ import annotations

from agent.runtime.paths import state_path

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, NoReturn

from agent.runtime.activity_store import ActivityStore
from agent.runtime.activity_sync import (
    ActivitySynchronizer,
    SourceDiscoveryError,
    SourceRoots,
    SyncReport,
    activity_sync_lock_path,
    resolve_source_roots,
    source_is_recent,
)

from .activity_launchd import (
    install_launch_agent,
    launch_agent_path,
    launch_agent_status,
    uninstall_launch_agent,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAX_OUTPUT_CHARS = 12_000
_SYNC_ERROR_TYPES = {
    "event_root_unavailable",
    "summary_root_unavailable",
    "summary_read_error",
    "source_read_error",
    "SourceDiscoveryError",
    "integrity_error",
}
_STATUS_ERROR_TYPES = _SYNC_ERROR_TYPES | {
    "JSONDecodeError",
    "UnicodeDecodeError",
    "ValueError",
}


class _ArgumentError(ValueError):
    pass


class _ActivityArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError


def _parser() -> argparse.ArgumentParser:
    parser = _ActivityArgumentParser(prog="astra activity")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--scheduled", action="store_true")
    commands.add_parser("status")
    install = commands.add_parser("install")
    install.add_argument("--event-root", default="", help="pin sync to this event archive (M5 source switch)")
    commands.add_parser("uninstall")
    browser_install = commands.add_parser("browser-install")
    browser_install.add_argument("--exclude-domain", action="append", default=[])
    commands.add_parser("browser-uninstall")
    commands.add_parser("browser-status")
    commands.add_parser("browser-token")
    commands.add_parser("rebuild-index")
    clear = commands.add_parser("clear")
    scope = clear.add_mutually_exclusive_group(required=True)
    scope.add_argument("--before")
    scope.add_argument("--all", action="store_true")
    return parser


def _bounded_path(path: Path | str) -> str:
    return str(path)[:1024]


def _error_type(value: str) -> str:
    if not value:
        return ""
    candidate = value.split(":", 1)[0]
    return candidate if candidate in _STATUS_ERROR_TYPES else "sync_error"


def _emit(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded) > _MAX_OUTPUT_CHARS:
        encoded = json.dumps(
            {"action": payload.get("action", "activity"), "status": "error", "error_type": "OutputLimitError"},
            separators=(",", ":"),
            sort_keys=True,
        )
    print(encoded)


def _sync_payload(report: SyncReport) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": report.status,
        "events_imported": report.events_imported,
        "events_duplicate": report.events_duplicate,
        "summaries_imported": report.summaries_imported,
        "summaries_ignored": report.summaries_ignored,
        "malformed_lines": report.malformed_lines,
        "deferred_lines": report.deferred_lines,
        "already_running": report.already_running,
        "source_active": report.source_active,
    }
    error_type = report.error if report.error in _SYNC_ERROR_TYPES else ("sync_error" if report.error else "")
    if error_type:
        payload["error_type"] = error_type
    return payload


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        Path(self.baseFilename).chmod(0o600)
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        _harden_retained_logs(Path(self.baseFilename).parent)


def _harden_retained_logs(log_dir: Path) -> None:
    for suffix in ("", ".1", ".2", ".3"):
        path = log_dir / f"activity-sync.log{suffix}"
        if path.exists() and not path.is_symlink():
            path.chmod(0o600)


def scheduled_activity_logger(log_dir: Path | None = None) -> logging.Logger:
    resolved_dir = (
        log_dir or Path(os.getenv("ASTRA_ACTIVITY_LOG_DIR", str(state_path("logs", root=_PROJECT_ROOT)))).expanduser()
    )
    resolved_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_dir.chmod(0o700)
    logger = logging.getLogger(f"astra.activity_sync.{resolved_dir.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old_handler in logger.handlers[:]:
        old_handler.close()
        logger.removeHandler(old_handler)
    handler = _PrivateRotatingFileHandler(
        resolved_dir / "activity-sync.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _harden_retained_logs(resolved_dir)
    return logger


def log_scheduled_report(logger: logging.Logger, report: SyncReport) -> None:
    logger.info(json.dumps(_sync_payload(report), separators=(",", ":"), sort_keys=True))


def _source_state() -> tuple[SourceRoots | None, dict[str, Any]]:
    try:
        roots = resolve_source_roots()
    except SourceDiscoveryError as exc:
        return None, {
            "source_status": "unavailable",
            "source_fresh": False,
            "source_override": "ASTRA_COMPUTER_HISTORY_ROOT",
            "error_type": type(exc).__name__,
        }
    available = roots.event_root.is_dir()
    fresh = available and source_is_recent(roots)
    return roots, {
        "source_status": "fresh" if fresh else ("stale" if available else "unavailable"),
        "source_fresh": fresh,
        "source_override": "ASTRA_COMPUTER_HISTORY_ROOT",
        "source_paths": {
            "events": _bounded_path(roots.event_root),
            "summaries": _bounded_path(roots.summary_root),
        },
    }


def _store_status(store: ActivityStore) -> dict[str, Any]:
    counts = store.stats()
    last_success_row = store.connection.execute(
        "SELECT MAX(last_success_at) FROM sync_files WHERE last_success_at != ''"
    ).fetchone()
    last_error_row = store.connection.execute(
        "SELECT last_error FROM sync_files WHERE last_error != '' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    journal_row = store.connection.execute("PRAGMA journal_mode").fetchone()
    return {
        "database_path": _bounded_path(store.path),
        "counts": counts,
        "last_success_at": str(last_success_row[0]) if last_success_row and last_success_row[0] else "",
        "last_error_type": _error_type(str(last_error_row[0])) if last_error_row else "",
        "wal_enabled": bool(journal_row and str(journal_row[0]).lower() == "wal"),
        **store.integrity_status(),
        "raw_payload_exposed": False,
    }


def _close_store(store: Any) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        close()


def _scheduled_sync(store_factory: Callable[[], ActivityStore]) -> int:
    logger = scheduled_activity_logger()
    try:
        try:
            roots = resolve_source_roots()
        except SourceDiscoveryError:
            log_scheduled_report(logger, SyncReport(status="source_unavailable", error="SourceDiscoveryError"))
            return 0
        if not roots.event_root.is_dir():
            log_scheduled_report(
                logger,
                SyncReport(status="source_unavailable", error="event_root_unavailable"),
            )
            return 0
        if not source_is_recent(roots):
            log_scheduled_report(logger, SyncReport(status="inactive"))
            return 0
        store = store_factory()
        try:
            report = ActivitySynchronizer(store, roots, activity_sync_lock_path(store.path)).sync(force=True)
        finally:
            _close_store(store)
        log_scheduled_report(logger, report)
        return 0 if report.status in {"ok", "partial", "inactive", "source_unavailable"} else 1
    # A scheduled job must convert every dependency failure into metadata-only logs.
    except Exception as exc:  # noqa: BLE001
        log_scheduled_report(logger, SyncReport(status="error", error=type(exc).__name__))
        return 1
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def execute_activity_command(argv: Sequence[str], *, store_factory: Callable[[], ActivityStore] = ActivityStore) -> int:
    try:
        arguments = _parser().parse_args(list(argv))
    except _ArgumentError:
        candidate = str(argv[0]) if argv else "activity"
        action = (
            candidate
            if candidate in {"sync", "status", "install", "uninstall", "rebuild-index", "clear",
                             "browser-install", "browser-uninstall", "browser-status", "browser-token"}
            else "activity"
        )
        _emit({"action": action, "status": "error", "error_type": "ArgumentError"})
        return 2
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0

    if arguments.command == "sync" and arguments.scheduled:
        return _scheduled_sync(store_factory)

    action = str(arguments.command)
    try:
        if action.startswith("browser-"):
            from . import activity_browser

            if action == "browser-install":
                domains = arguments.exclude_domain or os.getenv("ASTRA_ACTIVITY_EXCLUDE_DOMAINS", "").split(",")
                result = activity_browser.install(project_root=_PROJECT_ROOT,
                    python_executable=Path(sys.executable).absolute(), excluded_domains=domains)
            elif action == "browser-uninstall":
                result = activity_browser.uninstall()
            elif action == "browser-token":
                result = activity_browser.copy_token()
            else:
                result = activity_browser.status()
            _emit({"action": action, **result})
            return 0 if result.get("status") == "ok" else 1

        if action == "install":
            result = install_launch_agent(
                python_executable=Path(sys.executable).absolute(),
                project_root=_PROJECT_ROOT,
                event_root=Path(arguments.event_root) if getattr(arguments, "event_root", "") else None,
            )
            payload = {"action": action, "status": "ok", **result}
            if result.get("scheduler_status") == "error":
                payload["status"] = "error"
            _emit(payload)
            return 0 if payload["status"] == "ok" else 1

        if action == "uninstall":
            result = uninstall_launch_agent()
            payload = {"action": action, "status": "ok", **result}
            if result.get("scheduler_status") == "error":
                payload["status"] = "error"
            _emit(payload)
            return 0 if payload["status"] == "ok" else 1

        if action == "status":
            _roots, source_payload = _source_state()
            store = store_factory()
            try:
                payload = {
                    "action": action,
                    "status": "ok",
                    **source_payload,
                    **_store_status(store),
                    **launch_agent_status(),
                    "launch_agent_path": _bounded_path(launch_agent_path()),
                }
            finally:
                _close_store(store)
            healthy = payload["integrity_status"] == payload["fts_status"] == "ok"
            if not healthy:
                payload["status"] = "error"
            _emit(payload)
            return 0 if healthy else 1

        store = store_factory()
        try:
            if action == "sync":
                try:
                    roots = resolve_source_roots()
                except SourceDiscoveryError as exc:
                    _emit(
                        {
                            "action": action,
                            "status": "source_unavailable",
                            "error_type": type(exc).__name__,
                        }
                    )
                    return 0
                report = ActivitySynchronizer(store, roots, activity_sync_lock_path(store.path)).sync(force=True)
                _emit({"action": action, **_sync_payload(report)})
                return 0 if report.status in {"ok", "partial", "source_unavailable"} else 1
            if action == "rebuild-index":
                counts = store.rebuild_fts()
                _emit({"action": action, "status": "ok", "counts": counts})
                return 0
            cutoff = datetime.now(UTC).isoformat() if arguments.all else str(arguments.before)
            deleted = store.clear(before=cutoff)
            _emit({"action": action, "status": "ok", "before": cutoff, **deleted})
            return 0
        finally:
            _close_store(store)
    except ValueError:
        _emit({"action": action, "status": "error", "error_type": "ValueError"})
        return 2
    except (sqlite3.DatabaseError, OSError) as exc:
        _emit({"action": action, "status": "error", "error_type": type(exc).__name__})
        return 1
    # The CLI boundary emits only the class, never an arbitrary exception message.
    except Exception as exc:  # noqa: BLE001
        _emit({"action": action, "status": "error", "error_type": type(exc).__name__})
        return 1
