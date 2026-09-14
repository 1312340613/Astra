"""Real Git maintenance transactions with deterministic companion services."""

from __future__ import annotations

import sys

import pytest

from agent.launcher import cli, dependencies, services, setup, update
from agent.launcher.common import LauncherError, git, read_json, write_json
from agent.launcher.locking import RuntimeLease
from agent.launcher.transaction import Transaction
from test_launcher import advance, source as source_fixture

source = source_fixture


class FakeServices:
    def __init__(self, install):
        self.install = install
        self.running = {"recorder": True, "scheduled-sync": True}
        self.calls = []
        self.fail_stop = ""
        self.fail_start = ""

    def discover(self):
        return [{"id": name, "pids": [100 + index]} for index, (name, running) in enumerate(self.running.items()) if running]

    def validate(self, entry):
        if entry["id"] not in self.running:
            raise LauncherError("unknown service")

    def stop(self, entry):
        saved = read_json(self.install.control / services.JOURNAL)
        assert any(e["id"] == entry["id"] and e["phase"] == "stopping" for e in saved["services"])
        self.calls.append(("stop", entry["id"]))
        if entry["id"] == self.fail_stop:
            raise LauncherError("stop failure")
        self.running[entry["id"]] = False

    def start(self, entry):
        assert not (self.install.control / "pending.json").exists(), "never restart on incomplete code"
        self.calls.append(("start", entry["id"]))
        if entry["id"] == self.fail_start:
            raise LauncherError("restart failure")
        self.running[entry["id"]] = True


@pytest.fixture
def companion(source, monkeypatch):
    inst, _ = source
    adapter = FakeServices(inst)
    monkeypatch.setattr(services, "adapter_for", lambda _: adapter)
    def holders(_):
        return [f"PID {100 + i}: {name}; uses this installation" for i, (name, running)
                in enumerate(adapter.running.items()) if running]
    monkeypatch.setattr(update, "legacy_processes", holders)
    return adapter


def test_update_pauses_services_before_mutation_then_restores(source, companion, monkeypatch):
    inst, seed = source
    target = advance(seed)
    sync = dependencies.synchronize
    def observe(*args, **kwargs):
        assert not any(companion.running.values())
        sync(*args, **kwargs)
    monkeypatch.setattr(dependencies, "synchronize", observe)
    result = update.update_source(inst)
    assert result["target"] == target
    assert result["services_status"] == "restored"
    assert companion.calls == [("stop", "scheduled-sync"), ("stop", "recorder"),
                               ("start", "recorder"), ("start", "scheduled-sync")]
    assert not services.pending_services(inst)
    assert read_json(inst.control / "receipts/latest.json")["services_status"] == "restored"


@pytest.mark.parametrize("mode", ["check", "current", "cancel", "choice-required", "tooling-failure"])
def test_no_service_disruption_before_actual_update(source, companion, monkeypatch, mode):
    inst, seed = source
    if mode == "current":
        dependencies.record_environment(inst, [])
        monkeypatch.setattr(dependencies, "is_ready", lambda _: True)
        monkeypatch.setattr(dependencies, "node_health", lambda _: None)
        assert update.update_source(inst)["outcome"] == "current"
    else:
        advance(seed, "astra.py")
        if mode == "check":
            assert len(update.update_source(inst, check=True)["managed_services"]) == 2
        elif mode == "tooling-failure":
            def fail(_):
                raise LauncherError("missing uv")
            monkeypatch.setattr(dependencies, "uv_command", fail)
            with pytest.raises(LauncherError, match="missing uv"):
                update.update_source(inst)
        else:
            (inst.root / "astra.py").write_text("local work")
            if mode == "cancel":
                assert update.update_source(inst, choose_local=lambda _: "cancel")["outcome"] == "cancelled"
            else:
                with pytest.raises(LauncherError, match="Choose --keep-local"):
                    update.update_source(inst)
    assert companion.calls == []
    assert not services.pending_services(inst)


def test_unknown_holders_and_interactive_sessions_are_not_stopped(source, companion, monkeypatch):
    inst, seed = source
    advance(seed)
    with RuntimeLease(inst, "interface"), pytest.raises(LauncherError, match="Close these Astra instances"):
        update.update_source(inst)
    monkeypatch.setattr(update, "legacy_processes", lambda _: ["PID 999: unrelated Python"])
    with pytest.raises(LauncherError, match="unrelated Python"):
        update.update_source(inst)
    assert companion.calls == []


def test_rollback_restores_services_only_after_old_code_is_back(source, companion, monkeypatch):
    inst, seed = source
    old = git(inst.root, "rev-parse", "HEAD")
    advance(seed)
    def fail(*args, **kwargs):
        raise LauncherError("bad runtime")
    monkeypatch.setattr(dependencies, "validate", fail)
    start = companion.start
    def check_start(entry):
        assert git(inst.root, "rev-parse", "HEAD") == old
        start(entry)
    monkeypatch.setattr(companion, "start", check_start)
    with pytest.raises(LauncherError, match="previous installation was restored"):
        update.update_source(inst)
    assert all(companion.running.values())
    assert not services.pending_services(inst)


def test_partial_stop_failure_restores_every_stop_intent(source, companion):
    inst, seed = source
    old = git(inst.root, "rev-parse", "HEAD")
    advance(seed)
    companion.fail_stop = "recorder"
    with pytest.raises(LauncherError, match="stop failure"):
        update.update_source(inst)
    assert all(companion.running.values())
    assert not services.pending_services(inst)
    assert git(inst.root, "rev-parse", "HEAD") == old


def test_restart_failure_keeps_installed_code_and_is_recoverable(source, companion, monkeypatch):
    inst, seed = source
    target = advance(seed)
    companion.fail_start = "recorder"
    result = update.update_source(inst)
    assert result["outcome"] == "applied" and result["services_status"] == "restart_failed"
    assert git(inst.root, "rev-parse", "HEAD") == target
    assert services.pending_services(inst)
    assert not (inst.control / "pending.json").exists()
    with pytest.raises(LauncherError, match="--recover"):
        RuntimeLease(inst, "interface")
    companion.fail_start = ""
    companion.calls.clear()
    recovered = update.update_source(inst, recover=True)
    assert recovered["outcome"] == "services_recovered"
    assert recovered["services_status"] == "restored"
    assert companion.calls == [("start", "recorder")]
    assert not services.pending_services(inst)
    assert git(inst.root, "rev-parse", "HEAD") == target


def test_incomplete_rollback_leaves_services_paused_until_recovery(source, companion, monkeypatch):
    inst, seed = source
    old = git(inst.root, "rev-parse", "HEAD")
    advance(seed)
    def fail(*args, **kwargs):
        raise LauncherError("injected failure")
    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "validate", fail)
        patch.setattr(Transaction, "recover", fail)
        with pytest.raises(LauncherError, match="recovery needs attention"):
            update.update_source(inst)
    assert not any(companion.running.values())
    assert (inst.control / "pending.json").exists() and services.pending_services(inst)
    result = update.update_source(inst, recover=True)
    assert result["outcome"] == "recovered" and result["services_status"] == "restored"
    assert all(companion.running.values())
    assert git(inst.root, "rev-parse", "HEAD") == old


def test_crash_after_stop_intent_before_transaction_restores_only_intended_services(source, companion):
    inst, _ = source
    manager = services.ServiceMaintenance(inst)
    manager.entries[0]["phase"] = "stopping"
    manager.save()
    result = update.update_source(inst, recover=True)
    assert result["outcome"] == "services_recovered"
    assert companion.calls == [("start", "recorder")]
    assert not services.pending_services(inst)


@pytest.mark.parametrize("corruption", ["root", "platform", "duplicate", "phase"])
def test_invalid_service_journal_never_dispatches(source, companion, corruption):
    inst, _ = source
    manager = services.ServiceMaintenance(inst)
    if corruption in {"root", "platform"}:
        manager.state[corruption] = "other"
    elif corruption == "duplicate":
        manager.entries.append(manager.entries[0].copy())
    else:
        manager.entries[0]["phase"] = "nonsense"
    manager.save()
    with pytest.raises(LauncherError, match="Invalid service"):
        update.update_source(inst, recover=True)
    assert not companion.calls


def test_setup_repair_uses_the_same_service_lifecycle(source, companion, monkeypatch):
    inst, _ = source
    monkeypatch.setattr(dependencies, "ensure_uv", lambda _: None)
    result = setup.setup_source(inst, [], repair=True)
    assert result["outcome"] == "ready" and result["services_status"] == "restored"
    assert all(companion.running.values())


def test_disabled_services_are_not_enabled(source, companion):
    inst, seed = source
    companion.running["recorder"] = False
    advance(seed)
    result = update.update_source(inst)
    assert result["services"] == [{"id": "scheduled-sync", "state": "restored"}]
    assert not companion.running["recorder"]


def test_service_restart_failure_is_nonzero_for_json_cli(source, monkeypatch, capsys):
    inst, _ = source
    monkeypatch.setattr(cli, "update_source", lambda *args, **kwargs: {"outcome": "applied", "services_status": "restart_failed"})
    assert cli.main(["--updater-child", "update", "--json"], root=inst.root) == 1
    assert '"restart_failed"' in capsys.readouterr().out


def test_pending_service_state_is_visible_in_doctor(source):
    inst, _ = source
    write_json(inst.control / services.JOURNAL, {"schema": 1, "root": str(inst.root), "platform": sys.platform, "services": []})
    report = dependencies.diagnostics(inst)
    assert report["pending_services"] and not report["healthy"]
    assert any(problem["component"] == "services" for problem in report["problems"])
