import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import agent.runtime.tools.comfyui as comfyui_module
from agent.runtime.tools.comfyui import (
    _apply_workflow_settings,
    _build_anima_prompts,
    _build_anima_workflow,
    _build_prompts,
    register_comfyui_tools,
)
from agent.runtime.tools.registry import ToolRegistry

requires_posix_native = pytest.mark.skipif(
    os.name == "nt", reason="native lifecycle state uses POSIX process and filesystem APIs",
)


def _configure_native(monkeypatch, tmp_path):
    root = tmp_path / "ComfyUI"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    main = root / "main.py"
    main.write_text("", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "native")
    monkeypatch.setenv("COMFYUI_NATIVE_ROOT", str(root))
    monkeypatch.setenv("COMFYUI_NATIVE_PYTHON", str(python))
    return root.resolve(), python.resolve(), main.resolve()


def _owned_identity(pid, root, python, argv, start_token="owned-token"):
    return comfyui_module._NativeProcessIdentity(
        pid=pid,
        executable=str(python),
        argv=tuple(str(value) for value in argv),
        cwd=str(root),
        start_token=start_token,
        pgid=pid,
    )


def _valid_native_metadata(pid, root, python, main, argv, start_token="owned-token"):
    return {
        "schema_version": 1,
        "pid": pid,
        "root": str(root),
        "python": str(python),
        "main_path": str(main),
        "argv": list(argv),
        "start_token": start_token,
    }


def _forbid_lifecycle_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external lifecycle must not use a managed-process boundary")

    monkeypatch.setattr(comfyui_module, "_run_wsl", forbidden)
    monkeypatch.setattr(comfyui_module, "_spawn_wsl_comfy", forbidden)
    monkeypatch.setattr(comfyui_module, "_spawn_native_comfy", forbidden)
    monkeypatch.setattr(comfyui_module, "_native_process_identity", forbidden)
    monkeypatch.setattr(comfyui_module, "_native_process_group_members", forbidden)
    monkeypatch.setattr(comfyui_module, "_native_group_exists", forbidden)
    monkeypatch.setattr(comfyui_module.os, "killpg", forbidden, raising=False)


def test_register_comfyui_tools_exposes_status_and_draw(tmp_path):
    registry = ToolRegistry()

    register_comfyui_tools(registry, workdir=str(tmp_path))

    assert "comfyui_status" in registry.tool_names
    assert "comfyui_start" in registry.tool_names
    assert "comfyui_stop" in registry.tool_names
    assert "comfyui_draw" in registry.tool_names
    assert "comfyui_anima_status" in registry.tool_names
    assert "comfyui_anima_draw" in registry.tool_names
    assert "comfyui_result" in registry.tool_names
    draw_tool = registry.get("comfyui_draw")
    assert draw_tool is not None
    assert draw_tool.timeout is None
    assert draw_tool.parameters["required"] == ["prompt"]
    assert "copy_to_astra_cache" in draw_tool.parameters["properties"]
    assert "copy_to_hermes_cache" not in draw_tool.parameters["properties"]
    for tool_name in ("comfyui_start", "comfyui_stop"):
        lifecycle_tool = registry.get(tool_name)
        assert lifecycle_tool.risk == "execute"
        assert lifecycle_tool.approval == "on_risk"


def test_comfyui_status_preserves_lifecycle_and_workflow_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "invalid-mode")
    monkeypatch.setenv("COMFYUI_WORKFLOW", str(tmp_path / "missing-workflow.json"))
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_status").fn())

    assert "COMFYUI_LIFECYCLE" in payload["error"]
    assert "workflow" in payload["error"].lower()


def test_comfyui_start_tool_timeout_covers_configured_readiness_and_cleanup(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("COMFYUI_START_TIMEOUT", "120")
    registry = ToolRegistry()

    register_comfyui_tools(registry, workdir=str(tmp_path))

    assert registry.get("comfyui_start").timeout >= 155


def test_comfyui_stop_tool_timeout_covers_wsl_discovery_stop_and_verification(tmp_path):
    registry = ToolRegistry()

    register_comfyui_tools(registry, workdir=str(tmp_path))

    assert registry.get("comfyui_stop").timeout >= 35


def test_comfyui_start_returns_already_running_when_api_is_online(monkeypatch, tmp_path):
    monkeypatch.setattr(comfyui_module, "_request_json", lambda *args, **kwargs: {
        "devices": [{"name": "cuda:0 test GPU"}],
    })
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is True
    assert payload["status"] == "already_running"
    assert payload["server_ok"] is True
    assert payload["device"] == "cuda:0 test GPU"


def test_comfyui_start_uses_wsl_then_requires_verified_api(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "wsl")
    monkeypatch.setenv("COMFYUI_WSL_ROOT", "/home/example/comfy/ComfyUI")
    requests = iter([OSError("offline"), {"devices": [{"name": "cuda:0 test GPU"}]}])
    monkeypatch.setattr(comfyui_module, "_request_json", lambda *args, **kwargs: next(requests))
    scripts = []

    def fake_wsl(script, timeout=20):
        scripts.append(script)
        return subprocess.CompletedProcess(["wsl.exe"], 0, stdout="", stderr="")

    monkeypatch.setattr(comfyui_module, "_run_wsl", fake_wsl)
    monkeypatch.setattr(
        comfyui_module,
        "_spawn_wsl_comfy",
        lambda server_url, host_log_path: type("Proc", (), {"pid": 4321})(),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is True
    assert payload["status"] == "started"
    assert payload["pid"] == "4321"


def test_comfyui_stop_targets_only_matching_wsl_processes(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "wsl")
    monkeypatch.setenv("COMFYUI_WSL_ROOT", "/home/example/comfy/ComfyUI")
    scripts = []

    def fake_wsl(script, timeout=20):
        scripts.append(script)
        stdout = "123\n456\n" if "/proc/[0-9]*" in script else ""
        return subprocess.CompletedProcess(["wsl.exe"], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(comfyui_module, "_run_wsl", fake_wsl)
    monkeypatch.setattr(comfyui_module, "_request_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is True
    assert payload["status"] == "stopped"
    assert payload["stopped_pids"] == ["123", "456"]
    assert any("kill -TERM 123 456" in script for script in scripts)


def test_comfyui_lifecycle_auto_uses_wsl_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "auto")
    monkeypatch.setenv("COMFYUI_NATIVE_ROOT", "/configured/ComfyUI")
    monkeypatch.setenv("COMFYUI_NATIVE_PYTHON", "/configured/python")

    assert comfyui_module._comfyui_lifecycle_mode() == "wsl"


def test_comfyui_lifecycle_auto_uses_native_on_configured_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "auto")
    monkeypatch.setenv("COMFYUI_NATIVE_ROOT", "/configured/ComfyUI")
    monkeypatch.setenv("COMFYUI_NATIVE_PYTHON", "/configured/python")

    assert comfyui_module._comfyui_lifecycle_mode() == "native"


def test_comfyui_lifecycle_auto_uses_external_on_unconfigured_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "auto")
    monkeypatch.delenv("COMFYUI_NATIVE_ROOT", raising=False)
    monkeypatch.delenv("COMFYUI_NATIVE_PYTHON", raising=False)

    assert comfyui_module._comfyui_lifecycle_mode() == "external"


def test_comfyui_lifecycle_auto_uses_external_on_unconfigured_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "auto")
    monkeypatch.delenv("COMFYUI_NATIVE_ROOT", raising=False)
    monkeypatch.delenv("COMFYUI_NATIVE_PYTHON", raising=False)

    assert comfyui_module._comfyui_lifecycle_mode() == "external"


@pytest.mark.parametrize(
    ("host_platform", "mode"),
    [
        ("darwin", "wsl"),
        ("linux", "wsl"),
        ("win32", "native"),
        ("freebsd13", "native"),
    ],
)
@pytest.mark.parametrize("tool_name", ["comfyui_start", "comfyui_stop"])
def test_impossible_explicit_lifecycle_is_rejected_before_http_or_process_access(
    monkeypatch, tmp_path, host_platform, mode, tool_name,
):
    monkeypatch.setattr(sys, "platform", host_platform)
    monkeypatch.setenv("COMFYUI_LIFECYCLE", mode)

    def forbidden(*args, **kwargs):
        raise AssertionError("platform rejection must precede side effects")

    monkeypatch.setattr(comfyui_module, "_request_json", forbidden)
    monkeypatch.setattr(comfyui_module, "_run_wsl", forbidden)
    monkeypatch.setattr(comfyui_module, "_spawn_wsl_comfy", forbidden)
    monkeypatch.setattr(comfyui_module, "_spawn_native_comfy", forbidden)
    monkeypatch.setattr(comfyui_module.os, "killpg", forbidden, raising=False)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    with pytest.raises(ValueError, match="not supported"):
        registry.get(tool_name).fn()


def test_comfyui_lifecycle_auto_uses_external_on_unsupported_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd13")
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "auto")
    monkeypatch.setenv("COMFYUI_NATIVE_ROOT", "/configured/ComfyUI")
    monkeypatch.setenv("COMFYUI_NATIVE_PYTHON", "/configured/python")

    assert comfyui_module._comfyui_lifecycle_mode() == "external"


def test_comfyui_external_start_reports_manual_action(monkeypatch, tmp_path):
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "external")
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    _forbid_lifecycle_side_effects(monkeypatch)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert payload["status"] == "external_management_required"
    assert payload["lifecycle"] == "external"
    assert payload["server_ok"] is False
    assert "externally" in payload["error"].lower()


def test_comfyui_external_stop_never_signals_processes(monkeypatch, tmp_path):
    monkeypatch.setenv("COMFYUI_LIFECYCLE", "external")
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: {"devices": [{"name": "test GPU"}]},
    )
    _forbid_lifecycle_side_effects(monkeypatch)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    assert payload["status"] == "external_management_required"
    assert payload["lifecycle"] == "external"
    assert payload["server_ok"] is True
    assert "externally" in payload["error"].lower()


@requires_posix_native
def test_comfyui_native_start_spawns_absolute_main_in_new_session(monkeypatch, tmp_path):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    log_path = tmp_path / "logs" / "comfyui.log"
    monkeypatch.setenv("COMFYUI_NATIVE_LOG", str(log_path))
    requests = iter([
        OSError("offline"),
        OSError("still offline under lifecycle lock"),
        {"devices": [{"name": "test GPU"}]},
    ])
    monkeypatch.setattr(comfyui_module, "_request_json", lambda *args, **kwargs: next(requests))
    popen_calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(comfyui_module.subprocess, "Popen", fake_popen)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(pid, root, python, expected_argv),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is True
    assert payload["lifecycle"] == "native"
    assert payload["pid"] == "4321"
    args, kwargs = popen_calls[0]
    assert args[:2] == [str(python.resolve()), str(main.resolve())]
    assert args[-5:] == ["--listen", "0.0.0.0", "--port", "8188", "--lowvram"]
    assert kwargs["cwd"] == str(root)
    assert kwargs["start_new_session"] is True
    metadata = json.loads((tmp_path / ".astra" / "comfyui-native.json").read_text(encoding="utf-8"))
    assert metadata["schema_version"] == 1
    assert metadata["pid"] == 4321
    assert metadata["main_path"] == str(main.resolve())
    assert metadata["start_token"] == "owned-token"


@requires_posix_native
def test_comfyui_native_start_cleans_process_group_when_metadata_write_fails(monkeypatch, tmp_path):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(pid, root, python, expected_argv),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_write_native_pid_metadata",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("metadata unavailable")),
    )
    monkeypatch.setattr(
        comfyui_module.subprocess,
        "Popen",
        lambda *args, **kwargs: type(
            "Proc", (), {"pid": 4321, "poll": lambda self: None},
        )(),
    )
    signals = []
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(comfyui_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: frozenset({(4321, "owned-token")}),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: False)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert payload["status"] == "not_started"
    assert "metadata unavailable" in payload["error"]
    assert signals == [(4321, comfyui_module.signal.SIGTERM), (4321, comfyui_module.signal.SIGKILL)]


@requires_posix_native
def test_comfyui_native_start_timeout_cleans_owned_group_and_metadata(monkeypatch, tmp_path):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    monkeypatch.setenv("COMFYUI_START_TIMEOUT", "1")
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    reaped = {"value": False}

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            assert timeout is not None
            reaped["value"] = True
            return -comfyui_module.signal.SIGKILL

    monkeypatch.setattr(
        comfyui_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(pid, root, python, expected_argv),
    )
    monotonic_values = iter([0.0, 100.0, 101.0])
    monkeypatch.setattr(comfyui_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    signals = []
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: frozenset({(4321, "owned-token")}),
    )
    monkeypatch.setattr(
        comfyui_module, "_native_group_exists", lambda pid: not reaped["value"],
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert payload["status"] == "starting_or_failed"
    assert signals == [
        (4321, comfyui_module.signal.SIGTERM),
        (4321, comfyui_module.signal.SIGKILL),
    ]
    assert reaped["value"] is True
    assert not (tmp_path / ".astra" / "comfyui-native.json").exists()


@requires_posix_native
def test_native_cleanup_empty_snapshot_with_existing_group_fails_closed(monkeypatch):
    signals = []
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        comfyui_module, "_native_process_group_members", lambda pid: frozenset(),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: True)

    error, remains = comfyui_module._terminate_continuous_native_group(
        4321, frozenset({(4321, "owned-token")}),
    )

    assert remains is True
    assert "membership" in error
    assert signals == [(4321, comfyui_module.signal.SIGTERM)]


@requires_posix_native
def test_native_cleanup_treats_exact_zombie_only_group_as_exited(monkeypatch):
    signals = []
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: frozenset({(4321, "owned-token")}),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: True)
    monkeypatch.setattr(
        comfyui_module, "_native_process_state", lambda pid: "Z", raising=False,
    )

    error, remains = comfyui_module._terminate_continuous_native_group(
        4321, frozenset({(4321, "owned-token")}),
    )

    assert error == ""
    assert remains is False
    assert signals == [
        (4321, comfyui_module.signal.SIGTERM),
        (4321, comfyui_module.signal.SIGKILL),
    ]


@requires_posix_native
def test_comfyui_native_identity_capture_failure_cleans_stable_spawn_handle(
    monkeypatch, tmp_path,
):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    class FakeProcess:
        pid = 4321

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(comfyui_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(comfyui_module, "_native_process_identity", lambda pid: None)
    monkeypatch.setattr(comfyui_module.os, "getpgid", lambda pid: pid)
    snapshots = iter([
        frozenset({(4321, "spawn-token")}),
        frozenset({(4321, "spawn-token")}),
    ])
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: next(snapshots),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: False)
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    signals = []
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: signals.append(sig))
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert "exact native ComfyUI process identity" in payload["error"]
    assert signals == [comfyui_module.signal.SIGTERM, comfyui_module.signal.SIGKILL]
    assert not (tmp_path / ".astra" / "comfyui-native.json").exists()


@requires_posix_native
def test_comfyui_native_incomplete_identity_cleanup_retains_quarantine_and_blocks_respawn(
    monkeypatch, tmp_path,
):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    class FakeProcess:
        pid = 4321

        @staticmethod
        def poll():
            return None

    spawned = []
    monkeypatch.setattr(
        comfyui_module.subprocess,
        "Popen",
        lambda *args, **kwargs: spawned.append(FakeProcess()) or spawned[-1],
    )
    monkeypatch.setattr(comfyui_module, "_native_process_identity", lambda pid: None)
    monkeypatch.setattr(comfyui_module.os, "getpgid", lambda pid: pid)
    snapshots = iter([
        frozenset({(4321, "spawn-token")}),
        None,
    ])
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: next(snapshots),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: True)
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    signals = []
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: signals.append(sig))
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    first = json.loads(registry.get("comfyui_start").fn())
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    second = json.loads(registry.get("comfyui_start").fn())

    assert first["success"] is False
    assert "could not be rechecked" in first["error"]
    assert metadata["state"] == "quarantine"
    assert metadata["pid"] == 4321
    assert metadata["start_token"] == "spawn-token"
    assert second["success"] is False
    assert second["status"] == "cleanup_incomplete"
    assert len(spawned) == 1
    assert signals == [comfyui_module.signal.SIGTERM]


@requires_posix_native
def test_comfyui_native_unavailable_anchor_still_quarantines_and_blocks_respawn(
    monkeypatch, tmp_path,
):
    _configure_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    class FakeProcess:
        pid = 4321

        @staticmethod
        def poll():
            return None

    spawned = []
    monkeypatch.setattr(
        comfyui_module.subprocess,
        "Popen",
        lambda *args, **kwargs: spawned.append(FakeProcess()) or spawned[-1],
    )
    monkeypatch.setattr(comfyui_module, "_native_process_identity", lambda pid: None)
    monkeypatch.setattr(comfyui_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(comfyui_module, "_native_process_group_members", lambda pid: None)
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: True)
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module.os,
        "killpg",
        lambda *args: (_ for _ in ()).throw(AssertionError("unanchored group must not be signaled")),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    first = json.loads(registry.get("comfyui_start").fn())
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    second = json.loads(registry.get("comfyui_start").fn())

    assert first["success"] is False
    assert "could not snapshot" in first["error"]
    assert metadata["state"] == "quarantine"
    assert metadata["start_token"] == ""
    assert second["status"] == "cleanup_incomplete"
    assert len(spawned) == 1


@requires_posix_native
def test_comfyui_native_stop_never_signals_or_discards_unverified_quarantine(
    monkeypatch, tmp_path,
):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata_path.parent.mkdir()
    metadata_path.write_text(json.dumps({
        "schema_version": 1,
        "pid": 4321,
        "root": str(root),
        "python": str(python),
        "main_path": str(main),
        "argv": expected_argv,
        "start_token": "",
        "state": "quarantine",
    }), encoding="utf-8")
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(pid, root, python, expected_argv, "later-token"),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: True)
    monkeypatch.setattr(
        comfyui_module.os,
        "killpg",
        lambda *args: (_ for _ in ()).throw(AssertionError("quarantine must not be signaled")),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    assert payload["status"] == "stop_incomplete"
    assert metadata_path.exists()


def test_linux_start_token_includes_boot_id_and_start_ticks(tmp_path):
    proc_root = tmp_path / "proc"
    (proc_root / "sys" / "kernel" / "random").mkdir(parents=True)
    (proc_root / "sys" / "kernel" / "random" / "boot_id").write_text(
        "boot-identity\n", encoding="utf-8",
    )
    process_dir = proc_root / "4321"
    process_dir.mkdir()
    fields_after_name = ["S", *[str(value) for value in range(1, 19)], "987654", "tail"]
    (process_dir / "stat").write_text(
        f"4321 (python worker) {' '.join(fields_after_name)}\n", encoding="utf-8",
    )

    token = comfyui_module._linux_process_start_token(4321, proc_root=proc_root)

    assert token == "linux:boot-identity:987654"


@requires_posix_native
def test_stale_metadata_discard_does_not_unlink_newer_record(tmp_path):
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    old = {"pid": 123, "start_token": "old"}
    newer = {"pid": 456, "start_token": "new"}
    comfyui_module._write_native_pid_metadata(metadata_path, old)
    comfyui_module._write_native_pid_metadata(metadata_path, newer)

    removed = comfyui_module._discard_native_pid_metadata(metadata_path, expected=old)

    assert removed is False
    assert comfyui_module._read_native_pid_metadata(metadata_path) == newer


@pytest.mark.parametrize("symlink_name", ["metadata", "lock"])
@requires_posix_native
def test_native_lifecycle_rejects_symlinked_state_files_before_spawn(
    monkeypatch, tmp_path, symlink_name,
):
    _configure_native(monkeypatch, tmp_path)
    state_dir = tmp_path / ".astra"
    state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do not touch", encoding="utf-8")
    name = "comfyui-native.json" if symlink_name == "metadata" else "comfyui-native.lock"
    (state_dir / name).symlink_to(outside)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_spawn_native_comfy",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert "symbolic link" in payload["error"]
    assert outside.read_text(encoding="utf-8") == "do not touch"


@requires_posix_native
def test_native_lifecycle_rejects_symlinked_state_directory_before_spawn(monkeypatch, tmp_path):
    _configure_native(monkeypatch, tmp_path)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    (tmp_path / ".astra").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_spawn_native_comfy",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert "symbolic link" in payload["error"]
    assert list(outside.iterdir()) == []


@requires_posix_native
def test_metadata_writer_rejects_preexisting_symlinked_temp_path(monkeypatch, tmp_path):
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata_path.parent.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do not touch", encoding="utf-8")

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(comfyui_module.uuid, "uuid4", lambda: FixedUuid())
    temporary = metadata_path.with_name(
        f".{metadata_path.name}.{os.getpid()}.fixed.tmp",
    )
    temporary.symlink_to(outside)

    with pytest.raises(OSError, match="temporary metadata path"):
        comfyui_module._write_native_pid_metadata(metadata_path, {"pid": 4321})

    assert temporary.is_symlink()
    assert outside.read_text(encoding="utf-8") == "do not touch"


@requires_posix_native
def test_native_lifecycle_lock_uses_exclusive_posix_lock(monkeypatch, tmp_path):
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    operations = []
    monkeypatch.setattr(
        comfyui_module.fcntl,
        "flock",
        lambda fd, operation: operations.append(operation),
    )

    with comfyui_module._native_lifecycle_lock(metadata_path):
        operations.append("critical-section")

    assert operations == [
        comfyui_module.fcntl.LOCK_EX | comfyui_module.fcntl.LOCK_NB,
        "critical-section",
        comfyui_module.fcntl.LOCK_UN,
    ]


@requires_posix_native
def test_native_start_returns_lifecycle_busy_without_waiting_to_spawn(monkeypatch, tmp_path):
    _configure_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(
        comfyui_module.fcntl,
        "flock",
        lambda fd, operation: (
            (_ for _ in ()).throw(BlockingIOError("held"))
            if operation & comfyui_module.fcntl.LOCK_NB
            else None
        ),
    )
    monotonic_values = iter([10.0, 15.0])
    monkeypatch.setattr(comfyui_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module,
        "_spawn_native_comfy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a timed-out lock must never later spawn")
        ),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert payload["status"] == "lifecycle_busy"


@requires_posix_native
def test_native_stop_lifecycle_busy_never_inspects_or_signals_processes(monkeypatch, tmp_path):
    _configure_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        comfyui_module.fcntl,
        "flock",
        lambda fd, operation: (
            (_ for _ in ()).throw(BlockingIOError("held"))
            if operation & comfyui_module.fcntl.LOCK_NB
            else None
        ),
    )
    monotonic_values = iter([10.0, 15.0])
    monkeypatch.setattr(comfyui_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("busy stop must not inspect or signal a process")

    monkeypatch.setattr(comfyui_module, "_native_process_identity", forbidden)
    monkeypatch.setattr(comfyui_module, "_native_process_group_members", forbidden)
    monkeypatch.setattr(comfyui_module.os, "killpg", forbidden)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    assert payload["status"] == "lifecycle_busy"


@requires_posix_native
def test_native_state_rejects_group_or_world_writable_permissions(tmp_path):
    state_dir = tmp_path / ".astra"
    state_dir.mkdir(mode=0o777)
    state_dir.chmod(0o777)

    with pytest.raises(OSError, match="permissions"):
        with comfyui_module._native_lifecycle_lock(
            state_dir / "comfyui-native.json",
        ):
            pass


@requires_posix_native
def test_native_state_rejects_group_or_world_writable_metadata_file(tmp_path):
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    comfyui_module._write_native_pid_metadata(metadata_path, {"test": True})
    metadata_path.chmod(0o666)

    with pytest.raises(OSError, match="permissions"):
        comfyui_module._read_native_pid_metadata(metadata_path)


@requires_posix_native
def test_metadata_reader_revalidates_opened_file_permissions(monkeypatch, tmp_path):
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    comfyui_module._write_native_pid_metadata(metadata_path, {"test": True})
    real_open = os.open

    def chmod_before_metadata_open(path, flags, *args, **kwargs):
        if path == metadata_path.name and kwargs.get("dir_fd") is not None:
            metadata_path.chmod(0o666)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(comfyui_module.os, "open", chmod_before_metadata_open)

    with pytest.raises(OSError, match="permissions"):
        comfyui_module._read_native_pid_metadata(metadata_path)


@requires_posix_native
def test_state_directory_revalidates_opened_descriptor_permissions(monkeypatch, tmp_path):
    state_dir = tmp_path / ".astra"
    state_dir.mkdir(mode=0o700)
    real_open = os.open

    def chmod_before_directory_open(path, flags, *args, **kwargs):
        if path == state_dir:
            state_dir.chmod(0o777)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(comfyui_module.os, "open", chmod_before_directory_open)

    with pytest.raises(OSError, match="permissions"):
        comfyui_module._open_native_state_dir(state_dir, create=False)


@requires_posix_native
def test_metadata_creation_fsyncs_file_state_directory_and_workdir(monkeypatch, tmp_path):
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    synced_descriptors = []
    monkeypatch.setattr(comfyui_module.os, "fsync", lambda fd: synced_descriptors.append(fd))

    comfyui_module._write_native_pid_metadata(metadata_path, {"pid": 4321})

    assert len(synced_descriptors) >= 3


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc only")
def test_darwin_process_start_token_has_microsecond_identity():
    token = comfyui_module._darwin_process_start_token(os.getpid())

    prefix, seconds, microseconds = token.split(":")
    assert prefix == "darwin"
    assert int(seconds) > 0
    assert 0 <= int(microseconds) < 1_000_000


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS process APIs only")
def test_darwin_process_identity_reads_exact_kernel_process_data():
    identity = comfyui_module._darwin_process_identity(os.getpid())

    assert identity is not None
    assert os.path.samefile(identity.executable, sys.executable)
    assert os.path.samefile(identity.cwd, Path.cwd())
    assert identity.argv
    assert identity.start_token.startswith("darwin:")
    assert identity.pgid == os.getpgid(os.getpid())


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS process APIs only")
def test_darwin_process_group_snapshot_has_stable_current_identity():
    # The CI runner's shared process group can contain inaccessible or exiting
    # siblings. Managed native launches have their own session and process group.
    subprocess.run(
        [sys.executable, "-c", """
import os
from agent.runtime.tools import comfyui

pid = os.getpid()
assert os.getpgid(pid) == pid
token = comfyui._darwin_process_start_token(pid)
assert token.startswith("darwin:")
members = comfyui._darwin_process_group_members(pid)
assert members == frozenset({(pid, token)}), members
"""],
        cwd=Path(__file__).resolve().parents[1],
        start_new_session=True,
        check=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        "invalid-pid",
        "recorded-python",
        "recorded-argv",
        "executable",
        "argv",
        "cwd",
        "start-token",
        "process-group",
    ],
)
@requires_posix_native
def test_comfyui_native_stop_refuses_stale_pid_metadata(
    monkeypatch, tmp_path, mismatch,
):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    metadata_pid = "not-a-pid" if mismatch == "invalid-pid" else 4321
    metadata = {
        "schema_version": 1,
        "pid": metadata_pid,
        "root": str(root),
        "python": str(python),
        "main_path": str(main),
        "argv": expected_argv,
        "start_token": "owned-token",
    }
    if mismatch == "recorded-python":
        metadata["python"] = "/tmp/not-python"
    if mismatch == "recorded-argv":
        metadata["argv"] = [*expected_argv, "--extra"]
    state_dir = tmp_path / ".astra"
    state_dir.mkdir()
    (state_dir / "comfyui-native.json").write_text(json.dumps(metadata), encoding="utf-8")
    identity = _owned_identity(4321, root, python, expected_argv)
    if mismatch == "executable":
        identity = comfyui_module._NativeProcessIdentity(
            **{**identity.__dict__, "executable": "/tmp/not-python"},
        )
    elif mismatch == "argv":
        identity = comfyui_module._NativeProcessIdentity(
            **{**identity.__dict__, "argv": (*identity.argv, "--extra")},
        )
    elif mismatch == "cwd":
        identity = comfyui_module._NativeProcessIdentity(
            **{**identity.__dict__, "cwd": "/tmp/not-comfy"},
        )
    elif mismatch == "start-token":
        identity = comfyui_module._NativeProcessIdentity(
            **{**identity.__dict__, "start_token": "new-process-token"},
        )
    elif mismatch == "process-group":
        identity = comfyui_module._NativeProcessIdentity(
            **{**identity.__dict__, "pgid": 9999},
        )
    monkeypatch.setattr(comfyui_module, "_native_process_identity", lambda pid: identity)
    signals = []
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)), raising=False)
    monkeypatch.setattr(
        comfyui_module,
        "_native_group_exists",
        lambda pid: (_ for _ in ()).throw(AssertionError("unowned groups must not be inspected")),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: {"devices": [{"name": "test GPU"}]},
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    schema_invalid = mismatch in {"invalid-pid", "recorded-python", "recorded-argv"}
    assert payload["status"] == (
        "invalid_pid_metadata" if schema_invalid else "stale_pid_metadata"
    )
    assert payload["lifecycle"] == "native"
    assert payload["pid"] == str(metadata_pid)
    assert "refusing" in payload["error"].lower()
    assert signals == []
    assert (state_dir / "comfyui-native.json").exists() is schema_invalid


@pytest.mark.parametrize(
    "mutation",
    [
        "root-main-mismatch",
        "missing-fixed-argument",
        "extra-argument",
        "invalid-port",
        "unknown-version",
        "float-version",
        "float-pid",
    ],
)
@requires_posix_native
def test_comfyui_native_stop_rejects_noncanonical_metadata_before_process_access(
    monkeypatch, tmp_path, mutation,
):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    metadata = _valid_native_metadata(4321, root, python, main, expected_argv)
    if mutation == "root-main-mismatch":
        metadata["root"] = str((tmp_path / "OtherRoot").resolve())
    elif mutation == "missing-fixed-argument":
        metadata["argv"] = expected_argv[:-1]
    elif mutation == "extra-argument":
        metadata["argv"] = [*expected_argv, "--preview-method", "auto"]
    elif mutation == "invalid-port":
        metadata["argv"] = [*expected_argv]
        metadata["argv"][5] = "70000"
    elif mutation == "unknown-version":
        metadata["schema_version"] = 999
    elif mutation == "float-version":
        metadata["schema_version"] = 1.0
    elif mutation == "float-pid":
        metadata["pid"] = 4321.0
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    comfyui_module._write_native_pid_metadata(metadata_path, metadata)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid metadata must fail before process access")

    monkeypatch.setattr(comfyui_module, "_native_process_identity", forbidden)
    monkeypatch.setattr(comfyui_module, "_native_process_group_members", forbidden)
    monkeypatch.setattr(comfyui_module.os, "killpg", forbidden)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    assert payload["status"] == "invalid_pid_metadata"
    assert metadata_path.exists()


@requires_posix_native
def test_comfyui_native_stop_retains_metadata_when_leader_is_gone_but_group_remains(
    monkeypatch, tmp_path,
):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata_path.parent.mkdir()
    metadata_path.write_text(json.dumps({
        "schema_version": 1,
        "pid": 4321,
        "root": str(root),
        "python": str(python),
        "main_path": str(main),
        "argv": expected_argv,
        "start_token": "owned-token",
    }), encoding="utf-8")
    monkeypatch.setattr(comfyui_module, "_native_process_identity", lambda pid: None)
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: True)
    monkeypatch.setattr(
        comfyui_module.os,
        "killpg",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not signal without leader identity")),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    assert payload["status"] == "stop_incomplete"
    assert metadata_path.exists()


@requires_posix_native
def test_comfyui_native_start_blocks_duplicate_when_owned_metadata_uses_old_config(
    monkeypatch, tmp_path,
):
    _configure_native(monkeypatch, tmp_path)
    old_root = (tmp_path / "OldComfyUI").resolve()
    old_python = (old_root / ".venv" / "bin" / "python").resolve()
    old_main = (old_root / "main.py").resolve()
    old_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", old_python, old_main,
    )
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata_path.parent.mkdir()
    metadata = {
        "schema_version": 1,
        "pid": 4321,
        "root": str(old_root),
        "python": str(old_python),
        "main_path": str(old_main),
        "argv": old_argv,
        "start_token": "old-owned-token",
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(
            pid, old_root, old_python, old_argv, "old-owned-token",
        ),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_spawn_native_comfy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("live recorded ownership must block a duplicate spawn")
        ),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_start").fn())

    assert payload["success"] is False
    assert payload["status"] == "owned_config_mismatch"
    assert comfyui_module._read_native_pid_metadata(metadata_path) == metadata


@requires_posix_native
def test_comfyui_native_stop_manages_recorded_owned_group_after_config_change(
    monkeypatch, tmp_path,
):
    _configure_native(monkeypatch, tmp_path)
    old_root = (tmp_path / "OldComfyUI").resolve()
    old_python = (old_root / ".venv" / "bin" / "python").resolve()
    old_main = (old_root / "main.py").resolve()
    old_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", old_python, old_main,
    )
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata_path.parent.mkdir()
    metadata_path.write_text(json.dumps({
        "schema_version": 1,
        "pid": 4321,
        "root": str(old_root),
        "python": str(old_python),
        "main_path": str(old_main),
        "argv": old_argv,
        "start_token": "old-owned-token",
    }), encoding="utf-8")
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(
            pid, old_root, old_python, old_argv, "old-owned-token",
        ),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: frozenset({(4321, "old-owned-token")}),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: False)
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    signals = []
    monkeypatch.setattr(
        comfyui_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is True
    assert signals == [
        (4321, comfyui_module.signal.SIGTERM),
        (4321, comfyui_module.signal.SIGKILL),
    ]
    assert not metadata_path.exists()


@requires_posix_native
def test_comfyui_native_stop_uses_recorded_ownership_when_new_config_is_invalid(
    monkeypatch, tmp_path,
):
    old_root, old_python, old_main = _configure_native(monkeypatch, tmp_path)
    old_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", old_python, old_main,
    )
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    comfyui_module._write_native_pid_metadata(metadata_path, {
        "schema_version": 1,
        "pid": 4321,
        "root": str(old_root),
        "python": str(old_python),
        "main_path": str(old_main),
        "argv": old_argv,
        "start_token": "old-owned-token",
    })
    monkeypatch.setenv("COMFYUI_NATIVE_ROOT", str(tmp_path / "MissingNewComfyUI"))
    monkeypatch.setenv("COMFYUI_NATIVE_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(
            pid, old_root, old_python, old_argv, "old-owned-token",
        ),
    )
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: frozenset({(4321, "old-owned-token")}),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: False)
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    signals = []
    monkeypatch.setattr(
        comfyui_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is True
    assert signals == [
        (4321, comfyui_module.signal.SIGTERM),
        (4321, comfyui_module.signal.SIGKILL),
    ]
    assert not metadata_path.exists()


@requires_posix_native
def test_comfyui_native_stop_kills_verified_group_after_leader_exits(monkeypatch, tmp_path):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    state_dir = tmp_path / ".astra"
    state_dir.mkdir()
    (state_dir / "comfyui-native.json").write_text(json.dumps({
        "schema_version": 1,
        "pid": 4321,
        "root": str(root),
        "python": str(python),
        "main_path": str(main),
        "argv": expected_argv,
        "start_token": "owned-token",
    }), encoding="utf-8")
    identities = iter([
        _owned_identity(4321, root, python, expected_argv),
        None,
    ])
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: next(identities),
    )
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    signals = []
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: signals.append(sig))
    group_snapshots = iter([
        frozenset({(4321, "owned-token"), (5000, "child-token")}),
        frozenset({(5000, "child-token")}),
    ])
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: next(group_snapshots),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: False)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is True
    assert payload["status"] == "stopped"
    assert signals == [comfyui_module.signal.SIGTERM, comfyui_module.signal.SIGKILL]
    assert not (state_dir / "comfyui-native.json").exists()


@requires_posix_native
def test_comfyui_native_stop_refuses_kill_when_group_identity_is_recycled(monkeypatch, tmp_path):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata_path.parent.mkdir()
    metadata_path.write_text(json.dumps({
        "schema_version": 1,
        "pid": 4321,
        "root": str(root),
        "python": str(python),
        "main_path": str(main),
        "argv": expected_argv,
        "start_token": "owned-token",
    }), encoding="utf-8")
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(pid, root, python, expected_argv),
    )
    snapshots = iter([
        frozenset({(4321, "owned-token")}),
        frozenset({(4321, "recycled-token")}),
    ])
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: next(snapshots),
    )
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    signals = []
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(
        comfyui_module,
        "_native_group_exists",
        lambda pid: (_ for _ in ()).throw(AssertionError("recycled group must not be probed as owned")),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    assert payload["status"] == "stop_failed"
    assert signals == [comfyui_module.signal.SIGTERM]
    assert metadata_path.exists()


@pytest.mark.parametrize(
    ("group_remains", "server_online"),
    [(True, False), (False, True)],
    ids=["owned-children-remain", "http-still-online"],
)
@requires_posix_native
def test_comfyui_native_stop_keeps_metadata_when_stop_is_incomplete(
    monkeypatch, tmp_path, group_remains, server_online,
):
    root, python, main = _configure_native(monkeypatch, tmp_path)
    expected_argv = comfyui_module._native_launch_argv(
        "http://127.0.0.1:8188", python, main,
    )
    metadata_path = tmp_path / ".astra" / "comfyui-native.json"
    metadata_path.parent.mkdir()
    metadata_path.write_text(json.dumps({
        "schema_version": 1,
        "pid": 4321,
        "root": str(root),
        "python": str(python),
        "main_path": str(main),
        "argv": expected_argv,
        "start_token": "owned-token",
    }), encoding="utf-8")
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_identity",
        lambda pid: _owned_identity(pid, root, python, expected_argv),
    )
    monkeypatch.setattr(comfyui_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        comfyui_module,
        "_native_process_group_members",
        lambda pid: frozenset({(4321, "owned-token")}),
    )
    monkeypatch.setattr(comfyui_module, "_native_group_exists", lambda pid: group_remains)
    monkeypatch.setattr(comfyui_module.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(
        comfyui_module,
        "_request_json",
        (lambda *args, **kwargs: {"devices": [{"name": "test GPU"}]})
        if server_online
        else (lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))),
    )
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    payload = json.loads(registry.get("comfyui_stop").fn())

    assert payload["success"] is False
    assert payload["status"] == "stop_incomplete"
    assert metadata_path.exists()


def test_build_prompts_adds_preset_base_and_safety_tags():
    positive, negative = _build_prompts(
        "rooftop, selfie",
        preset="starry",
        hair_mode="up",
    )

    assert "1girl, catgirl" in positive
    assert "starry sky" in positive
    assert "hair bun" in positive
    assert "rooftop, selfie" in positive
    assert "nude" in negative
    assert "blue eyes" in negative
    assert "monochrome" in negative


def test_apply_workflow_settings_updates_reference_nodes():
    workflow = {
        "2": {"inputs": {"strength_model": 1.0, "strength_clip": 1.0}},
        "3": {"inputs": {"strength_model": 1.0, "strength_clip": 1.0}},
        "4": {"inputs": {"strength_model": 1.0, "strength_clip": 1.0}},
        "5": {"inputs": {"text": ""}},
        "6": {"inputs": {"text": ""}},
        "8": {"inputs": {"seed": 0, "cfg": 0, "steps": 0}},
        "10": {"inputs": {"width": 0, "height": 0}},
        "11": {"inputs": {"strength_model": 1.0, "strength_clip": 1.0}},
        "99": {"inputs": {"filename_prefix": "old"}},
    }

    updated = _apply_workflow_settings(
        json.loads(json.dumps(workflow)),
        prompt="positive",
        negative_prompt="negative",
        seed=123,
        cfg=4.5,
        steps=28,
        width=896,
        height=1152,
        lora_strength=0.8,
        filename_prefix="agent_output/test",
    )

    assert updated["5"]["inputs"]["text"] == "positive"
    assert updated["6"]["inputs"]["text"] == "negative"
    assert updated["8"]["inputs"]["seed"] == 123
    assert updated["8"]["inputs"]["cfg"] == 4.5
    assert updated["8"]["inputs"]["steps"] == 28
    assert updated["10"]["inputs"]["width"] == 896
    assert updated["10"]["inputs"]["height"] == 1152
    assert updated["2"]["inputs"]["strength_model"] == 0.8
    assert updated["2"]["inputs"]["strength_clip"] == 0.8
    assert updated["3"]["inputs"]["strength_model"] == 0.0
    assert updated["4"]["inputs"]["strength_clip"] == 0.0
    assert updated["11"]["inputs"]["strength_model"] == 0.0
    assert updated["99"]["inputs"]["filename_prefix"] == "agent_output/test"


def test_build_anima_workflow_uses_locked_qwen_stack_and_lora_chain():
    positive, negative, saturation = _build_anima_prompts(
        "white cardigan, sitting by a window, gentle smile",
        profile="signature-muted",
        hair_mode="soft",
    )
    workflow = _build_anima_workflow(
        prompt=positive,
        negative_prompt=negative,
        seed=123,
        width=1024,
        height=1344,
        saturation=saturation,
        model_version="v2",
        filename_prefix="agent_anima/test",
    )

    assert workflow["1"]["inputs"]["unet_name"] == "oneObsessionAnima_v20.safetensors"
    assert workflow["2"]["inputs"] == {
        "clip_name": "qwen_3_06b_base.safetensors",
        "type": "qwen_image",
    }
    assert workflow["3"]["inputs"]["vae_name"] == "qwen_image_vae.safetensors"
    assert workflow["16"]["inputs"]["model"] == ["1", 0]
    assert workflow["17"]["inputs"]["model"] == ["16", 0]
    assert workflow["18"]["inputs"]["strength_model"] == -1.8
    assert workflow["19"]["inputs"]["strength_model"] == 0.8
    assert workflow["7"]["inputs"]["steps"] == 16
    assert workflow["7"]["inputs"]["cfg"] == 1.0
    assert workflow["9"]["inputs"]["filename_prefix"] == "agent_anima/test"
    assert "white cardigan" in workflow["4"]["inputs"]["text"]
    assert "nsfw" in workflow["5"]["inputs"]["text"]


def test_build_anima_prompts_accepts_detailed_prompts_over_880_characters():
    detailed_scene = ", ".join(
        f"scene detail {index} with clothing pose expression and background"
        for index in range(30)
    )

    positive, _, _ = _build_anima_prompts(detailed_scene)

    assert len(detailed_scene) > 880
    assert detailed_scene in positive


def test_anima_draw_submits_once_without_polling(monkeypatch, tmp_path):
    calls = []

    def fake_request(url, payload=None, timeout=30):
        calls.append((url, payload, timeout))
        return {"prompt_id": "12345678-abcd-1234-abcd-1234567890ab"}

    monkeypatch.setattr(comfyui_module, "_request_json", fake_request)
    monkeypatch.setenv("COMFYUI_SERVER", "http://127.0.0.1:8188")
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    import asyncio
    progress = []
    result = asyncio.run(registry.execute("comfyui_anima_draw", {
        "prompt": "white blouse, sitting at an office desk, calm smile",
        "profile": "signature-muted",
        "hair_mode": "hairup",
    }, on_progress=progress.append))
    payload = json.loads(result["output"])

    assert result["error"] == ""
    assert payload["status"] == "queued"
    assert payload["model_version"] == "v2"
    assert payload["prompt_id"] == "12345678-abcd-1234-abcd-1234567890ab"
    assert len(calls) == 1
    assert calls[0][0] == "http://127.0.0.1:8188/prompt"
    assert calls[0][1]["prompt"]["1"]["inputs"]["unet_name"] == "oneObsessionAnima_v20.safetensors"
    assert [item["stage"] for item in progress] == [
        "authorizing", "running", "building_workflow", "submitting", "queued", "finalizing",
    ]


def test_comfyui_result_returns_image_attachment_when_ready(monkeypatch, tmp_path):
    prompt_id = "12345678-abcd-1234-abcd-1234567890ab"
    monkeypatch.setattr(comfyui_module, "_request_json", lambda *args, **kwargs: {
        prompt_id: {
            "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "agent_anima"}]}},
        },
    })
    generated = tmp_path / "downloaded.png"
    generated.write_bytes(b"png")
    monkeypatch.setattr(comfyui_module, "_download_image", lambda *args, **kwargs: generated)
    registry = ToolRegistry()
    register_comfyui_tools(registry, workdir=str(tmp_path))

    import asyncio
    progress = []
    result = asyncio.run(registry.execute(
        "comfyui_result",
        {"prompt_id": prompt_id},
        on_progress=progress.append,
    ))
    payload = json.loads(result["output"])

    assert result["error"] == ""
    assert payload["status"] == "completed"
    assert payload["type"] == "image_attachment"
    assert payload["image_paths"] == [str(generated)]
    stages = [item["stage"] for item in progress]
    assert stages == ["authorizing", "running", "checking", "downloading", "finalizing"]
    assert progress[3]["current"] == 1
    assert progress[3]["total"] == 1
    assert progress[3]["unit"] == "images"


def test_wsl_lifecycle_requires_an_explicit_installation(monkeypatch):
    monkeypatch.delenv("COMFYUI_WSL_ROOT", raising=False)
    with pytest.raises(ValueError, match="Set COMFYUI_WSL_ROOT"):
        comfyui_module._wsl_lifecycle_config()
    monkeypatch.setenv("COMFYUI_WSL_ROOT", "/home/example/comfy/ComfyUI")
    monkeypatch.setenv("COMFYUI_WSL_DISTRO", "Debian")
    assert comfyui_module._wsl_lifecycle_config()[1] == "/home/example/comfy/ComfyUI"
    candidates = [str(path) for path in comfyui_module._workflow_candidates()]
    assert any(path.startswith(r"\\wsl.localhost\Debian\home\example\comfy\ComfyUI") for path in candidates)
    assert any(path.startswith(r"\\wsl$\Debian\home\example\comfy\ComfyUI") for path in candidates)
