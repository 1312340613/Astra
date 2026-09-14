"""Durable pause/resume of explicitly owned companion services during maintenance."""

from __future__ import annotations

import sys
from contextlib import contextmanager

from .common import LauncherError, read_json, write_json
from .installation import Installation
from .transaction import receipt

JOURNAL = "services.json"


def adapter_for(install: Installation):
    # Keep the isolated updater dependency-free and platform imports lazy.
    if sys.platform == "darwin":
        from .service_launchd import LaunchdServices
        from .service_embedding import EmbeddingServices
        return CombinedServices(EmbeddingServices(install), LaunchdServices(install))
    if sys.platform == "win32":
        from .service_windows import WindowsServices
        return WindowsServices(install)
    return NoServices()


class NoServices:
    def discover(self) -> list[dict]:
        return []

    def validate(self, entry: dict) -> None:
        raise LauncherError("The service recovery journal belongs to another platform.")

    def stop(self, entry: dict) -> None:
        self.validate(entry)

    def start(self, entry: dict) -> None:
        self.validate(entry)


class CombinedServices:
    def __init__(self, embedding, launchd):
        self.adapters = {"embedding": embedding, "launchd": launchd}

    def discover(self) -> list[dict]:
        return [entry for adapter in self.adapters.values() for entry in adapter.discover()]

    def _adapter(self, entry: dict):
        key = entry.get("adapter")
        adapter = self.adapters.get(key) if isinstance(key, str) else None
        if adapter is None:
            raise LauncherError("Unknown companion service adapter in recovery journal.")
        return adapter

    def validate(self, entry: dict) -> None:
        self._adapter(entry).validate(entry)

    def stop(self, entry: dict) -> None:
        self._adapter(entry).stop(entry)

    def start(self, entry: dict) -> None:
        self._adapter(entry).start(entry)


class ServiceMaintenance:
    """The caller holds update.lock from discovery through restoration.

    Stop intent is saved before the OS call. If we crash at either side of that
    call, recovery can safely restore the recorded service without guessing.
    A pending source transaction always takes precedence over service restart.
    """

    def __init__(self, install: Installation, *, recovering: bool = False):
        self.install = install
        self.path = install.control / JOURNAL
        self.adapter = adapter_for(install)
        self.restoration_failed = False
        self.errors: dict[str, str] = {}
        if recovering and self.path.exists():
            if self.path.stat().st_size > 128 * 1024:
                raise LauncherError("Service recovery journal is too large; preserve it for inspection.")
            self.state = read_json(self.path)
            entries = self.state.get("services")
            if (self.state.get("schema") != 1 or self.state.get("root") != str(install.root)
                    or self.state.get("platform") != sys.platform or not isinstance(entries, list)
                    or len(entries) > 16):
                raise LauncherError("Invalid service recovery journal; preserve it for inspection.")
            identifiers = set()
            for entry in entries:
                if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                        or entry["id"] in identifiers
                        or entry.get("phase") not in {"planned", "stopping", "paused", "restoring", "restored"}):
                    raise LauncherError("Invalid service recovery entry.")
                self.adapter.validate(entry)
                identifiers.add(entry["id"])
        else:
            self.state = {"schema": 1, "root": str(install.root), "platform": sys.platform,
                          "services": [dict(entry, phase="planned") for entry in self.adapter.discover()]}

    @property
    def entries(self) -> list[dict]:
        return self.state["services"]

    @property
    def allowed_pids(self) -> set[int]:
        # Periodic jobs can start between discovery and preflight; stale PIDs can
        # also be reused. Re-prove current ownership rather than exempt old IDs.
        selected = {entry["id"] for entry in self.entries}
        return {pid for entry in self.adapter.discover() if entry["id"] in selected
                for pid in entry.get("pids", []) if type(pid) is int and pid > 0}

    def summary(self) -> list[dict]:
        return [dict({"id": entry["id"], "state": entry["phase"]},
                     **({"message": self.errors[entry["id"]]} if entry["id"] in self.errors else {}))
                for entry in self.entries]

    def save(self) -> None:
        write_json(self.path, self.state)

    def pause(self) -> None:
        if not self.entries:
            return
        self.save()
        for entry in reversed(self.entries):
            # Also handles a job that a service manager reloaded after a crash.
            entry["phase"] = "stopping"
            self.save()
            print(f"Astra: pausing {entry['id']}...", file=sys.stderr, flush=True)
            self.adapter.stop(entry)
            entry["phase"] = "paused"
            self.save()

    def restore(self) -> None:
        for entry in self.entries:
            if entry["phase"] in {"planned", "restored"}:
                continue
            try:
                entry["phase"] = "restoring"
                self.save()
                print(f"Astra: restoring {entry['id']}...", file=sys.stderr, flush=True)
                self.adapter.start(entry)
                entry["phase"] = "restored"
                self.save()
            except (LauncherError, OSError) as exc:
                self.restoration_failed = True
                # Adapters' LauncherErrors are intentionally content-free; raw OS
                # errors and native command output never enter receipts.
                message = str(exc) if isinstance(exc, LauncherError) else type(exc).__name__
                self.errors[entry["id"]] = message
                print(f"Astra: {entry['id']} could not be restored: {message} "
                      "Run astra update --recover to retry.", file=sys.stderr, flush=True)
        if not self.restoration_failed:
            self.path.unlink(missing_ok=True)

    @contextmanager
    def suspended(self):
        try:
            self.pause()
            yield
        finally:
            if (self.install.control / "pending.json").exists():
                if self.entries:
                    print("Astra: companion services remain paused until astra update --recover completes.",
                          file=sys.stderr, flush=True)
            else:
                self.restore()

    def finish(self, result: dict) -> dict:
        if not self.entries:
            return result
        result = dict(result, services=self.summary(),
                      services_status="restart_failed" if self.restoration_failed else "restored")
        if self.restoration_failed:
            result["recovery_command"] = "astra update --recover"
        # Enrich the receipt only after service restoration has been observed.
        fields = {k: v for k, v in result.items() if k not in {"schema", "root", "time"}}
        return receipt(self.install, **fields)


def pending_services(install: Installation) -> bool:
    return (install.control / JOURNAL).exists()
