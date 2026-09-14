from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from agent.runtime.computer_backend import ComputerSessionError
from agent.runtime.computer_protocol import ComputerSnapshotTextDetailMode

from . import fixtures as computer_fixtures
from .fixtures import CooperativeMatrixRecord, accepted_compatibility_entries

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HARNESS_SOURCE = (
    PROJECT_ROOT
    / "native/macos-computer-helper/Tests/AstraMacComputerE2EHarness/main.swift"
)
PACKAGE_MANIFEST = PROJECT_ROOT / "native/macos-computer-helper/Package.swift"
PACKAGE_RESOLUTION = PROJECT_ROOT / "native/macos-computer-helper/Package.resolved"
SWIFT_TEST_MAIN = (
    PROJECT_ROOT
    / "native/macos-computer-helper/Tests/AstraMacComputerHelperTests/Main.swift"
)
HELPER_HARNESS_SOURCE = (
    PROJECT_ROOT
    / "native/macos-computer-helper/Tests/AstraMacComputerHelperHarness/main.swift"
)
FIXTURE_SOURCE = (
    PROJECT_ROOT
    / "native/macos-computer-helper/Sources/AstraComputerFixture/main.swift"
)


def deterministic_record(**changes: object) -> CooperativeMatrixRecord:
    values: dict[str, object] = {
        "bundle_id": "com.example.fixture",
        "app_version": "1",
        "action": "click",
        "cursor_before": (100.0, 100.0),
        "cursor_after": (100.0, 100.0),
        "exact_window": True,
        "fresh_effect": True,
        "outside_side_effects": False,
        "held_input_clean": True,
    }
    values.update(changes)
    return CooperativeMatrixRecord(**values)


def real_record(**changes: object) -> CooperativeMatrixRecord:
    values: dict[str, object] = {
        "bundle_id": "com.example.fixture",
        "app_version": "1.0.0",
        "action": "drag",
        "cursor_before": (100.0, 100.0),
        "cursor_after": (100.0, 100.0),
        "exact_window": True,
        "fresh_effect": True,
        "outside_side_effects": False,
        "held_input_clean": True,
        "evidence_source": "real",
        "target_pid": 222,
        "sentinel_pid": 111,
        "frontmost_before": 111,
        "frontmost_during": 222,
        "frontmost_after": 111,
        "receiving_window": "window-9",
    }
    values.update(changes)
    return CooperativeMatrixRecord(**values)


def test_deterministic_matrix_record_accepts_only_complete_evidence() -> None:
    assert deterministic_record().accepted is True
    assert deterministic_record().accepted_actions == frozenset({"click"})

    failures = [
        {"cursor_before": (math.nan, 100.0)},
        {"cursor_after": (101.0, 100.0)},
        {"exact_window": False},
        {"fresh_effect": False},
        {"outside_side_effects": True},
        {"held_input_clean": False},
    ]
    for fields in failures:
        record = deterministic_record(**fields)
        assert record.accepted is False, fields
        assert record.rejection_reasons, fields
        assert record.accepted_actions == frozenset(), fields


def test_real_matrix_record_requires_exact_frontmost_and_receiving_window_evidence() -> None:
    assert real_record().accepted is True
    failures = [
        {"frontmost_before": None},
        {"frontmost_during": 111},
        {"frontmost_after": 222},
        {"receiving_window": ""},
        {"target_pid": 0},
        {"sentinel_pid": 222},
    ]
    for fields in failures:
        record = real_record(**fields)
        assert record.accepted is False, fields
        assert record.rejection_reasons, fields


def test_matrix_record_round_trip_is_strict_and_preserves_evidence() -> None:
    record = real_record()

    decoded = CooperativeMatrixRecord.from_mapping(record.to_mapping())

    assert decoded == record
    assert decoded.accepted
    with pytest.raises(ValueError, match="fields"):
        CooperativeMatrixRecord.from_mapping({**record.to_mapping(), "unexpected": True})


def test_compatibility_entries_copy_only_exact_accepted_cells_without_inference() -> None:
    records = [
        real_record(action="click"),
        real_record(action="scroll"),
        real_record(action="drag", fresh_effect=False),
        real_record(bundle_id="com.example.chromium", app_version="140.0", action="drag"),
        real_record(bundle_id="com.example.chromium", app_version="141.0", action="double_click"),
    ]

    assert accepted_compatibility_entries(records) == [
        {
            "bundle_id": "com.example.chromium",
            "app_version": "140.0",
            "capabilities": [{
                "backend": "pid_pointer",
                "enabled_actions": ["drag"],
            }],
        },
        {
            "bundle_id": "com.example.chromium",
            "app_version": "141.0",
            "capabilities": [{
                "backend": "pid_pointer",
                "enabled_actions": ["double_click"],
            }],
        },
        {
            "bundle_id": "com.example.fixture",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "pid_pointer",
                "enabled_actions": ["click", "scroll"],
            }],
        },
    ]


def test_no_accepted_record_produces_an_empty_application_registry() -> None:
    assert accepted_compatibility_entries([
        real_record(action="click", cursor_after=(101.0, 100.0)),
        real_record(action="scroll", outside_side_effects=True),
    ]) == []


def test_checked_in_compatibility_registry_matches_head() -> None:
    # The production registry is allowed to carry deliberately approved cells
    # (committed to HEAD); what it must never contain is uncommitted test
    # contamination. Compare against the checked-in version exactly.
    registry = json.loads(
        (PROJECT_ROOT / "config/macos_computer_compatibility.json").read_text(encoding="utf-8")
    )
    head_text = subprocess.check_output(
        [
            "git", "-C", str(PROJECT_ROOT),
            "show", "HEAD:config/macos_computer_compatibility.json",
        ],
        text=True,
    )
    head_registry = json.loads(head_text)

    assert registry == head_registry


def test_synthetic_capability_gate_precedes_dispatcher_plan_storage() -> None:
    dispatcher = (PROJECT_ROOT / "native/macos-computer-helper/Sources/AstraMacComputerHelper/InputDispatcher.swift").read_text(encoding="utf-8")
    windows = (PROJECT_ROOT / "native/macos-computer-helper/Sources/AstraMacComputerHelper/Windows.swift").read_text(encoding="utf-8")
    helper_main = (PROJECT_ROOT / "native/macos-computer-helper/Sources/AstraMacComputerHelper/HelperMain.swift").read_text(encoding="utf-8")

    gate = dispatcher.index("syntheticPolicy.allows(")
    storage = dispatcher.index("records[reference]", gate)
    assert gate < storage
    assert "PIDPointerPlanningPolicy(" not in windows
    assert "syntheticPolicy: SyntheticInputPlanningPolicy" in dispatcher
    assert "application: PIDTargetApplication?" in dispatcher
    # Released capability in production (registry-gated); experimental must
    # never appear in the production helper composition.
    assert "pidPointerCapability: .available" in helper_main
    assert ".experimentalAvailable" not in helper_main


def test_release_harness_defaults_to_ax_and_pointer_fail_closed() -> None:
    source = HARNESS_SOURCE.read_text(encoding="utf-8")
    assert "ASTRA_MACOS_COMPUTER_PID_POINTER_EXPERIMENT" in source
    assert "PASS AX-first/fail-closed" in source
    assert "pidPointerCapability: .experimentalAvailable" in source
    assert "pidPointerCapability: .unavailable" in source


def test_production_composition_never_enables_experimental_pointer_capability() -> None:
    # Production helper ships the *released* pidPointer capability (.available,
    # gated by the compatibility registry); the experimental variant must
    # never leak into production composition.
    source = (PROJECT_ROOT / "native/macos-computer-helper/Sources/AstraMacComputerHelper/HelperMain.swift").read_text(encoding="utf-8")
    assert "pidPointerCapability: .available" in source
    assert ".experimentalAvailable" not in source


def test_native_e2e_harness_uses_only_cooperative_protocol_and_emits_complete_records() -> None:
    source = HARNESS_SOURCE.read_text(encoding="utf-8")

    assert "observer.focus(" not in source
    assert "observer.act(" not in source
    for operation in (
        "planActions(",
        "takeoverBegin(",
        "cooperativeAct(",
        "takeoverEnd(",
    ):
        assert operation in source
    for evidence in (
        "cursor_before",
        "cursor_after",
        "frontmost_before",
        "frontmost_during",
        "frontmost_after",
        "receiving_window",
        "fresh_effect",
        "outside_side_effects",
        "held_input_clean",
    ):
        assert evidence in source
    assert '"/usr/bin/open"' in source
    assert 'arguments: [fixtureApp.path]' in source
    assert 'arguments: ["-n"' not in source
    assert '"/usr/bin/codesign", "--force", "--sign", "-"' in source
    assert '"/usr/bin/codesign", "--verify", "--deep", "--strict"' in source
    assert "CommandLine.arguments[0]" not in source
    assert 'appendingPathComponent("AstraMacComputerE2EHarness")' in source
    assert 'appendingPathComponent("AstraVirtualCursorSidecar")' in source
    assert "isExecutableFile(atPath:" in source


def test_real_e2e_scripts_split_release_acceptance_from_pointer_experiment() -> None:
    release = (PROJECT_ROOT / "scripts/test_macos_computer_actions_e2e.sh").read_text(encoding="utf-8")
    experiment = (PROJECT_ROOT / "scripts/test_macos_computer_pid_pointer_experimental.sh").read_text(encoding="utf-8")

    assert "PASS AX-first/fail-closed" in release
    assert "ASTRA_COOPERATIVE_MATRIX_RECORD" not in release
    assert "accepted_compatibility_entries" not in release
    assert "ASTRA_MACOS_COMPUTER_PID_POINTER_EXPERIMENT" in experiment
    assert "ASTRA_MACOS_COMPUTER_E2E" in experiment


def test_swiftpm_declares_the_native_swift_suite_as_a_real_test_target() -> None:
    manifest = PACKAGE_MANIFEST.read_text(encoding="utf-8")
    resolution = PACKAGE_RESOLUTION.read_text(encoding="utf-8")

    assert re.search(
        r'\.testTarget\(\s*name:\s*"AstraMacComputerSwiftTests"',
        manifest,
    )
    assert not re.search(
        r'\.executableTarget\(\s*name:\s*"AstraMacComputerSwiftTests"',
        manifest,
    )
    assert manifest.count(
        '.product(name: "Testing", package: "swift-testing")'
    ) == 1
    assert re.search(
        r'url:\s*"https://github\.com/swiftlang/swift-testing\.git",\s*'
        r'exact:\s*"6\.3\.2"',
        manifest,
    )
    assert "linkerSettings: testingLinkerSettings" in manifest
    assert '"identity" : "swift-testing"' in resolution
    assert '"version" : "6.3.2"' in resolution
    assert not SWIFT_TEST_MAIN.exists()


def test_swiftpm_excludes_the_native_helper_harness_without_its_test_switch() -> None:
    manifest = PACKAGE_MANIFEST.read_text(encoding="utf-8")
    helper_script = (
        PROJECT_ROOT / "scripts/test_macos_computer_helper.sh"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'let includeHelperHarness\s*=\s*ProcessInfo\.processInfo\.environment'
        r'\["ASTRA_MACOS_COMPUTER_HELPER_HARNESS"\]\s*==\s*"1"',
        manifest,
    )
    assert re.search(
        r'if includeHelperHarness\s*\{\s*targets\.append\(\.executableTarget\('
        r'\s*name:\s*"AstraMacComputerHelperHarness"',
        manifest,
    )
    assert re.search(
        r'ASTRA_MACOS_COMPUTER_HELPER_HARNESS=1(?:\s|\\)+swift run\s+'
        r'--package-path\s+"\$package_root"\s+AstraMacComputerHelperHarness',
        helper_script,
    )


def test_swiftpm_exposes_the_e2e_harness_only_with_explicit_authorization() -> None:
    manifest = PACKAGE_MANIFEST.read_text(encoding="utf-8")
    release_script = (
        PROJECT_ROOT / "scripts/test_macos_computer_actions_e2e.sh"
    ).read_text(encoding="utf-8")
    experiment_script = (
        PROJECT_ROOT / "scripts/test_macos_computer_pid_pointer_experimental.sh"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'let includeE2EHarness\s*=\s*ProcessInfo\.processInfo\.environment'
        r'\["ASTRA_MACOS_COMPUTER_E2E"\]\s*==\s*"1"',
        manifest,
    )
    assert re.search(
        r'if includeE2EHarness\s*\{\s*targets\.append\(\.executableTarget\('
        r'\s*name:\s*"AstraMacComputerE2EHarness"',
        manifest,
    )
    for script in (release_script, experiment_script):
        assert re.search(
            r'ASTRA_MACOS_COMPUTER_E2E=1(?:\s+'
            r'ASTRA_MACOS_COMPUTER_PID_POINTER_EXPERIMENT=1)?(?:\s|\\)+swift run\s+'
            r'--package-path',
            script,
        )


def test_real_e2e_preflight_uses_only_current_protocol_version() -> None:
    source = (Path(__file__).with_name("conftest.py")).read_text(encoding="utf-8")

    assert '"protocol_version":4' in source
    assert '"protocol_version":1' not in source


def test_get_app_state_native_harness_covers_background_fixture_without_input_authority() -> None:
    harness = HELPER_HARNESS_SOURCE.read_text(encoding="utf-8")
    fixture = FIXTURE_SOURCE.read_text(encoding="utf-8")

    assert 'setAccessibilityIdentifier("astra.app_state_marker")' in fixture
    assert '"app-state:primary"' in fixture
    start = harness.index("private func testDeterministicBackgroundFixtureGetAppState")
    end = harness.index("private func test", start + 1)
    scenario = harness[start:end]
    assert "NSWorkspace.shared.frontmostApplication?.processIdentifier" in scenario
    assert "CGEvent(source: nil)" in scenario
    assert '"operation":"apps"' in scenario
    assert r'\"operation\":\"get_app_state\"' in harness
    assert "target_gone" in scenario
    assert "stale_target" in scenario
    assert "activate(" not in scenario
    assert "cooperativeAct(" not in scenario
    assert "planActions(" not in scenario
    fixture_observer_start = harness.index(
        "private final class DeterministicFixtureAppStateObserver"
    )
    fixture_observer_end = harness.index(
        "private final class HarnessSecureValueProbe",
        fixture_observer_start,
    )
    fixture_observer = harness[fixture_observer_start:fixture_observer_end]
    assert ".resolveCatalogTarget(" in fixture_observer
    assert "completeSnapshotCaptureTransaction(" in fixture_observer
    assert "SnapshotReferenceRegistry()" in fixture_observer
    assert ".contextForPlanning(snapshotID:" in fixture_observer
    assert "InputDispatcher(" in fixture_observer

    fixture_support = Path(__file__).with_name("fixtures.py").read_text(
        encoding="utf-8"
    )
    backend_start = fixture_support.index("class DeterministicGetAppStateBackend")
    backend_end = fixture_support.index("class DeterministicGetAppStateHarness")
    python_backend = fixture_support[backend_start:backend_end]
    assert 'ComputerSessionError("target_gone"' not in python_backend
    assert 'ComputerSessionError("stale_target"' not in python_backend


@pytest.mark.skipif(os.name != 'posix', reason='deterministic native harness uses POSIX held-directory cache leases')
def test_get_app_state_deterministic_harness_always_closes_on_failure(
    tmp_path: Path,
) -> None:
    harness = computer_fixtures.DeterministicGetAppStateHarness(tmp_path)
    session_dir = harness.manager.session_dir

    with pytest.raises(RuntimeError, match="forced assertion"):
        try:
            raise RuntimeError("forced assertion")
        finally:
            computer_fixtures.run(harness.manager.close())

    assert not session_dir.exists()
    assert harness.backend.calls[-1] == ("close", None)


@pytest.mark.skipif(os.name != 'posix', reason='deterministic native harness uses POSIX held-directory cache leases')
def test_get_app_state_deterministic_fixture_transaction_preserves_background_state(
    tmp_path: Path,
) -> None:
    harness = computer_fixtures.DeterministicGetAppStateHarness(tmp_path)
    try:
        catalog = computer_fixtures.run(harness.manager.apps())
        app_ref = str(catalog[0]["app_ref"])
        window_ref = str(catalog[0]["windows"][0]["window_ref"])
        sentinel_before = harness.backend.frontmost_pid
        cursor_before = harness.backend.cursor_position

        state = computer_fixtures.run(
            harness.manager.get_app_state(app_ref, window_ref)
        )

        assert state.target.app_ref == app_ref
        assert state.target.window_ref == window_ref
        assert state.image_data.startswith(b"\x89PNG\r\n\x1a\n")
        assert state.snapshot.snapshot_id.startswith("fixture-state-")
        assert state.snapshot.payload["ax_tree"] == {
            "role": "AXWindow",
            "subrole": "AXStandardWindow",
            "title": "Astra Computer Fixture",
            "identifier": "astra.window",
            "element_ref": f"{state.snapshot.snapshot_id}:0",
            "children": [{
                "role": "AXStaticText",
                "subrole": "AXText",
                "label": "App state transaction marker",
                "value": "app-state:primary",
                "identifier": "astra.app_state_marker",
                "element_ref": f"{state.snapshot.snapshot_id}:1",
            }],
        }
        assert harness.backend.frontmost_pid == sentinel_before
        assert harness.backend.cursor_position == cursor_before
        assert harness.backend.activation_calls == []
        assert harness.backend.input_calls == []
        assert [name for name, _details in harness.backend.calls] == [
            "bind_artifact_directory",
            "apps",
            "get_app_state",
        ]
    finally:
        computer_fixtures.run(harness.manager.close())


@pytest.mark.skipif(os.name != 'posix', reason='deterministic native harness uses POSIX held-directory cache leases')
def test_get_app_state_old_refs_fail_closed_without_selecting_replacement(
    tmp_path: Path,
) -> None:
    harness = computer_fixtures.DeterministicGetAppStateHarness(tmp_path)
    try:
        catalog = computer_fixtures.run(harness.manager.apps())
        app_ref = str(catalog[0]["app_ref"])
        window_ref = str(catalog[0]["windows"][0]["window_ref"])
        refreshed = computer_fixtures.run(harness.manager.apps())
        assert refreshed[0]["windows"][0]["title"] == "Astra Computer Fixture"
        assert refreshed[0]["windows"][0]["window_ref"] != window_ref
        calls_before = len(harness.backend.calls)
        with pytest.raises(ComputerSessionError) as captured:
            computer_fixtures.run(harness.manager.get_app_state(app_ref, window_ref))

        assert captured.value.code == "catalog_required"
        later_calls = harness.backend.calls[calls_before:]
        assert not any(name == "select" for name, _details in harness.backend.calls)
        assert later_calls == []
    finally:
        computer_fixtures.run(harness.manager.close())


@pytest.mark.skipif(os.name != 'posix', reason='deterministic native harness uses POSIX held-directory cache leases')
def test_get_app_state_smart_pair_has_stable_authority_and_close_cleanup(
    tmp_path: Path,
) -> None:
    harness = computer_fixtures.DeterministicGetAppStateHarness(tmp_path)
    image: Path | None = None
    detail: Path | None = None
    session_dir = harness.manager.session_dir
    try:
        catalog = computer_fixtures.run(harness.manager.apps())
        app_ref = str(catalog[0]["app_ref"])
        window_ref = str(catalog[0]["windows"][0]["window_ref"])

        state = computer_fixtures.run(harness.manager.get_app_state(
            app_ref,
            window_ref,
            text_detail=ComputerSnapshotTextDetailMode.ON,
        ))
        image = state.image_path
        detail = Path(str(state.snapshot.payload["text_detail_path"]))
        image_stat_before = image.stat()
        detail_stat_before = detail.stat()
        metadata = state.snapshot.payload["text_detail_metadata"]
        detail_bytes = detail.read_bytes()

        assert image.name.removesuffix(".png") == detail.name.removesuffix(".ax.json")
        assert stat.S_IMODE(image_stat_before.st_mode) == 0o600
        assert stat.S_IMODE(detail_stat_before.st_mode) == 0o600
        assert image_stat_before.st_uid == os.getuid()
        assert detail_stat_before.st_uid == os.getuid()
        assert image_stat_before.st_nlink == 1
        assert detail_stat_before.st_nlink == 1
        assert (image_stat_before.st_dev, image_stat_before.st_ino) == state.image_identity
        assert (image_stat_before.st_dev, image_stat_before.st_ino) != (
            detail_stat_before.st_dev,
            detail_stat_before.st_ino,
        )
        def stable_authority(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                stat.S_IMODE(value.st_mode),
                value.st_uid,
                value.st_nlink,
                value.st_size,
            )
        assert stable_authority(image.stat()) == stable_authority(image_stat_before)
        assert stable_authority(detail.stat()) == stable_authority(detail_stat_before)
        assert metadata["snapshot_id"] == state.snapshot.snapshot_id
        assert metadata["byte_count"] == len(detail_bytes)
        assert metadata["sha256"] == hashlib.sha256(detail_bytes).hexdigest()
        assert state.image_sha256 == hashlib.sha256(state.image_data).hexdigest()
        assert json.loads(detail_bytes)["snapshot_id"] == state.snapshot.snapshot_id
    finally:
        computer_fixtures.run(harness.manager.close())

    assert image is not None and not image.exists()
    assert detail is not None and not detail.exists()
    assert not session_dir.exists()
    assert harness.backend.calls[-1] == ("close", None)
