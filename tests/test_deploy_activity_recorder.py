"""Exercise deployment ordering and fail-closed checks without touching launchd/TCC."""
import os
from pathlib import Path
import plistlib
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy_activity_recorder.sh"
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="recorder deployment integration requires POSIX bash, executable shebangs and paths",
)


def setup_deploy(tmp_path):
    home = tmp_path / "home"
    dest = home / "Library/Application Support/Astra/bin/AstraActivityRecorder"
    log = home / "Library/Logs/astra-activity-recorder.log"
    log.parent.mkdir(parents=True)
    log.write_text("old accessibility=true\n")
    plist = home / "Library/LaunchAgents/com.astra.activity-recorder.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": "com.astra.activity-recorder", "ProgramArguments": [str(dest)], "StandardErrorPath": str(log)}))
    binaries = tmp_path / "fake-bin"
    binaries.mkdir()
    build = tmp_path / ".build/release"
    build.mkdir(parents=True)
    (build / "AstraActivityRecorder").write_text("new binary")
    fake = '''#!/usr/bin/env python3
import os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
if name == "uname":
    print(os.environ.get("FAKE_UNAME", "Darwin"))
    sys.exit(0)
with open(os.environ["CALLS"], "a") as f: f.write(name + " " + " ".join(sys.argv[1:]) + "\\n")
if name == "swift" and "--show-bin-path" in sys.argv: print(os.environ["BUILD"])
if name == "codesign" and "--display" in sys.argv:
    print("Identifier=" + os.environ.get("IDENTIFIER", "com.astra.activity-recorder"), file=sys.stderr)
if name == "launchctl" and "kickstart" in sys.argv:
    if os.environ.get("ROTATE_LOG"):
        Path(os.environ["LOG"]).rename(os.environ["LOG"] + ".old")
    with open(os.environ["LOG"], "a") as f: f.write("recorder: accessibility=" + os.environ.get("ACCESSIBILITY", "true") + "\\n")
    with open(os.environ["LOG"], "a") as f: f.write("recorder: inputMonitoring=true tapInstalled=true keyboardIncluded=" + os.environ.get("KEYBOARD", "true") + " tapEnabled=true\\n")
if name == "launchctl" and "print" in sys.argv: print("program = " + os.environ.get("STATE_DEST", os.environ["DEST"]))
'''
    for name in ["uname", "swift", "codesign", "launchctl"]:
        path = binaries / name
        path.write_text(fake)
        path.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{binaries}:{os.environ['PATH']}", CALLS=str(tmp_path / "calls"), BUILD=str(build), LOG=str(log), DEST=str(dest))
    return env, dest, plist


def test_deploy_signs_stable_copy_before_reload(tmp_path):
    env, dest, _ = setup_deploy(tmp_path)
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert dest.read_text() == "new binary"
    calls = Path(env["CALLS"]).read_text()
    assert calls.index("--identifier com.astra.activity-recorder") < calls.index("launchctl kickstart")
    assert "Identifier=com.astra.activity-recorder" in result.stdout
    assert "accessibility=true" in result.stdout
    assert not any(".build" in line for line in calls.splitlines() if line.startswith("codesign"))


def test_deploy_rejects_unsupported_host_before_running_commands(tmp_path):
    env, dest, _ = setup_deploy(tmp_path)
    env["FAKE_UNAME"] = "Linux"

    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "Recorder deployment requires macOS." in result.stderr
    assert not dest.exists()
    assert not Path(env["CALLS"]).exists()


def test_deploy_rejects_launch_agent_build_path(tmp_path):
    env, dest, plist = setup_deploy(tmp_path)
    data = plistlib.loads(plist.read_bytes())
    data["ProgramArguments"] = [env["BUILD"] + "/AstraActivityRecorder"]
    plist.write_bytes(plistlib.dumps(data))
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True)
    assert result.returncode != 0
    assert not dest.exists()
    assert not Path(env["CALLS"]).exists()


def test_deploy_rejects_signature_mismatch_before_replace(tmp_path):
    env, dest, _ = setup_deploy(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_text("previous binary")
    env["IDENTIFIER"] = "wrong.identifier"
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True)
    assert result.returncode != 0
    assert dest.read_text() == "previous binary"
    assert "kickstart" not in Path(env["CALLS"]).read_text()


def test_deploy_reports_new_accessibility_failure_despite_old_success(tmp_path):
    env, dest, _ = setup_deploy(tmp_path)
    env["ACCESSIBILITY"] = "false"
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert dest.exists()  # Deployment happened; the live acceptance gate failed.
    assert "Accessibility is unavailable" in result.stderr


def test_deploy_rejects_bin_symlink_into_build(tmp_path):
    env, dest, _ = setup_deploy(tmp_path)
    dest.parent.parent.mkdir(parents=True)
    dest.parent.symlink_to(Path(env["BUILD"]), target_is_directory=True)
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "Refusing deployment" in result.stderr
    assert not Path(env["CALLS"]).exists()


def test_deploy_rejects_replaced_log_as_fresh_evidence(tmp_path):
    env, _, _ = setup_deploy(tmp_path)
    env["ROTATE_LOG"] = "1"
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "log changed identity" in result.stderr


def test_deploy_rejects_loaded_service_with_path_suffix_before_replace(tmp_path):
    env, dest, _ = setup_deploy(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_text("previous binary")
    env["STATE_DEST"] = str(dest) + ".old"
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "exact stable executable path" in result.stderr
    assert dest.read_text() == "previous binary"
    calls = Path(env["CALLS"]).read_text()
    assert "swift" not in calls
    assert "kickstart" not in calls


def test_deploy_rejects_partial_tap_despite_accessibility(tmp_path):
    env, _, _ = setup_deploy(tmp_path)
    env["KEYBOARD"] = "false"
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "healthy keyboard event tap" in result.stderr
