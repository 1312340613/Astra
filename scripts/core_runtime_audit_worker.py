"""Hard process deadline and reaping boundary for one provider-audit probe."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from collections.abc import Callable
from multiprocessing.process import BaseProcess
from typing import Any, Protocol


class WorkerConnection(Protocol):
    """Shared byte-message interface for POSIX and Windows named pipes."""

    def send_bytes(self, buf: bytes) -> None: ...
    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...
    def close(self) -> None: ...


WorkerTarget = Callable[[WorkerConnection, str, dict[str, Any]], None]
_POLL_SECONDS = 0.01
_KILL_REAP_SECONDS = 2.0
MAX_WORKER_RESULT_BYTES = 2_048


class ProbeManagementError(RuntimeError):
    """Raised when the parent cannot prove a child was reaped."""


def _failure(probe_id: str, error_type: str, started: float) -> dict[str, Any]:
    return {
        "id": probe_id,
        "status": (
            "external_failure" if error_type == "ProbeTimeout" else "product_failure"
        ),
        "duration_ms": max(0, int((time.monotonic() - started) * 1_000)),
        "error_type": error_type,
    }


def _join_until(process: BaseProcess, deadline: float) -> bool:
    while process.is_alive() and time.monotonic() < deadline:
        process.join(min(0.05, max(0.0, deadline - time.monotonic())))
    if not process.is_alive():
        process.join()
        return True
    return False


def _stop_process(process: BaseProcess) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    if _join_until(process, time.monotonic() + 0.2):
        return
    kill = getattr(process, "kill", None)
    if not callable(kill):  # pragma: no cover - all supported Python versions have kill
        raise ProbeManagementError("Probe worker has no hard-kill primitive")
    kill()
    if not _join_until(process, time.monotonic() + _KILL_REAP_SECONDS):
        raise ProbeManagementError("Probe worker remained live after hard kill")


def _hard_kill_process(process: BaseProcess) -> None:
    """Kill a timed-out child immediately, then spend only bounded time reaping."""
    if not process.is_alive():
        process.join()
        return
    kill = getattr(process, "kill", None)
    if not callable(kill):  # pragma: no cover - supported Python has kill
        raise ProbeManagementError("Probe worker has no hard-kill primitive")
    kill()
    if not _join_until(process, time.monotonic() + _KILL_REAP_SECONDS):
        raise ProbeManagementError("Probe worker remained live after hard kill")


def send_worker_result(connection: WorkerConnection, result: object) -> None:
    """Send one small, non-executable JSON result over the worker pipe."""
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = b'{"status":"product_failure","error_type":"InvalidProbeResult"}'
    if len(encoded) > MAX_WORKER_RESULT_BYTES:
        encoded = b'{"status":"product_failure","error_type":"InvalidProbeResult"}'
    connection.send_bytes(encoded)


def _decode_worker_result(connection: WorkerConnection) -> dict[str, Any]:
    try:
        encoded = connection.recv_bytes(maxlength=MAX_WORKER_RESULT_BYTES)
    except EOFError as exc:
        raise LookupError("ProbeWorkerExit") from exc
    except OSError as exc:
        raise RuntimeError("ProbeIPCFailure") from exc
    try:
        result = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("InvalidProbeResult") from exc
    if not isinstance(result, dict):
        raise ValueError("InvalidProbeResult")
    return result


def _make_process(context, *, target, args, name):
    return context.Process(
        target=target,
        args=args,
        daemon=True,
        name=name,
    )


async def run_probe_worker(
    probe_id: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    worker_target: WorkerTarget,
) -> dict[str, Any]:
    """Run one probe under a deadline that begins after successful spawn.

    Python offers no reliable way to interrupt ``Process.start()`` itself. The
    operation deadline therefore begins only after start returns; reaping after
    a timeout has its own short bound.
    """
    started = time.monotonic()
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = _make_process(
        context,
        target=worker_target,
        args=(send, probe_id, payload),
        name=f"astra-audit-{probe_id}",
    )
    try:
        if float(timeout) <= 0:
            return _failure(probe_id, "ProbeTimeout", started)
        try:
            process.start()
        except Exception:
            if process.pid is not None:
                _stop_process(process)
            return _failure(probe_id, "ProbeStartFailure", started)
        send.close()
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if not process.is_alive():
                process.join()
                try:
                    available = receive.poll()
                except OSError as exc:
                    # Windows PeekNamedPipe may report ERROR_BROKEN_PIPE
                    # after the worker closes an empty result channel.
                    disconnected = isinstance(exc, BrokenPipeError) or getattr(exc, "winerror", None) == 109
                    return _failure(probe_id, "ProbeWorkerExit" if disconnected else "ProbeIPCFailure", started)
                if not available:
                    return _failure(probe_id, "ProbeWorkerExit", started)
                try:
                    return _decode_worker_result(receive)
                except LookupError:
                    return _failure(probe_id, "ProbeWorkerExit", started)
                except ValueError:
                    return _failure(probe_id, "InvalidProbeResult", started)
                except RuntimeError:
                    return _failure(probe_id, "ProbeIPCFailure", started)
            await asyncio.sleep(
                min(_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
            )
        _hard_kill_process(process)
        return _failure(probe_id, "ProbeTimeout", started)
    except asyncio.CancelledError:
        if process.pid is not None:
            _stop_process(process)
        raise
    except ProbeManagementError:
        raise
    except Exception:
        if process.pid is not None:
            _stop_process(process)
        return _failure(probe_id, "ProbeWorkerFailure", started)
    finally:
        send.close()
        receive.close()
