from __future__ import annotations

import os
import platform
import stat
import subprocess
from pathlib import Path

import pytest

from .fixtures import E2E_SWITCH, PRODUCTION_HELPER, create_minimal_docx


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_macos_computer: explicit interactive macOS Computer Use acceptance",
    )


def pytest_collection_modifyitems(config, items):
    del config
    if os.getenv(E2E_SWITCH) == "1":
        return
    reason = f"real macOS Computer Use NOT_RUN; set {E2E_SWITCH}=1 in an authorized desktop"
    for item in items:
        if item.get_closest_marker("real_macos_computer") is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture
def computer_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "astra-computer-e2e"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "finder-source.txt").write_text("ASTRA_FINDER_SOURCE", encoding="utf-8")
    os.chmod(root / "finder-source.txt", 0o600)
    create_minimal_docx(root / "wps-source.docx", marker="ASTRA_WPS_ORIGINAL")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    return root


@pytest.fixture(scope="session", autouse=True)
def require_real_macos_preconditions(request):
    if os.getenv(E2E_SWITCH) != "1":
        return
    if platform.system() != "Darwin":
        pytest.fail("BLOCKED_REAL_E2E: host is not macOS", pytrace=False)
    if not PRODUCTION_HELPER.is_file():
        pytest.fail(
            "BLOCKED_REAL_E2E: signed production helper is missing; "
            "run bash scripts/build_macos_computer_helper.sh",
            pytrace=False,
        )
    status = subprocess.run(
        [PRODUCTION_HELPER],
        input='{"protocol_version":4,"request_id":"e2e-preflight","operation":"status","payload":{}}\n',
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if status.returncode != 0 or '"accessibility":true' not in status.stdout or '"screen_recording":true' not in status.stdout:
        pytest.fail(
            "BLOCKED_REAL_E2E: Accessibility and Screen Recording must already be granted "
            f"to the signed helper; nonprompt status={status.stdout.strip()!r}",
            pytrace=False,
        )
    cg = subprocess.run(
        ["swift", "-e", 'import CoreGraphics; print(CGPreflightPostEventAccess() ? "true" : "false")'],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if cg.returncode != 0 or cg.stdout.strip() != "true":
        pytest.fail(
            "BLOCKED_REAL_E2E: CGEvent posting access is unavailable; "
            f"nonprompt preflight={cg.stdout.strip()!r} {cg.stderr.strip()!r}",
            pytrace=False,
        )
