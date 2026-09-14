"""LaunchAgent contracts without contacting the user's service manager."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from agent.launcher import service_launchd as mod
from agent.launcher.common import LauncherError
from agent.launcher.installation import discover

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX LaunchAgent identity and private-file rules")


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    home = tmp_path / "home"
    folder = home / "Library/LaunchAgents"
    folder.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    root = tmp_path / "Astra 中文 & spaces"
    root.mkdir()
    (root / "agent").mkdir()
    (root / "astra.py").touch()
    (root / "pyproject.toml").write_text('[project]\nname="agent-lab-local"\nversion="0.2.0"')
    inst = discover(root)
    adapter = mod.LaunchdServices(inst)
    loaded = {}
    calls = []
    def install(identifier, *, active=True, other=False, pid=42):
        project = root if not other else tmp_path / "other"
        data = {"Label": identifier, "WorkingDirectory": str(project), "RunAtLoad": True}
        if identifier == mod.NATIVE_JOB:
            data["ProgramArguments"] = [str(home / "Library/Application Support/Astra/bin/AstraActivityRecorder")]
            data["KeepAlive"] = True
        else:
            executable = inst.python if not other else project / ".venv/bin/python"
            data["ProgramArguments"] = [str(executable), *mod.PYTHON_JOBS[identifier]]
            if identifier.endswith("browser-bridge"):
                data["ProgramArguments"] += ["--excluded-domains", "", "--state", str(home / "private state")]
                data["KeepAlive"] = True
            else:
                data["StartInterval"] = 300
        path = folder / (identifier + ".plist")
        path.write_bytes(plistlib.dumps(data))
        path.chmod(0o600)
        if active:
            loaded[identifier] = (data, pid)
        return path
    def run(*args):
        calls.append(args)
        identifier = Path(args[-1]).name.removesuffix(".plist")
        if args[0] == "bootout":
            loaded.pop(identifier, None)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "bootstrap":
            data = plistlib.loads(Path(args[-1]).read_bytes())
            loaded[identifier] = (data, 77)
            return subprocess.CompletedProcess(args, 0, "", "")
        if identifier not in loaded:
            return subprocess.CompletedProcess(args, 113, "", "Could not find service")
        data, pid = loaded[identifier]
        state = "running" if pid else "not running"
        arg_text = "".join("\t\t" + arg + "\n" for arg in data["ProgramArguments"])
        text = (f"\tpath = {folder / (identifier + '.plist')}\n"
                f"\tprogram = {data['ProgramArguments'][0]}\n\targuments = {{\n{arg_text}\t}}\n"
                f"\tstate = {state}\n" + (f"\tpid = {pid}\n" if pid else ""))
        return subprocess.CompletedProcess(args, 0, text, "")
    monkeypatch.setattr(mod, "_run", run)
    return adapter, install, loaded, calls


def test_all_owned_jobs_including_idle_schedules_restore_without_rewriting_plists(launchd):
    adapter, install, loaded, calls = launchd
    paths = [install(name, pid=0 if name.endswith(("sync", "summarizer")) else 42)
             for name in (mod.NATIVE_JOB, *mod.PYTHON_JOBS)]
    original = {path: path.read_bytes() for path in paths}
    entries = adapter.discover()
    assert {e["id"] for e in entries} == {mod.NATIVE_JOB, *mod.PYTHON_JOBS}
    assert len([e for e in entries if e["pids"] == []]) == 2
    for entry in reversed(entries):
        adapter.stop(entry)
    assert not loaded
    for entry in entries:
        adapter.start(entry)
    assert len(loaded) == 4
    assert all(path.read_bytes() == content for path, content in original.items())
    assert any(args[0] == "bootstrap" and "中文 & spaces" not in args[-1] for args in calls)


def test_unloaded_and_other_checkout_jobs_are_not_managed(launchd):
    adapter, install, _, calls = launchd
    install(mod.NATIVE_JOB)
    install("com.astra.activity-sync", active=False)
    install("com.astra.activity-browser-bridge", other=True)
    assert adapter.discover() == []
    assert not any(args[0] in {"bootout", "bootstrap"} for args in calls)


def test_stable_native_recorder_without_checkout_ownership_is_not_managed(launchd):
    adapter, install, _, _ = launchd
    install(mod.NATIVE_JOB)
    assert adapter.discover() == []


@pytest.mark.parametrize("change_before", ["stop", "start"])
def test_changed_configuration_is_preserved_and_not_dispatched(launchd, change_before):
    adapter, install, _, calls = launchd
    path = install("com.astra.activity-sync")
    entry = adapter.discover()[0]
    if change_before == "start":
        adapter.stop(entry)
    data = plistlib.loads(path.read_bytes())
    data["StartInterval"] = 900
    path.write_bytes(plistlib.dumps(data))
    calls.clear()
    with pytest.raises(LauncherError, match="changed during maintenance"):
        getattr(adapter, change_before)(entry)
    assert calls == []
    assert plistlib.loads(path.read_bytes())["StartInterval"] == 900


def test_loaded_definition_from_other_installation_is_rejected(launchd):
    adapter, install, loaded, _ = launchd
    install("com.astra.activity-sync")
    data, pid = loaded["com.astra.activity-sync"]
    altered = {**data, "ProgramArguments": ["/other/python", *data["ProgramArguments"][1:]]}
    loaded["com.astra.activity-sync"] = (altered, pid)
    with pytest.raises(LauncherError, match="differs from its saved configuration"):
        adapter.discover()


def test_unavailable_service_manager_is_not_treated_as_unloaded(launchd, monkeypatch):
    adapter, install, _, _ = launchd
    install("com.astra.activity-sync")
    monkeypatch.setattr(mod, "_run", lambda *args: subprocess.CompletedProcess(args, 1, "", "Permission denied"))
    with pytest.raises(LauncherError, match="state is unknown"):
        adapter.discover()


def test_stop_timeout_does_not_claim_quiescence(launchd, monkeypatch):
    adapter, install, _, _ = launchd
    install("com.astra.activity-sync")
    entry = adapter.discover()[0]
    monkeypatch.setattr(adapter, "_query", lambda *args: {"running": True, "pid": 42})
    monkeypatch.setattr(mod, "TIMEOUT", 0)
    with pytest.raises(LauncherError, match="still stopping"):
        adapter.stop(entry)


def test_continuous_job_must_be_running_but_periodic_job_can_be_idle(launchd, monkeypatch):
    adapter, install, _, _ = launchd
    install("com.astra.activity-sync", pid=0)
    install("com.astra.activity-browser-bridge", pid=0)
    entries = {e["id"]: e for e in adapter.discover()}
    monkeypatch.setattr(mod, "TIMEOUT", 0)
    adapter.start(entries["com.astra.activity-sync"])
    with pytest.raises(LauncherError, match="did not return"):
        adapter.start(entries["com.astra.activity-browser-bridge"])


@pytest.mark.parametrize("unsafe", ["symlink", "writable", "wrong-label"])
def test_unsafe_or_unowned_definition_is_never_dispatched(launchd, unsafe):
    adapter, install, _, calls = launchd
    path = install("com.astra.activity-sync")
    if unsafe == "symlink":
        other = path.with_suffix(".original")
        path.rename(other)
        path.symlink_to(other)
    elif unsafe == "writable":
        path.chmod(0o666)
    else:
        data = plistlib.loads(path.read_bytes())
        data["Label"] = "other"
        path.write_bytes(plistlib.dumps(data))
    if unsafe == "wrong-label":
        assert adapter.discover() == []
    else:
        with pytest.raises(LauncherError):
            adapter.discover()
    assert not any(args[0] in {"bootstrap", "bootout"} for args in calls)
