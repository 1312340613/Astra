"""Real Swift/Node/Python composition, isolated from installed helper and user data."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="native Appshot socket/identity fixture requires macOS")
def test_real_broker_draft_admission_provider_and_cleanup():
    root = Path(__file__).resolve().parents[1]
    # Cold CI runners fetch and compile Swift dependencies before the fixture
    # can run. Keep that preparation separate from its bounded runtime check.
    build = subprocess.run(
        ["swift", "build", "--package-path", str(root / "native/macos-computer-helper"),
         "--build-tests", "--disable-automatic-resolution"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(root / "scripts/test_appshot_e2e.sh"), "--skip-build"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=150,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test" in result.stdout + result.stderr
    assert "Appshot cross-process:" in result.stdout + result.stderr
