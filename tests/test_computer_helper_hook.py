"""The Computer Use helper pre-commit hook must compile-check, never re-install.

Replacing ``.astra/bin/AstraMacComputerHelper.app`` changes its cdhash, which silently
invalidates the macOS Screen Recording TCC grant for a live helper. A commit must never
cost the operator a manual permission re-grant, so the hook is only allowed to prove the
release build compiles.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = PROJECT_ROOT / "scripts" / "git-hooks" / "pre-commit"
LIVE_BUNDLE = PROJECT_ROOT / ".astra" / "bin" / "AstraMacComputerHelper.app"


def _bundle_state() -> tuple[float, float] | None:
    executable = LIVE_BUNDLE / "Contents" / "MacOS" / "AstraMacComputerHelper"
    if not executable.exists():
        return None
    stat = executable.stat()
    return (stat.st_mtime_ns, stat.st_size)


def test_hook_is_tracked_in_the_repository() -> None:
    assert HOOK_PATH.is_file(), "the helper hook must live in the repository, not .git/hooks"


@pytest.mark.skipif(os.name != "posix", reason="POSIX Git hook executable mode is not a Windows file contract")
def test_hook_is_executable_on_posix() -> None:
    assert os.access(HOOK_PATH, os.X_OK), "git requires the hook to be executable"


def test_hook_does_not_invoke_the_installing_build_script() -> None:
    # Comments may name the installer to explain why it is forbidden; only executable
    # lines decide whether the hook actually swaps the live bundle.
    commands = "\n".join(
        line
        for line in HOOK_PATH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "build_macos_computer_helper.sh" not in commands, (
        "the pre-commit hook must not run the script that swaps the live helper bundle"
    )


def test_hook_compile_checks_swift_changes_in_release_mode() -> None:
    body = HOOK_PATH.read_text(encoding="utf-8")
    assert "swift build" in body and "-c release" in body, (
        "the hook still has to prove release-mode compilation for helper changes"
    )


@pytest.mark.skipif(os.name != "posix", reason="native helper hook execution requires POSIX bash")
def test_running_the_hook_never_touches_the_live_helper_bundle() -> None:
    before = _bundle_state()
    environment = dict(os.environ, GIT_DIR=str(PROJECT_ROOT / ".git"))
    completed = subprocess.run(
        ["bash", str(HOOK_PATH)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert _bundle_state() == before, "an empty commit must not rewrite the live helper binary"


def _appshot_test_binary() -> Path:
    import sys
    import pytest

    if sys.platform != "darwin":
        pytest.skip("native Appshot CLI is macOS-only")
    binary = PROJECT_ROOT / "native/macos-computer-helper/.build/debug/AstraMacComputerHelper"
    if not binary.is_file():
        pytest.skip("build native helper before CLI integration tests")
    return binary


def test_appshot_identity_cli_reports_only_actual_parent_and_native_clock() -> None:
    import json

    binary = _appshot_test_binary()
    first = subprocess.run([str(binary), "--appshot-client-identity"], input="",
                           capture_output=True, text=True, timeout=5)
    assert first.returncode == 0 and first.stderr == ""
    data = json.loads(first.stdout)
    assert set(data) == {"pid", "uid", "process_start", "monotonic_ns"}
    assert data["pid"] == os.getpid() and data["uid"] == os.getuid()
    for key in ("process_start", "monotonic_ns"):
        assert isinstance(data[key], str) and data[key].isascii() and data[key].isdecimal()
        assert str(int(data[key])) == data[key] and 0 < int(data[key]) < 2**64
    assert len(first.stdout.encode()) < 256
    second = subprocess.run([str(binary), "--appshot-client-identity"], input="",
                            capture_output=True, text=True, timeout=5)
    later = json.loads(second.stdout)
    assert later["process_start"] == data["process_start"]
    assert int(later["monotonic_ns"]) >= int(data["monotonic_ns"])


def test_appshot_cli_rejects_extra_and_malicious_arguments_without_dispatch() -> None:
    binary = _appshot_test_binary()
    for args in (("--appshot-daemon", "extra"), ("--appshot-client-identity", "--capture"),
                 ("--appshot-broker-identity", "123"), ("--appshot-broker-identity", "/tmp/other"),
                 ("--appshot-daemon=true",), ("--capture",), ("$(touch /tmp/forbidden)",)):
        result = subprocess.run([str(binary), *args], input="private source text\n",
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 64
        assert result.stdout == "" and result.stderr == "unsupported helper mode\n"
    result = subprocess.run([str(binary)], input="", capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout == ""


def test_appshot_error_fixture_matches_native_vocabulary() -> None:
    import json
    import re

    native = PROJECT_ROOT / "native/macos-computer-helper"
    codes = json.loads((native / "Tests/Fixtures/appshot_error_codes_v1.json").read_text())
    source = (native / "Sources/AstraMacComputerHelper/AppshotHUD.swift").read_text()
    declared = re.findall(r'^  case \w+ = "([a-z_]+)"', source, re.MULTILINE)
    assert len(codes) == len(set(codes)) and set(codes) == set(declared)
    assert {"backend_busy", "submission_unknown", "settings_invalid", "settings_write_failed",
            "capture_timeout", "capture_cancelled", "protocol_invalid"} <= set(codes)
    assert "ok" not in codes and "settings_failed" not in codes


def test_appshot_hud_and_build_preserve_nonactivating_helper() -> None:
    native = PROJECT_ROOT / "native/macos-computer-helper/Sources/AstraMacComputerHelper"
    hud = (native / "AppshotHUD.swift").read_text()
    assert "[.borderless, .nonactivatingPanel]" in hud
    assert "override var canBecomeKey: Bool { false }" in hud
    assert "override var canBecomeMain: Bool { false }" in hud
    assert "panel.hidesOnDeactivate = false" in hud
    assert "panel.becomesKeyOnlyIfNeeded = false" in hud
    assert "panel.orderFrontRegardless()" in hud
    assert ".activate(" not in hud and "localizedDescription" not in hud
    script = (PROJECT_ROOT / "scripts/build_macos_computer_helper.sh").read_text()
    assert "--appshot-daemon" in script and "--appshot-client-identity" in script
    assert "plutil -extract LSUIElement raw" in script
    assert 'codesign --verify --deep --strict "$staging_bundle"' in script


def test_appshot_broker_identity_missing_runtime_is_read_only() -> None:
    import pytest

    binary = _appshot_test_binary()
    runtime = Path(f"/private/tmp/astra-appshot-{os.getuid()}")
    if runtime.exists() or runtime.is_symlink():
        pytest.skip("existing runtime belongs to the user; fixture native tests cover discovery")
    result = subprocess.run([str(binary), "--appshot-broker-identity"],
                            input="", capture_output=True, text=True, timeout=5)
    assert result.returncode == 69
    assert result.stdout == "" and result.stderr == "broker_unavailable\n"
    assert not runtime.exists() and not runtime.is_symlink()


def test_bundle_asserts_read_only_broker_identity_mode() -> None:
    script = (PROJECT_ROOT / "scripts/build_macos_computer_helper.sh").read_text()
    assert "'--appshot-broker-identity'" in script
