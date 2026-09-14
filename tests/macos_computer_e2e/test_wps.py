from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from .fixtures import (
    ComputerHarness,
    action_result_metadata,
    approval_path_is_within_workspace,
    create_minimal_docx,
    extract_docx_text,
    extract_pdf_text,
    find_nodes,
    first_node,
    open_application,
    retry_preinput_stale_once,
    run,
    wait_for_path,
)


def select_wps_editor_canvas(ax_tree: dict, capture_bounds: dict) -> dict:
    """Return one trusted WPS document canvas from the current window snapshot."""
    primary = find_nodes(
        ax_tree,
        lambda node: node.get("role") in {"AXSplitGroup", "AXTextArea"}
        and isinstance(node.get("bounds"), dict),
    )
    if primary:
        return max(
            primary,
            key=lambda node: node["bounds"]["width"] * node["bounds"]["height"],
        )

    required = {"x", "y", "width", "height"}
    if not required.issubset(capture_bounds):
        raise AssertionError("WPS snapshot capture bounds are missing")
    window_x = float(capture_bounds["x"])
    window_y = float(capture_bounds["y"])
    window_width = float(capture_bounds["width"])
    window_height = float(capture_bounds["height"])
    if window_width <= 0 or window_height <= 0:
        raise AssertionError("WPS snapshot capture bounds are invalid")

    def is_large_contained_group(node: dict) -> bool:
        if node.get("role") != "AXGroup" or not isinstance(node.get("bounds"), dict):
            return False
        bounds = node["bounds"]
        if not required.issubset(bounds):
            return False
        x = float(bounds["x"])
        y = float(bounds["y"])
        width = float(bounds["width"])
        height = float(bounds["height"])
        return (
            width >= window_width * 0.8
            and height >= window_height * 0.5
            and x >= window_x
            and y >= window_y
            and x + width <= window_x + window_width
            and y + height <= window_y + window_height
        )

    fallback = find_nodes(ax_tree, is_large_contained_group)
    if len(fallback) != 1:
        raise AssertionError(
            "WPS editor canvas requires one unique large contained AXGroup fallback"
        )
    return fallback[0]


def wps_editor_focus_action(editor: dict) -> dict[str, object]:
    bounds = editor["bounds"]
    return {
        "type": "click",
        "x": bounds["x"] + bounds["width"] / 2,
        "y": bounds["y"] + min(140, bounds["height"] / 3),
        "target_element_ref": editor["element_ref"],
    }


def wps_center_click_action(control: dict) -> dict[str, object]:
    bounds = control["bounds"]
    return {
        "type": "click",
        "x": bounds["x"] + bounds["width"] / 2,
        "y": bounds["y"] + bounds["height"] / 2,
        "target_element_ref": control["element_ref"],
    }


def wps_editor_replacement_actions(value: str) -> list[dict[str, object]]:
    if not value or "\n" in value or "\r" in value:
        raise AssertionError("WPS editor replacement must be one non-empty line")
    return [{"type": "type", "text": value}]


def wps_modal_transition_actions(key: str, modifiers: list[str]) -> list[dict[str, object]]:
    return [{"type": "keypress", "key": key, "modifiers": modifiers}]


def test_wps_editor_canvas_accepts_current_large_axgroup_and_rejects_ambiguous_groups() -> None:
    capture_bounds = {"x": 0, "y": 0, "width": 1512, "height": 860}
    current_wps_tree = {
        "children": [
            {
                "role": "AXGroup",
                "bounds": {"x": 0, "y": 151, "width": 1512, "height": 683},
                "element_ref": "editor",
            },
            {
                "role": "AXGroup",
                "bounds": {"x": 12, "y": 79, "width": 1488, "height": 69},
                "element_ref": "toolbar",
            },
        ]
    }
    assert select_wps_editor_canvas(current_wps_tree, capture_bounds)["element_ref"] == "editor"

    ambiguous = {
        "children": [
            {
                "role": "AXGroup",
                "bounds": {"x": 0, "y": 150, "width": 1512, "height": 680},
                "element_ref": "first",
            },
            {
                "role": "AXGroup",
                "bounds": {"x": 0, "y": 151, "width": 1512, "height": 679},
                "element_ref": "second",
            },
        ]
    }
    with pytest.raises(AssertionError, match="unique"):
        select_wps_editor_canvas(ambiguous, capture_bounds)


def test_wps_editor_focus_action_binds_coordinate_to_canvas_safe_region() -> None:
    editor = {
        "bounds": {"x": 0, "y": 151, "width": 1512, "height": 683},
        "element_ref": "snapshot:canvas",
    }

    assert wps_editor_focus_action(editor) == {
        "type": "click",
        "x": 756,
        "y": 291,
        "target_element_ref": "snapshot:canvas",
    }


def test_wps_center_click_action_binds_coordinate_to_exact_control_safe_region() -> None:
    control = {
        "bounds": {"x": 120, "y": 240, "width": 80, "height": 20},
        "element_ref": "snapshot:control",
    }

    assert wps_center_click_action(control) == {
        "type": "click",
        "x": 160,
        "y": 250,
        "target_element_ref": "snapshot:control",
    }


def test_wps_editor_replacement_is_one_text_action() -> None:
    assert wps_editor_replacement_actions("ASTRA_WPS_EDITED") == [
        {"type": "type", "text": "ASTRA_WPS_EDITED"},
    ]


def test_wps_modal_transition_keypress_is_one_guarded_action() -> None:
    assert wps_modal_transition_actions("s", ["command", "shift"]) == [
        {"type": "keypress", "key": "s", "modifiers": ["command", "shift"]}
    ]


def test_preinput_stale_retry_refreshes_once_without_replaying_unknown() -> None:
    async def exercise() -> None:
        snapshot_ids: list[str] = []
        responses = [
            {
                "error": "The target changed before input.",
                "code": "stale_snapshot",
                "details": {
                    "last_acknowledged_action": -1,
                    "outcomes": [
                        {"index": 0, "ok": False, "error_code": "stale_snapshot"},
                    ],
                },
            },
            {"error": "", "fresh_output": "{}"},
        ]

        async def call_raw(_name: str, **arguments):
            snapshot_ids.append(arguments["snapshot_id"])
            return responses.pop(0)

        async def refresh():
            return {"snapshot_id": "fresh"}

        result = await retry_preinput_stale_once(
            call_raw,
            refresh,
            snapshot={"snapshot_id": "old"},
            actions=[{"type": "keypress", "key": "g"}],
        )
        assert result["error"] == ""
        assert snapshot_ids == ["old", "fresh"]

        snapshot_ids.clear()
        responses[:] = [{"error": "Input may have started.", "code": "unknown_outcome"}]
        result = await retry_preinput_stale_once(
            call_raw,
            refresh,
            snapshot={"snapshot_id": "new"},
            actions=[{"type": "keypress", "key": "g"}],
        )
        assert result["code"] == "unknown_outcome"
        assert snapshot_ids == ["new"]

        snapshot_ids.clear()
        responses[:] = [{
            "error": "The target changed after a known action.",
            "code": "stale_snapshot",
            "details": {
                "last_acknowledged_action": 0,
                "outcomes": [
                    {"index": 0, "ok": True},
                    {"index": 1, "ok": False, "error_code": "stale_snapshot"},
                ],
                "status": "action_acknowledged",
                "effect_verification": "unverified",
            },
        }]
        result = await retry_preinput_stale_once(
            call_raw,
            refresh,
            snapshot={"snapshot_id": "acked"},
            actions=[{"type": "keypress", "key": "s"}, {"type": "wait", "duration_ms": 1}],
        )
        assert result["code"] == "stale_snapshot"
        assert snapshot_ids == ["acked"]

    run(exercise())


def test_action_result_metadata_reads_success_snapshot_and_production_failure() -> None:
    acknowledged = {
        "status": "action_acknowledged",
        "effect_verification": "unverified",
        "last_acknowledged_action": 0,
        "outcomes": [{"index": 0, "ok": True}],
    }
    assert action_result_metadata({
        "error": "",
        "fresh_output": json.dumps({"action_result": acknowledged}),
    }) == acknowledged
    assert action_result_metadata({
        "error": "The target changed after a known action.",
        "code": "stale_snapshot",
        "details": acknowledged,
    }) == acknowledged


def test_wps_unknown_text_replacement_and_observation_handoff_without_replay() -> None:
    class FakeComputer:
        def __init__(self, responses: list[dict]) -> None:
            self.responses = list(responses)
            self.calls: list[str] = []

        async def call_raw(self, name: str, **_arguments):
            self.calls.append(name)
            if name == "computer_handoff":
                return {"error": ""}
            assert name == "computer_act"
            return self.responses.pop(0)

    async def exercise() -> None:
        unknown = {"error": "input may have happened", "code": "unknown_outcome"}
        select_unknown = FakeComputer([unknown])
        workflow = WPSExportStateMachine(Path("/tmp"), "safe")
        with pytest.raises(WPSUnknownOutcome):
            await replace_focused_wps_text(
                select_unknown,
                {"snapshot_id": "old", "ax_tree": {}},
                "field",
                "safe",
                workflow=workflow,
            )
        assert select_unknown.calls == ["computer_act", "computer_handoff"]
        assert workflow.stopped

        selected = {
            "error": "",
            "fresh_output": json.dumps({
                "snapshot_id": "selected",
                "ax_tree": {"role": "AXTextField", "focused": True, "element_ref": "field"},
            }),
        }
        type_unknown = FakeComputer([selected, unknown])
        workflow = WPSExportStateMachine(Path("/tmp"), "safe")
        with pytest.raises(WPSUnknownOutcome):
            await replace_focused_wps_text(
                type_unknown,
                {"snapshot_id": "old", "ax_tree": {}},
                "field",
                "safe",
                workflow=workflow,
            )
        assert type_unknown.calls == ["computer_act", "computer_act", "computer_handoff"]

        observe_unknown = FakeComputer([])
        workflow = WPSExportStateMachine(Path("/tmp"), "safe")
        with pytest.raises(WPSUnknownOutcome):
            await observe_known_wps_action(
                observe_unknown,
                unknown,
                step="Go-to-Folder Return",
                workflow=workflow,
            )
        assert observe_unknown.calls == ["computer_handoff"]

        class PreinputStaleComputer:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.act_count = 0

            async def call_raw(self, name: str, **_arguments):
                self.calls.append(name)
                if name == "computer_snapshot":
                    return {
                        "error": "",
                        "fresh_output": json.dumps({
                            "snapshot_id": "fresh",
                            "ax_tree": {
                                "role": "AXTextField",
                                "focused": True,
                                "element_ref": "fresh-field",
                            },
                        }),
                    }
                assert name == "computer_act"
                self.act_count += 1
                if self.act_count == 1:
                    return {"error": "stale", "code": "stale_snapshot"}
                value = "" if self.act_count == 2 else "safe"
                return {
                    "error": "",
                    "fresh_output": json.dumps({
                        "snapshot_id": f"after-{self.act_count}",
                        "ax_tree": {
                            "role": "AXTextField",
                            "focused": True,
                            "element_ref": "fresh-field",
                            "value": value,
                        },
                    }),
                }

        stale_computer = PreinputStaleComputer()
        workflow = WPSExportStateMachine(Path("/tmp"), "safe")
        replaced = await replace_focused_wps_text(
            stale_computer,
            {"snapshot_id": "old", "ax_tree": {}},
            "old-field",
            "safe",
            workflow=workflow,
        )
        assert replaced["snapshot_id"] == "after-3"
        assert stale_computer.calls == [
            "computer_act",
            "computer_snapshot",
            "computer_act",
            "computer_act",
        ]

        acknowledged_stale = FakeComputer([{
            "error": "The target changed after a known action.",
            "code": "stale_snapshot",
            "details": {
                "last_acknowledged_action": 0,
                "outcomes": [{"index": 0, "ok": True}],
                "status": "action_acknowledged",
                "effect_verification": "unverified",
            },
        }])
        workflow = WPSExportStateMachine(Path("/tmp"), "safe")
        with pytest.raises(AssertionError, match="select focused WPS text failed"):
            await replace_focused_wps_text(
                acknowledged_stale,
                {"snapshot_id": "old", "ax_tree": {}},
                "field",
                "safe",
                workflow=workflow,
            )
        assert acknowledged_stale.calls == ["computer_act"]

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_wps_destination_first_state_machine_and_unknown_terminal(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path_node = {"role": "AXStaticText", "label": str(private)}
    name_node = {"role": "AXStaticText", "label": "wabcde"}
    workflow = WPSExportStateMachine(private, "wabcde")

    workflow.verify_destination({"children": [path_node]})
    workflow.rename_committed(from_snapshot_id="rename")
    workflow.verify_fresh_main_panel({
        "snapshot_id": "main",
        "ax_tree": {"children": [path_node, name_node]},
    })
    workflow.start(snapshot_id="main")
    assert workflow.phase == "started"

    async def exercise_unknown() -> None:
        class FakeComputer:
            async def call_raw(self, name: str, **_arguments):
                assert name == "computer_handoff"
                return {"error": ""}

        stopped = WPSExportStateMachine(private, "wabcde")
        stopped.verify_destination({"children": [path_node]})
        with pytest.raises(WPSUnknownOutcome):
            await observe_known_wps_action(
                FakeComputer(),
                {"error": "uncertain", "code": "unknown_outcome"},
                step="rename commit",
                workflow=stopped,
            )
        with pytest.raises(AssertionError, match="stopped"):
            stopped.start(snapshot_id="anything")

    run(exercise_unknown())

WPS_BUNDLE = "com.kingsoft.wpsoffice.mac"
WPS_SAVE_AS_WINDOW_TITLES = {"另存为", "Save As"}
WPS_FORMAT_WINDOW_TITLES = {"另存为新格式", "Save As New Format"}
WPS_FORMAT_BUTTON_LABELS = {
    "保存新格式",
    "使用 Word 2007-365 格式",
    "Use Word 2007-365 Format",
}
WPS_EXPORT_PDF_LABELS = {"输出为PDF...", "Export as PDF..."}
WPS_EXPORT_WINDOW_TITLES = {"输出为PDF", "Export as PDF"}
WPS_EXPORT_RENAME_WINDOW_TITLES = {"修改输出名称", "Change Output Name"}
WPS_COMPLETION_WINDOW_TITLES = {"完成提示", "Completion"}
WPS_EXPORT_NAME_BUTTON_LABELS = {"修改输出名称", "Change output name"}
WPS_SOURCE_FOLDER_LABELS = {"源文件夹", "Source folder"}
WPS_CUSTOM_FOLDER_LABELS = {"自定义文件夹", "Custom folder"}
WPS_DIRECTORY_CHOOSER_TITLES = {"选择路径", "Choose Path", "Select Path"}
WPS_DIRECTORY_CONFIRM_LABELS = {
    "打开",
    "选取",
    "选择",
    "选择文件夹",
    "Open",
    "Choose",
    "Choose Folder",
}
WPS_START_EXPORT_LABELS = {"开始输出", "Start Export"}
WPS_CANCEL_LABELS = {"取消", "Cancel"}
WPS_DONT_SAVE_LABELS = {"不保存", "不存储", "放弃更改", "Don't Save", "Discard Changes"}
WPS_E2E_SOURCE_NAME = re.compile(r"^wps-source-[0-9a-f]+\.docx$", re.IGNORECASE)
WPS_TEST_PROMPT_TITLES = (
    WPS_FORMAT_WINDOW_TITLES
    | WPS_EXPORT_WINDOW_TITLES
    | WPS_EXPORT_RENAME_WINDOW_TITLES
    | WPS_COMPLETION_WINDOW_TITLES
)


class WPSUnknownOutcome(AssertionError):
    """A semantic WPS action may have started and must never be replayed."""


class WPSPostconditionFailed(AssertionError):
    """An acknowledged WPS action did not produce its exact expected state."""


async def wps_handoff_report(computer) -> str:
    result = await computer.call_raw("computer_handoff")
    if result.get("error"):
        return f"handoff failed code={result.get('code') or 'unknown'}"
    return "handed off"


class WPSExportStateMachine:
    def __init__(self, expected_directory: Path, expected_basename: str) -> None:
        self.expected_directory = expected_directory
        self.expected_basename = expected_basename
        self.phase = "destination_first"
        self._rename_snapshot_id = ""
        self._ready_snapshot_id = ""
        self.stopped = False

    def _require(self, phase: str) -> None:
        if self.stopped:
            raise AssertionError("WPS export workflow is stopped")
        if self.phase != phase:
            raise AssertionError(f"WPS export workflow expected {phase}, got {self.phase}")

    def verify_destination(self, ax_tree: object) -> None:
        self._require("destination_first")
        workspace = self.expected_directory.resolve(strict=True)
        metadata = workspace.stat()
        if not workspace.is_dir() or workspace.is_symlink() or metadata.st_uid != os.getuid():
            raise AssertionError("WPS export destination must be an owned canonical directory")
        if metadata.st_mode & 0o077:
            raise AssertionError("WPS export destination must be private mode-0700")
        exact_canonical_directory_node(ax_tree, workspace)
        self.phase = "destination_verified"

    def rename_committed(self, *, from_snapshot_id: str) -> None:
        self._require("destination_verified")
        if not from_snapshot_id:
            raise AssertionError("WPS rename commit requires its source snapshot")
        self._rename_snapshot_id = from_snapshot_id
        self.phase = "rename_committed"

    def verify_fresh_main_panel(self, snapshot: dict) -> None:
        self._require("rename_committed")
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        if not snapshot_id or snapshot_id == self._rename_snapshot_id:
            raise AssertionError("WPS export requires a fresh main-panel snapshot after rename")
        exact_canonical_directory_node(snapshot.get("ax_tree", {}), self.expected_directory)
        names = find_nodes(
            snapshot.get("ax_tree", {}),
            lambda node: node.get("role") in {"AXStaticText", "AXTextField"}
            and normalized_ax_label(
                node.get("title") or node.get("label") or node.get("value")
            ) == normalized_ax_label(self.expected_basename),
        )
        if len(names) != 1:
            raise AssertionError("WPS export basename must be present exactly once")
        self._ready_snapshot_id = snapshot_id
        self.phase = "ready_to_start"

    def start(self, *, snapshot_id: str) -> None:
        self._require("ready_to_start")
        if snapshot_id != self._ready_snapshot_id:
            raise AssertionError("WPS export start requires the verified fresh main panel")
        self.phase = "started"

    def stop_unknown(self) -> None:
        self.stopped = True
        self.phase = "stopped_unknown"


async def require_known_wps_action(
    computer,
    result: dict,
    *,
    step: str,
    workflow: WPSExportStateMachine | None = None,
) -> None:
    if not result.get("error"):
        return
    if result.get("code") == "unknown_outcome":
        if workflow is not None:
            workflow.stop_unknown()
        await computer.call_raw("computer_handoff")
        raise WPSUnknownOutcome(f"{step} has unknown outcome; handed off without replay")
    raise AssertionError(f"{step} failed code={result.get('code')}")


async def observe_known_wps_action(
    computer,
    result: dict,
    *,
    step: str,
    workflow: WPSExportStateMachine,
) -> dict:
    await require_known_wps_action(
        computer,
        result,
        step=step,
        workflow=workflow,
    )
    return json.loads(result["fresh_output"])


async def replace_focused_wps_text(
    computer,
    snapshot: dict,
    element_ref: str,
    value: str,
    *,
    workflow: WPSExportStateMachine,
) -> dict:
    selected = await computer.call_raw(
        "computer_act",
        snapshot_id=snapshot["snapshot_id"],
        actions=[{
            "type": "keypress",
            "key": "a",
            "modifiers": ["command"],
            "element_ref": element_ref,
        }],
    )
    if selected.get("code") == "stale_snapshot":
        acknowledged = action_result_metadata(selected).get("last_acknowledged_action", -1)
        if not isinstance(acknowledged, int) or isinstance(acknowledged, bool):
            acknowledged = -1
        if acknowledged < 0:
            refreshed_result = await computer.call_raw(
                "computer_snapshot", scope="target_window"
            )
            if refreshed_result.get("error"):
                raise AssertionError(
                    "refresh focused WPS text after pre-input stale failed "
                    f"code={refreshed_result.get('code')}"
                )
            refreshed = json.loads(refreshed_result["fresh_output"])
            refreshed_field = first_node(
                refreshed["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "refreshed focused WPS text field",
            )
            selected = await computer.call_raw(
                "computer_act",
                snapshot_id=refreshed["snapshot_id"],
                actions=[{
                    "type": "keypress",
                    "key": "a",
                    "modifiers": ["command"],
                    "element_ref": refreshed_field["element_ref"],
                }],
            )
    current = await observe_known_wps_action(
        computer, selected, step="select focused WPS text", workflow=workflow
    )
    expected = ""
    for offset in range(0, len(value), 9):
        characters = value[offset:offset + 9]
        field = first_node(
            current["ax_tree"],
            lambda node: node.get("role") == "AXTextField" and node.get("focused") is True,
            "WPS focused field before text insertion",
        )
        result = await computer.call_raw(
            "computer_act",
            snapshot_id=current["snapshot_id"],
            actions=[
                {"type": "type", "text": characters, "element_ref": field["element_ref"]},
                {"type": "wait", "duration_ms": 30},
            ],
        )
        current = await observe_known_wps_action(
            computer, result, step="type focused WPS text", workflow=workflow
        )
        expected += characters
        observed = first_node(
            current["ax_tree"],
            lambda node: node.get("role") == "AXTextField" and node.get("focused") is True,
            "WPS text field after insertion",
        )
        assert observed.get("value") == expected, observed
    return current


def normalized_ax_label(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def exact_format_confirmation_button(ax_tree: object) -> dict:
    expected = {normalized_ax_label(value) for value in WPS_FORMAT_BUTTON_LABELS}
    matches = find_nodes(
        ax_tree,
        lambda node: node.get("role") == "AXButton"
        and normalized_ax_label(node.get("title") or node.get("label")) in expected,
    )
    if len(matches) != 1:
        raise AssertionError("WPS format confirmation requires exactly one trusted matching button")
    return matches[0]


def exact_ax_control(
    ax_tree: object,
    *,
    roles: set[str],
    labels: set[str],
    description: str,
) -> dict:
    expected = {normalized_ax_label(value) for value in labels}
    matches = find_nodes(
        ax_tree,
        lambda node: node.get("role") in roles
        and normalized_ax_label(
            node.get("title") or node.get("label") or node.get("value")
        ) in expected,
    )
    if len(matches) != 1:
        raise AssertionError(f"{description} requires exactly one trusted matching control")
    return matches[0]


def exact_wps_custom_folder_button(ax_tree: object, location_popup: dict) -> dict:
    popup_bounds = location_popup.get("bounds")
    try:
        popup_x = float(popup_bounds["x"])
        popup_y = float(popup_bounds["y"])
        popup_width = float(popup_bounds["width"])
        popup_height = float(popup_bounds["height"])
    except (KeyError, TypeError, ValueError):
        raise AssertionError("WPS custom-folder chooser requires trusted popup bounds") from None

    def is_adjacent_unlabelled_button(node: dict) -> bool:
        bounds = node.get("bounds")
        try:
            x = float(bounds["x"])
            y = float(bounds["y"])
            width = float(bounds["width"])
            height = float(bounds["height"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            node.get("role") == "AXButton"
            and node.get("enabled") is True
            and not normalized_ax_label(
                node.get("title") or node.get("label") or node.get("value")
            )
            and "AXPress" in (node.get("actions") or [])
            and 1 <= width <= 32
            and 1 <= height <= 32
            and popup_x <= x
            and x + width <= popup_x + popup_width
            and popup_y + popup_height < y + height / 2 <= popup_y + popup_height + 48
        )

    matches = find_nodes(ax_tree, is_adjacent_unlabelled_button)
    if len(matches) != 1:
        raise AssertionError(
            "WPS custom-folder chooser requires exactly one adjacent trusted button"
        )
    return matches[0]


def exact_canonical_directory_node(ax_tree: object, expected: Path) -> dict:
    canonical = expected.resolve(strict=True)
    matches: list[dict] = []
    for node in find_nodes(ax_tree, lambda item: item.get("role") == "AXStaticText"):
        for raw in (node.get("label"), node.get("value")):
            text = str(raw or "").strip().rstrip("/") or "/"
            if not text.startswith("/"):
                continue
            try:
                candidate = Path(text).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if candidate == canonical:
                matches.append(node)
                break
    if len(matches) != 1:
        raise AssertionError("WPS export directory requires exactly one canonical trusted path")
    return matches[0]


def exact_wps_window_refs(apps: list[dict], titles: set[str]) -> tuple[str, str]:
    expected = {normalized_ax_label(value) for value in titles}
    matches = [
        (str(app["app_ref"]), str(window["window_ref"]))
        for app in apps
        if app.get("bundle_id") == WPS_BUNDLE
        for window in app.get("windows", [])
        if normalized_ax_label(window.get("title")) in expected
    ]
    if len(matches) != 1:
        raise AssertionError("WPS workflow requires exactly one trusted matching window")
    return matches[0]


async def refresh_exact_wps_window_state(
    computer,
    *,
    states: dict[str, Callable[[dict], bool]],
    max_attempts: int = 40,
    retry_delay: float = 0.2,
) -> tuple[str, dict]:
    """Obtain a fresh exact WPS dialog/state without replaying input.

    Every retry starts with a new catalog, so an opaque window reference that
    disappeared is never reused. Focus retries only target_gone. Snapshot
    observation may also retry helper_failed because no input is dispatched;
    every other error and every ambiguous trusted state fails closed.
    """
    last_catalog: list[dict] = []
    for attempt in range(max_attempts):
        last_catalog = await computer.apps()
        matches: list[tuple[str, str, str]] = []
        for application in last_catalog:
            if application.get("bundle_id") != WPS_BUNDLE:
                continue
            app_ref = str(application.get("app_ref") or "")
            for window in application.get("windows", []):
                matching_states = [name for name, predicate in states.items() if predicate(window)]
                if len(matching_states) > 1:
                    raise AssertionError("WPS workflow state is ambiguous")
                if matching_states:
                    matches.append((matching_states[0], app_ref, str(window.get("window_ref") or "")))
        if len(matches) > 1:
            raise AssertionError("WPS workflow requires exactly one trusted matching window state")
        if matches:
            state, app_ref, window_ref = matches[0]
            focus_result = await computer.call_raw(
                "computer_focus",
                app_ref=app_ref,
                window_ref=window_ref,
            )
            if focus_result.get("error"):
                if focus_result.get("code") == "target_gone":
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(retry_delay)
                    continue
                raise AssertionError(
                    "WPS exact-state focus failed closed "
                    f"code={focus_result.get('code')}: {focus_result.get('error')}"
                )
            snapshot_result = await computer.call_raw(
                "computer_snapshot",
                scope="target_window",
            )
            if snapshot_result.get("error"):
                if snapshot_result.get("code") in {"target_gone", "helper_failed"}:
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(retry_delay)
                    continue
                raise AssertionError(
                    "WPS exact-state snapshot failed closed "
                    f"code={snapshot_result.get('code')}: {snapshot_result.get('error')}"
                )
            return state, json.loads(snapshot_result["fresh_output"])
        if attempt + 1 < max_attempts:
            await asyncio.sleep(retry_delay)
    catalog = [
        (
            application.get("bundle_id"),
            [window.get("title") for window in application.get("windows", [])],
        )
        for application in last_catalog
    ]
    raise AssertionError(f"timed out waiting for exact WPS window state; catalog={catalog!r}")


async def observe_wps_document_open_after_press(
    computer,
    result: dict,
    *,
    expected_titles: set[str],
    preexisting_titles: set[str],
    max_attempts: int = 40,
    retry_delay: float = 0.2,
) -> dict:
    if result.get("error"):
        if result.get("code") == "unknown_outcome":
            handoff = await wps_handoff_report(computer)
            raise WPSUnknownOutcome(
                f"WPS Home-list press has unknown outcome; {handoff}; no replay"
            )
        raise AssertionError(f"WPS Home-list press failed code={result.get('code')}")

    action_result = action_result_metadata(result)
    acknowledged = action_result.get("last_acknowledged_action")
    if not (
        action_result.get("status") == "action_acknowledged"
        and action_result.get("effect_verification") == "unverified"
        and isinstance(acknowledged, int)
        and not isinstance(acknowledged, bool)
        and acknowledged >= 0
    ):
        handoff = await wps_handoff_report(computer)
        raise WPSPostconditionFailed(
            "WPS Home-list press acknowledgement metadata is invalid; "
            f"{handoff}; no replay"
        )

    expected = {normalized_ax_label(value) for value in expected_titles}
    baseline = {normalized_ax_label(value) for value in preexisting_titles}
    try:
        _state, snapshot = await refresh_exact_wps_window_state(
            computer,
            states={
                "document": lambda window: normalized_ax_label(window.get("title"))
                in expected
                and normalized_ax_label(window.get("title")) not in baseline,
            },
            max_attempts=max_attempts,
            retry_delay=retry_delay,
        )
    except Exception as error:
        handoff = await wps_handoff_report(computer)
        raise WPSPostconditionFailed(
            "WPS Home-list press was acknowledged but the exact document window did not open; "
            f"{handoff}; no replay"
        ) from error

    snapshot["effect_verification"] = "verified"
    snapshot["verified_effect"] = "wps_document_window_open"
    return snapshot


def test_wps_home_list_acknowledgement_without_document_window_hands_off_once() -> None:
    class FakeComputer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def apps(self):
            self.calls.append("computer_apps")
            return [{
                "bundle_id": WPS_BUNDLE,
                "app_ref": "wps",
                "windows": [{"window_ref": "home", "title": "首页"}],
            }]

        async def call_raw(self, name: str, **_arguments):
            self.calls.append(name)
            if name == "computer_handoff":
                return {"error": ""}
            raise AssertionError(f"unexpected call {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        with pytest.raises(WPSPostconditionFailed):
            await observe_wps_document_open_after_press(
                computer,
                {
                    "error": "",
                    "fresh_output": json.dumps({
                        "action_result": {
                            "status": "action_acknowledged",
                            "effect_verification": "unverified",
                            "last_acknowledged_action": 0,
                        }
                    }),
                },
                expected_titles={"fixture.docx"},
                preexisting_titles={"首页"},
                max_attempts=2,
                retry_delay=0,
            )
        assert computer.calls.count("computer_handoff") == 1
        assert "computer_act" not in computer.calls

    run(exercise())


def test_wps_home_list_acknowledgement_verified_window() -> None:
    class FakeComputer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def apps(self):
            self.calls.append("computer_apps")
            return [{
                "bundle_id": WPS_BUNDLE,
                "app_ref": "wps",
                "windows": [{"window_ref": "document", "title": "fixture.docx"}],
            }]

        async def call_raw(self, name: str, **_arguments):
            self.calls.append(name)
            if name == "computer_focus":
                return {"error": ""}
            if name == "computer_snapshot":
                return {"error": "", "fresh_output": '{"snapshot_id":"document"}'}
            raise AssertionError(f"unexpected call {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        acknowledged_result = {
            "error": "",
            "fresh_output": json.dumps({
                "action_result": {
                    "status": "action_acknowledged",
                    "effect_verification": "unverified",
                    "last_acknowledged_action": 0,
                }
            }),
        }
        snapshot = await observe_wps_document_open_after_press(
            computer,
            acknowledged_result,
            expected_titles={"fixture.docx"},
            preexisting_titles={"首页"},
            max_attempts=1,
            retry_delay=0,
        )
        assert snapshot["effect_verification"] == "verified"
        assert snapshot["verified_effect"] == "wps_document_window_open"
        assert "computer_handoff" not in computer.calls

    run(exercise())


def test_wps_home_list_refuses_preexisting_title_with_refreshed_window_ref() -> None:
    preexisting_window = {"window_ref": "document-before", "title": "fixture.docx"}

    class FakeComputer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def apps(self):
            self.calls.append("computer_apps")
            return [{
                "bundle_id": WPS_BUNDLE,
                "app_ref": "wps",
                "windows": [{
                    "window_ref": "document-after",
                    "title": "fixture.docx",
                    "document_path": "/private/tmp/fixture.docx",
                }],
            }]

        async def call_raw(self, name: str, **_arguments):
            self.calls.append(name)
            if name == "computer_handoff":
                return {"error": ""}
            raise AssertionError(f"unexpected call {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        with pytest.raises(WPSPostconditionFailed, match="did not open"):
            await observe_wps_document_open_after_press(
                computer,
                {
                    "error": "",
                    "fresh_output": json.dumps({
                        "action_result": {
                            "status": "action_acknowledged",
                            "effect_verification": "unverified",
                            "last_acknowledged_action": 0,
                        }
                    }),
                },
                expected_titles={"fixture.docx"},
                preexisting_titles={str(preexisting_window["title"])},
                max_attempts=1,
                retry_delay=0,
            )
        assert computer.calls == ["computer_apps", "computer_handoff"]
        assert "computer_act" not in computer.calls
        assert preexisting_window["window_ref"] != "document-after"

    run(exercise())


def test_wps_home_list_reports_handoff_failure_without_losing_primary_error() -> None:
    class UnknownComputer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call_raw(self, name: str, **_arguments):
            self.calls.append(name)
            assert name == "computer_handoff"
            return {"error": "helper unavailable", "code": "helper_failed"}

    class MissingWindowComputer(UnknownComputer):
        async def apps(self):
            self.calls.append("computer_apps")
            return [{
                "bundle_id": WPS_BUNDLE,
                "app_ref": "wps",
                "windows": [{"window_ref": "home", "title": "首页"}],
            }]

    async def exercise() -> None:
        unknown = UnknownComputer()
        with pytest.raises(WPSUnknownOutcome) as unknown_error:
            await observe_wps_document_open_after_press(
                unknown,
                {"error": "input may have happened", "code": "unknown_outcome"},
                expected_titles={"fixture.docx"},
                preexisting_titles={"首页"},
            )
        unknown_message = str(unknown_error.value)
        assert "unknown outcome" in unknown_message
        assert "handoff failed code=helper_failed" in unknown_message
        assert "handed off" not in unknown_message
        assert unknown.calls == ["computer_handoff"]

        missing = MissingWindowComputer()
        with pytest.raises(WPSPostconditionFailed) as postcondition_error:
            await observe_wps_document_open_after_press(
                missing,
                {
                    "error": "",
                    "fresh_output": json.dumps({
                        "action_result": {
                            "status": "action_acknowledged",
                            "effect_verification": "unverified",
                            "last_acknowledged_action": 0,
                        }
                    }),
                },
                expected_titles={"fixture.docx"},
                preexisting_titles={"首页"},
                max_attempts=1,
                retry_delay=0,
            )
        postcondition_message = str(postcondition_error.value)
        assert "exact document window did not open" in postcondition_message
        assert "handoff failed code=helper_failed" in postcondition_message
        assert "handed off" not in postcondition_message
        assert missing.calls == ["computer_apps", "computer_handoff"]

    run(exercise())


@pytest.mark.parametrize(
    "action_result",
    [
        None,
        {
            "status": "completed",
            "effect_verification": "unverified",
            "last_acknowledged_action": 0,
        },
        {
            "status": "action_acknowledged",
            "effect_verification": "verified",
            "last_acknowledged_action": 0,
        },
        {
            "status": "action_acknowledged",
            "effect_verification": "unverified",
            "last_acknowledged_action": -1,
        },
    ],
    ids=["missing", "wrong-status", "wrong-effect", "unacknowledged"],
)
def test_wps_home_list_rejects_invalid_acknowledgement_before_polling(action_result) -> None:
    class FakeComputer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def apps(self):
            self.calls.append("computer_apps")
            return [{
                "bundle_id": WPS_BUNDLE,
                "app_ref": "wps",
                "windows": [{"window_ref": "document", "title": "fixture.docx"}],
            }]

        async def call_raw(self, name: str, **_arguments):
            self.calls.append(name)
            if name == "computer_handoff":
                return {"error": ""}
            if name == "computer_focus":
                return {"error": ""}
            if name == "computer_snapshot":
                return {"error": "", "fresh_output": '{"snapshot_id":"document"}'}
            raise AssertionError(f"unexpected call {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        payload = {} if action_result is None else {"action_result": action_result}
        with pytest.raises(WPSPostconditionFailed, match="acknowledgement metadata"):
            await observe_wps_document_open_after_press(
                computer,
                {"error": "", "fresh_output": json.dumps(payload)},
                expected_titles={"fixture.docx"},
                preexisting_titles=set(),
                max_attempts=1,
                retry_delay=0,
            )
        assert computer.calls == ["computer_handoff"]
        assert "computer_act" not in computer.calls

    run(exercise())


def acknowledged_action_count(result: dict) -> int:
    acknowledged = action_result_metadata(result).get("last_acknowledged_action", -1)
    return acknowledged + 1 if isinstance(acknowledged, int) and not isinstance(acknowledged, bool) else 0


async def observe_expected_wps_transition(
    computer,
    result: dict,
    *,
    states: dict[str, Callable[[dict], bool]],
    step: str,
    workflow: WPSExportStateMachine,
) -> tuple[str, dict]:
    """Resolve an acknowledged UI transition by exact fresh observation, never replay."""
    if result.get("error") and not (
        result.get("code") == "unknown_outcome" and acknowledged_action_count(result) == 1
    ):
        await require_known_wps_action(
            computer,
            result,
            step=step,
            workflow=workflow,
        )
    try:
        return await refresh_exact_wps_window_state(
            computer,
            states=states,
            max_attempts=80,
            retry_delay=0.1,
        )
    except Exception as error:
        if result.get("error"):
            workflow.stop_unknown()
            await computer.call_raw("computer_handoff")
            raise WPSUnknownOutcome(
                f"{step} could not resolve its acknowledged transition; handed off"
            ) from error
        raise


def test_acknowledged_wps_transition_is_resolved_by_observation_without_replay() -> None:
    class FakeComputer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def apps(self):
            self.calls.append("computer_apps")
            return [{
                "bundle_id": WPS_BUNDLE,
                "app_ref": "wps",
                "windows": [{"window_ref": "save", "title": "Save As"}],
            }]

        async def call_raw(self, name: str, **_arguments):
            self.calls.append(name)
            if name == "computer_focus":
                return {"error": ""}
            if name == "computer_snapshot":
                return {"error": "", "fresh_output": '{"snapshot_id":"save"}'}
            raise AssertionError(f"unexpected call {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        workflow = WPSExportStateMachine(Path("/tmp"), "safe")
        result = {
            "error": "observation failed",
            "code": "unknown_outcome",
            "details": {
                "last_acknowledged_action": 0,
                "outcomes": [{"index": 0, "ok": True}],
                "status": "action_acknowledged",
                "effect_verification": "unverified",
            },
        }
        state, snapshot = await observe_expected_wps_transition(
            computer,
            result,
            states={"save": lambda window: window.get("title") == "Save As"},
            step="open Save As",
            workflow=workflow,
        )
        assert state == "save"
        assert snapshot["snapshot_id"] == "save"
        assert computer.calls == ["computer_apps", "computer_focus", "computer_snapshot"]
        assert not workflow.stopped

    run(exercise())


def _private_cleanup_workspace(workspace: Path) -> Path:
    canonical = workspace.resolve(strict=True)
    metadata = canonical.stat()
    if (
        not canonical.is_dir()
        or workspace.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise AssertionError("WPS cleanup workspace must be owned canonical mode-0700")
    return canonical


def _resolved_wps_document_path(window: dict) -> Path | None:
    raw_path = str(window.get("document_path") or "")
    if not raw_path:
        return None
    try:
        return Path(raw_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _owned_wps_test_document(
    window: dict,
    owned_names: set[str],
    *,
    workspace: Path,
) -> bool:
    canonical = _resolved_wps_document_path(window)
    if canonical is None:
        title = str(window.get("title") or "")
        if title not in owned_names or Path(title).name != title:
            return False
        candidate = workspace / title
        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return (
            not candidate.is_symlink()
            and canonical.is_file()
            and canonical.is_relative_to(workspace)
        )
    candidate = Path(str(window["document_path"]))
    return (
        not candidate.is_symlink()
        and canonical.is_file()
        and canonical.name in owned_names
        and canonical.is_relative_to(workspace)
    )


def _cleanup_catalog_matches(
    apps: list[dict],
    owned_names: set[str],
    *,
    workspace: Path,
) -> tuple[list[dict], list[dict]]:
    documents: list[dict] = []
    format_prompts: list[dict] = []
    expected_prompts = {normalized_ax_label(value) for value in WPS_TEST_PROMPT_TITLES}
    for application in apps:
        if application.get("bundle_id") != WPS_BUNDLE:
            continue
        for raw_window in application.get("windows", []):
            window = dict(raw_window)
            window["app_ref"] = str(application.get("app_ref") or "")
            if normalized_ax_label(window.get("title")) in expected_prompts:
                format_prompts.append(window)
            elif _owned_wps_test_document(window, owned_names, workspace=workspace):
                documents.append(window)
    return documents, format_prompts


async def _wait_cleanup_window_absent(
    computer,
    predicate: Callable[[dict], bool],
    *,
    max_attempts: int = 20,
    retry_delay: float = 0.1,
) -> bool:
    for attempt in range(max_attempts):
        apps = await computer.apps()
        matches = [
            window
            for application in apps
            if application.get("bundle_id") == WPS_BUNDLE
            for window in application.get("windows", [])
            if predicate(window)
        ]
        if not matches:
            return True
        if attempt + 1 < max_attempts:
            await asyncio.sleep(retry_delay)
    return False


async def cleanup_test_owned_wps_windows(
    computer,
    *,
    owned_names: set[str],
    workspace: Path,
    max_rounds: int = 8,
    retry_delay: float = 0.1,
) -> list[str]:
    """Close only this run's exact documents inside its private workspace."""
    canonical_workspace = _private_cleanup_workspace(workspace)
    cleaned: list[str] = []
    for _round in range(max_rounds):
        apps = await computer.apps()
        documents, prompts = _cleanup_catalog_matches(
            apps,
            owned_names,
            workspace=canonical_workspace,
        )
        if prompts:
            if len(documents) > 1:
                await computer.call_raw("computer_handoff")
                raise AssertionError(
                    "refusing ambiguous WPS prompt cleanup with multiple test-owned documents"
                )
            rename_titles = {
                normalized_ax_label(value) for value in WPS_EXPORT_RENAME_WINDOW_TITLES
            }
            export_titles = {
                normalized_ax_label(value) for value in WPS_EXPORT_WINDOW_TITLES
            }
            format_titles = {
                normalized_ax_label(value) for value in WPS_FORMAT_WINDOW_TITLES
            }
            completion_titles = {
                normalized_ax_label(value) for value in WPS_COMPLETION_WINDOW_TITLES
            }
            by_kind = {
                "completion": [
                    prompt for prompt in prompts
                    if normalized_ax_label(prompt.get("title")) in completion_titles
                ],
                "rename": [
                    prompt for prompt in prompts
                    if normalized_ax_label(prompt.get("title")) in rename_titles
                ],
                "format": [
                    prompt for prompt in prompts
                    if normalized_ax_label(prompt.get("title")) in format_titles
                ],
                "export": [
                    prompt for prompt in prompts
                    if normalized_ax_label(prompt.get("title")) in export_titles
                ],
            }
            if any(len(matches) > 1 for matches in by_kind.values()):
                await computer.call_raw("computer_handoff")
                raise AssertionError("refusing ambiguous duplicate WPS test prompt cleanup")
            present = {kind for kind, matches in by_kind.items() if matches}
            if not present <= {"completion", "rename", "export"} and len(present) != 1:
                await computer.call_raw("computer_handoff")
                raise AssertionError("refusing ambiguous WPS test prompt stack cleanup")
            kind = (
                "completion" if by_kind["completion"]
                else "rename" if by_kind["rename"]
                else "format" if by_kind["format"]
                else "export"
            )
            selected_title = normalized_ax_label(by_kind[kind][0]["title"])
            selected_prompt = by_kind[kind][0]
            prompt_owner = _resolved_wps_document_path(selected_prompt)
            document_owner = prompt_owner
            if documents:
                document_owner = (
                    _resolved_wps_document_path(documents[0])
                    or (canonical_workspace / str(documents[0].get("title") or "")).resolve(strict=True)
                )
            if (
                document_owner is None
                or prompt_owner is not None and prompt_owner != document_owner
                or kind == "completion" and not by_kind["export"]
            ):
                await computer.call_raw("computer_handoff")
                raise AssertionError(
                    "refusing WPS prompt cleanup without proven document ownership association"
                )
            try:
                _state, snapshot = await refresh_exact_wps_window_state(
                    computer,
                    states={
                        "test_prompt": lambda window, expected=selected_title, owner=prompt_owner: (
                            normalized_ax_label(window.get("title")) == expected
                            and (
                                owner is None
                                or _resolved_wps_document_path(dict(window)) == owner
                            )
                        ),
                    },
                    max_attempts=10,
                    retry_delay=retry_delay,
                )
            except AssertionError:
                await computer.call_raw("computer_handoff")
                raise
            actions: list[dict[str, object]]
            if prompt_owner is None and kind != "completion":
                expected_names = {
                    normalized_ax_label(document_owner.name),
                    normalized_ax_label(document_owner.stem),
                }
                visible_names = {
                    normalized_ax_label(
                        node.get("title") or node.get("label") or node.get("value")
                    )
                    for node in find_nodes(snapshot["ax_tree"], lambda _node: True)
                }
                if not expected_names & visible_names:
                    await computer.call_raw("computer_handoff")
                    raise AssertionError(
                        "refusing pathless WPS prompt cleanup without exact test filename ownership"
                    )
            if kind in {"completion", "export"}:
                close_buttons = find_nodes(
                    snapshot["ax_tree"],
                    lambda node: node.get("role") == "AXButton"
                    and node.get("subrole") == "AXCloseButton"
                    and node.get("enabled") is True,
                )
                if len(close_buttons) != 1:
                    raise AssertionError(
                        f"test-owned WPS {kind} requires one exact close button"
                    )
                actions = [{
                    "type": "click",
                    "element_ref": close_buttons[0]["element_ref"],
                }]
            else:
                cancel = exact_ax_control(
                    snapshot["ax_tree"],
                    roles={"AXButton"},
                    labels=WPS_CANCEL_LABELS,
                    description="test-owned WPS prompt Cancel button",
                )
                actions = [{"type": "click", "element_ref": cancel["element_ref"]}]
            result = await computer.call_raw(
                "computer_act",
                snapshot_id=snapshot["snapshot_id"],
                actions=actions,
            )
            prompt_absent = await _wait_cleanup_window_absent(
                computer,
                lambda window, expected=selected_title: normalized_ax_label(
                    window.get("title")
                ) == expected,
                retry_delay=retry_delay,
            )
            if result.get("code") == "unknown_outcome":
                if not prompt_absent:
                    await computer.call_raw("computer_handoff")
                    raise WPSUnknownOutcome(
                        "cleanup WPS prompt has unknown outcome; handed off without replay"
                    )
            else:
                await require_known_wps_action(
                    computer,
                    result,
                    step="cleanup WPS prompt",
                )
            if not prompt_absent:
                raise AssertionError("test-owned WPS prompt remained after one cleanup action")
            continue
        if not documents:
            return cleaned
        document = documents[0]
        document_name = str(document.get("title") or Path(str(document.get("document_path") or "")).name)
        document_match = lambda window, expected=document_name: _owned_wps_test_document(
            dict(window),
            {expected},
            workspace=canonical_workspace,
        )
        _state, snapshot = await refresh_exact_wps_window_state(
            computer,
            states={"document": document_match},
            max_attempts=10,
            retry_delay=retry_delay,
        )
        close_result = await computer.call_raw(
            "computer_act",
            snapshot_id=snapshot["snapshot_id"],
            actions=[{"type": "keypress", "key": "w", "modifiers": ["command"]}],
        )
        if close_result.get("code") != "unknown_outcome":
            await require_known_wps_action(
                computer,
                close_result,
                step="cleanup WPS document Close",
            )
        if await _wait_cleanup_window_absent(
            computer,
            document_match,
            max_attempts=8,
            retry_delay=retry_delay,
        ):
            cleaned.append(document_name)
            continue
        _state, close_prompt = await refresh_exact_wps_window_state(
            computer,
            states={"document": document_match},
            max_attempts=10,
            retry_delay=retry_delay,
        )
        dont_save = exact_ax_control(
            close_prompt["ax_tree"],
            roles={"AXButton"},
            labels=WPS_DONT_SAVE_LABELS,
            description="test-owned WPS Don't Save button",
        )
        discard_result = await computer.call_raw(
            "computer_act",
            snapshot_id=close_prompt["snapshot_id"],
            actions=[{"type": "click", "element_ref": dont_save["element_ref"]}],
        )
        document_absent = await _wait_cleanup_window_absent(
            computer,
            document_match,
            retry_delay=retry_delay,
        )
        if discard_result.get("code") == "unknown_outcome":
            if not document_absent:
                await computer.call_raw("computer_handoff")
                raise WPSUnknownOutcome(
                    "cleanup WPS Don't Save has unknown outcome; handed off without replay"
                )
        else:
            await require_known_wps_action(
                computer,
                discard_result,
                step="cleanup WPS Don't Save",
            )
        if not document_absent:
            raise AssertionError("test-owned WPS document remained after one discard action")
        cleaned.append(document_name)
    raise AssertionError("test-owned WPS cleanup exceeded its bounded rounds")


def test_exact_wps_state_refresh_reenumerates_target_gone_without_act_replay() -> None:
    class FakeComputer:
        def __init__(self) -> None:
            self.catalogs = [
                [{
                    "app_ref": "wps-old",
                    "bundle_id": WPS_BUNDLE,
                    "windows": [{"window_ref": "old", "title": "Save As New Format"}],
                }],
                [{
                    "app_ref": "wps-new",
                    "bundle_id": WPS_BUNDLE,
                    "windows": [{"window_ref": "new", "title": "Save As New Format"}],
                }],
            ]
            self.calls: list[tuple[str, dict]] = []

        async def apps(self):
            return self.catalogs.pop(0)

        async def call_raw(self, name, **arguments):
            self.calls.append((name, arguments))
            if name == "computer_focus" and arguments["window_ref"] == "old":
                return {"error": "gone", "code": "target_gone"}
            if name == "computer_focus":
                return {"error": ""}
            if name == "computer_snapshot":
                return {"error": "", "fresh_output": '{"snapshot_id":"fresh"}'}
            raise AssertionError("act must never be dispatched while refreshing")

    async def exercise():
        computer = FakeComputer()
        state, snapshot = await refresh_exact_wps_window_state(
            computer,
            states={
                "dialog": lambda window: normalized_ax_label(window.get("title"))
                in {normalized_ax_label(value) for value in WPS_FORMAT_WINDOW_TITLES},
            },
            max_attempts=2,
            retry_delay=0,
        )
        assert state == "dialog"
        assert snapshot["snapshot_id"] == "fresh"
        assert [name for name, _arguments in computer.calls] == [
            "computer_focus",
            "computer_focus",
            "computer_snapshot",
        ]

    run(exercise())


def test_exact_wps_state_refresh_retries_snapshot_observation_failure_without_act() -> None:
    class FakeComputer:
        def __init__(self) -> None:
            self.snapshot_responses = [
                {"error": "capture failed", "code": "helper_failed"},
                {"error": "", "fresh_output": '{"snapshot_id":"fresh"}'},
            ]
            self.calls: list[str] = []

        async def apps(self):
            return [{
                "app_ref": "wps",
                "bundle_id": WPS_BUNDLE,
                "windows": [{"window_ref": "export", "title": "输出为PDF"}],
            }]

        async def call_raw(self, name, **_arguments):
            self.calls.append(name)
            if name == "computer_focus":
                return {"error": ""}
            if name == "computer_snapshot":
                return self.snapshot_responses.pop(0)
            raise AssertionError("act must never be dispatched while refreshing")

    async def exercise():
        computer = FakeComputer()
        state, snapshot = await refresh_exact_wps_window_state(
            computer,
            states={"export": lambda window: window.get("title") == "输出为PDF"},
            max_attempts=2,
            retry_delay=0,
        )
        assert state == "export"
        assert snapshot["snapshot_id"] == "fresh"
        assert computer.calls == [
            "computer_focus",
            "computer_snapshot",
            "computer_focus",
            "computer_snapshot",
        ]

    run(exercise())


def test_exact_wps_state_refresh_accepts_already_advanced_expected_state() -> None:
    class FakeComputer:
        async def apps(self):
            return [{
                "app_ref": "wps",
                "bundle_id": WPS_BUNDLE,
                "windows": [{
                    "window_ref": "document",
                    "title": "copy.docx",
                    "document_path": "/private/tmp/copy.docx",
                }],
            }]

        async def call_raw(self, name, **_arguments):
            if name == "computer_focus":
                return {"error": ""}
            if name == "computer_snapshot":
                return {"error": "", "fresh_output": '{"snapshot_id":"advanced"}'}
            raise AssertionError("act must never be dispatched while refreshing")

    async def exercise():
        state, snapshot = await refresh_exact_wps_window_state(
            FakeComputer(),
            states={
                "dialog": lambda window: window.get("title") == "Save As New Format",
                "advanced": lambda window: window.get("document_path") == "/private/tmp/copy.docx",
            },
            max_attempts=1,
            retry_delay=0,
        )
        assert state == "advanced"
        assert snapshot["snapshot_id"] == "advanced"

    run(exercise())


def test_exact_wps_state_refresh_rejects_wrong_or_ambiguous_windows() -> None:
    class FakeComputer:
        def __init__(self, windows) -> None:
            self.windows = windows
            self.calls: list[str] = []

        async def apps(self):
            return [{"app_ref": "wps", "bundle_id": WPS_BUNDLE, "windows": self.windows}]

        async def call_raw(self, name, **_arguments):
            self.calls.append(name)
            raise AssertionError("wrong window must not be focused")

    async def exercise():
        wrong = FakeComputer([{"window_ref": "wrong", "title": "Account Login"}])
        with pytest.raises(AssertionError, match="timed out"):
            await refresh_exact_wps_window_state(
                wrong,
                states={"dialog": lambda window: window.get("title") == "Save As New Format"},
                max_attempts=2,
                retry_delay=0,
            )
        assert wrong.calls == []

        ambiguous = FakeComputer([
            {"window_ref": "one", "title": "Save As New Format"},
            {"window_ref": "two", "title": "Save As New Format"},
        ])
        with pytest.raises(AssertionError, match="exactly one"):
            await refresh_exact_wps_window_state(
                ambiguous,
                states={"dialog": lambda window: window.get("title") == "Save As New Format"},
                max_attempts=1,
                retry_delay=0,
            )
        assert ambiguous.calls == []

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_test_owned_wps_cleanup_removes_pollution_and_preserves_unrelated_window(tmp_path: Path) -> None:
    source = tmp_path / "wps-source-a1b2c.docx"
    source.write_bytes(b"test")
    class FakeComputer:
        def __init__(self) -> None:
            self.windows = [
                {"window_ref": "source", "title": source.name, "document_path": str(source)},
                {"window_ref": "prompt", "title": "Save As New Format", "document_path": str(source)},
                {"window_ref": "personal", "title": "Personal notes.docx"},
            ]
            self.focused = ""
            self.close_requested = False
            self.calls: list[tuple[str, dict]] = []

        async def apps(self):
            return [{"app_ref": "wps", "bundle_id": WPS_BUNDLE, "windows": list(self.windows)}]

        async def call_raw(self, name, **arguments):
            self.calls.append((name, arguments))
            if name == "computer_focus":
                self.focused = arguments["window_ref"]
                return {"error": ""}
            if name == "computer_snapshot":
                if self.focused == "prompt":
                    tree = {"children": [
                        {"role": "AXStaticText", "label": "wps-source-a1b2c.docx"},
                        {"role": "AXButton", "title": "Cancel", "element_ref": "cancel"},
                    ]}
                elif self.close_requested:
                    tree = {"children": [{
                        "role": "AXButton",
                        "title": "Don't Save",
                        "element_ref": "discard",
                    }]}
                else:
                    tree = {"role": "AXWindow"}
                return {
                    "error": "",
                    "fresh_output": json.dumps({"snapshot_id": "fresh", "ax_tree": tree}),
                }
            if name == "computer_act":
                action = arguments["actions"][0]
                if action.get("element_ref") == "cancel":
                    self.windows = [window for window in self.windows if window["window_ref"] != "prompt"]
                elif action.get("type") == "keypress":
                    self.close_requested = True
                elif action.get("element_ref") == "discard":
                    self.windows = [window for window in self.windows if window["window_ref"] != "source"]
                else:
                    raise AssertionError(f"unexpected cleanup action {action!r}")
                return {"error": ""}
            raise AssertionError(f"unexpected tool {name}")

    async def exercise():
        computer = FakeComputer()
        cleaned = await cleanup_test_owned_wps_windows(
            computer,
            owned_names={"wps-source-a1b2c.docx"},
            workspace=tmp_path,
            retry_delay=0,
        )
        assert cleaned == ["wps-source-a1b2c.docx"]
        assert [window["title"] for window in computer.windows] == ["Personal notes.docx"]

        computer.windows.append({"window_ref": "new", "title": "wps-source-d4e5f.docx"})
        state, snapshot = await refresh_exact_wps_window_state(
            computer,
            states={"new": lambda window: window.get("title") == "wps-source-d4e5f.docx"},
            max_attempts=1,
            retry_delay=0,
        )
        assert state == "new" and snapshot["snapshot_id"] == "fresh"
        assert "Personal notes.docx" in [window["title"] for window in computer.windows]

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_test_owned_wps_cleanup_refuses_ambiguous_prompt_ownership(tmp_path: Path) -> None:
    one = tmp_path / "wps-source-aaaaa.docx"
    two = tmp_path / "wps-source-bbbbb.docx"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    class FakeComputer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def apps(self):
            return [{
                "app_ref": "wps",
                "bundle_id": WPS_BUNDLE,
                "windows": [
                    {"window_ref": "one", "title": one.name, "document_path": str(one)},
                    {"window_ref": "two", "title": two.name, "document_path": str(two)},
                    {"window_ref": "prompt", "title": "Save As New Format"},
                ],
            }]

        async def call_raw(self, name, **_arguments):
            self.calls.append(name)
            assert name == "computer_handoff"
            return {"error": ""}

    async def exercise():
        computer = FakeComputer()
        with pytest.raises(AssertionError, match="ambiguous"):
            await cleanup_test_owned_wps_windows(
                computer,
                owned_names={one.name, two.name},
                workspace=tmp_path,
                retry_delay=0,
            )
        assert computer.calls == ["computer_handoff"]

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_test_owned_wps_cleanup_preserves_unrelated_same_title_prompt(tmp_path: Path) -> None:
    source = tmp_path / "wps-source-abc12.docx"
    source.write_bytes(b"test")
    class FakeComputer:
        def __init__(self) -> None:
            self.act_calls = 0
            self.focused = ""

        async def apps(self):
            return [{
                "app_ref": "wps",
                "bundle_id": WPS_BUNDLE,
                "windows": [
                    {"window_ref": "owned", "title": source.name, "document_path": str(source)},
                    {"window_ref": "prompt", "title": "Save As New Format", "document_path": "/Users/example/Personal notes.docx"},
                ],
            }]

        async def call_raw(self, name, **arguments):
            if name == "computer_focus":
                self.focused = arguments["window_ref"]
                return {"error": ""}
            if name == "computer_snapshot":
                assert self.focused == "prompt"
                return {
                    "error": "",
                    "fresh_output": json.dumps({
                        "snapshot_id": "prompt",
                        "ax_tree": {"children": [
                            {"role": "AXStaticText", "label": "Personal notes.docx"},
                            {"role": "AXButton", "title": "Cancel", "element_ref": "cancel"},
                        ]},
                    }),
                }
            if name == "computer_act":
                self.act_calls += 1
            if name == "computer_handoff":
                return {"error": ""}
            raise AssertionError(f"unexpected tool {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        with pytest.raises(AssertionError, match="ownership"):
            await cleanup_test_owned_wps_windows(
                computer,
                owned_names={"wps-source-abc12.docx"},
                workspace=tmp_path,
                retry_delay=0,
            )
        assert computer.act_calls == 0

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_test_owned_wps_cleanup_accepts_observed_unknown_cancel_without_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wps-source-abc12.docx"
    source.write_bytes(b"test")
    class FakeComputer:
        def __init__(self) -> None:
            self.windows = [
                {"window_ref": "source", "title": source.name, "document_path": str(source)},
                {"window_ref": "prompt", "title": "Save As New Format", "document_path": str(source)},
            ]
            self.focused = ""
            self.act_calls = 0
            self.handoff_calls = 0

        async def apps(self):
            return [{"app_ref": "wps", "bundle_id": WPS_BUNDLE, "windows": self.windows}]

        async def call_raw(self, name, **arguments):
            if name == "computer_focus":
                self.focused = arguments["window_ref"]
                return {"error": ""}
            if name == "computer_snapshot":
                assert self.focused == "prompt"
                return {
                    "error": "",
                    "fresh_output": json.dumps({
                        "snapshot_id": "prompt-snapshot",
                        "ax_tree": {"children": [
                            {"role": "AXStaticText", "label": "wps-source-abc12.docx"},
                            {"role": "AXButton", "title": "Cancel", "element_ref": "cancel"},
                        ]},
                    }),
                }
            if name == "computer_act":
                self.act_calls += 1
                assert arguments["actions"] == [{"type": "click", "element_ref": "cancel"}]
                self.windows = []
                return {"error": "input may have happened", "code": "unknown_outcome"}
            if name == "computer_handoff":
                self.handoff_calls += 1
                return {"error": ""}
            raise AssertionError(f"unexpected tool {name}")

    async def exercise():
        computer = FakeComputer()
        cleaned = await cleanup_test_owned_wps_windows(
            computer,
            owned_names={"wps-source-abc12.docx"},
            workspace=tmp_path,
            retry_delay=0,
        )
        assert cleaned == []
        assert computer.act_calls == 1
        assert computer.handoff_calls == 0
        assert computer.windows == []

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_test_owned_wps_cleanup_observes_unknown_document_close_without_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wps-source-cd345.docx"
    source.write_bytes(b"test")

    class FakeComputer:
        def __init__(self) -> None:
            self.windows = [{
                "window_ref": "source",
                "title": source.name,
                "document_path": str(source),
            }]
            self.close_requested = False
            self.act_calls = 0
            self.handoff_calls = 0

        async def apps(self):
            return [{"app_ref": "wps", "bundle_id": WPS_BUNDLE, "windows": self.windows}]

        async def call_raw(self, name, **arguments):
            if name == "computer_focus":
                return {"error": ""}
            if name == "computer_snapshot":
                tree = (
                    {"children": [{
                        "role": "AXButton",
                        "title": "Don't Save",
                        "element_ref": "discard",
                    }]}
                    if self.close_requested
                    else {"role": "AXWindow"}
                )
                return {
                    "error": "",
                    "fresh_output": json.dumps({"snapshot_id": "fresh", "ax_tree": tree}),
                }
            if name == "computer_act":
                self.act_calls += 1
                action = arguments["actions"][0]
                if action.get("type") == "keypress":
                    self.close_requested = True
                elif action.get("element_ref") == "discard":
                    self.windows = []
                else:
                    raise AssertionError(f"unexpected cleanup action {action!r}")
                return {"error": "input may have happened", "code": "unknown_outcome"}
            if name == "computer_handoff":
                self.handoff_calls += 1
                return {"error": ""}
            raise AssertionError(f"unexpected tool {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        cleaned = await cleanup_test_owned_wps_windows(
            computer,
            owned_names={source.name},
            workspace=tmp_path,
            retry_delay=0,
        )
        assert cleaned == [source.name]
        assert computer.act_calls == 2
        assert computer.handoff_calls == 0

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_test_owned_wps_cleanup_unwinds_exact_export_stack_before_document(tmp_path: Path) -> None:
    source = tmp_path / "wps-source-def34.docx"
    source.write_bytes(b"test")
    class FakeComputer:
        def __init__(self) -> None:
            self.windows = [
                {"window_ref": "source", "title": source.name, "document_path": str(source)},
                {"window_ref": "export", "title": "Export as PDF", "document_path": str(source)},
                {"window_ref": "rename", "title": "Change Output Name", "document_path": str(source)},
                {"window_ref": "personal", "title": "Personal notes.docx"},
            ]
            self.focused = ""
            self.close_requested = False
            self.actions: list[dict] = []

        async def apps(self):
            return [{"app_ref": "wps", "bundle_id": WPS_BUNDLE, "windows": list(self.windows)}]

        async def call_raw(self, name, **arguments):
            if name == "computer_focus":
                self.focused = arguments["window_ref"]
                return {"error": ""}
            if name == "computer_snapshot":
                if self.focused == "rename":
                    tree = {"children": [
                        {"role": "AXStaticText", "label": "wps-source-def34"},
                        {"role": "AXButton", "title": "Cancel", "element_ref": "cancel-rename"},
                    ]}
                elif self.focused == "export":
                    tree = {"children": [
                        {"role": "AXStaticText", "label": "wps-source-def34"},
                        {
                            "role": "AXButton",
                            "subrole": "AXCloseButton",
                            "enabled": True,
                            "element_ref": "close-export",
                        },
                    ]}
                elif self.close_requested:
                    tree = {"children": [{
                        "role": "AXButton",
                        "title": "Don't Save",
                        "element_ref": "discard",
                    }]}
                else:
                    tree = {"role": "AXWindow"}
                return {
                    "error": "",
                    "fresh_output": json.dumps({"snapshot_id": "fresh", "ax_tree": tree}),
                }
            if name == "computer_act":
                action = arguments["actions"][0]
                self.actions.append(action)
                if action.get("element_ref") == "cancel-rename":
                    self.windows = [window for window in self.windows if window["window_ref"] != "rename"]
                elif action.get("element_ref") == "close-export":
                    self.windows = [window for window in self.windows if window["window_ref"] != "export"]
                elif self.focused == "source" and action.get("type") == "keypress":
                    self.close_requested = True
                elif action.get("element_ref") == "discard":
                    self.windows = [window for window in self.windows if window["window_ref"] != "source"]
                else:
                    raise AssertionError(f"unexpected cleanup action {action!r}")
                return {"error": ""}
            raise AssertionError(f"unexpected tool {name}")

    async def exercise():
        computer = FakeComputer()
        cleaned = await cleanup_test_owned_wps_windows(
            computer,
            owned_names={"wps-source-def34.docx"},
            workspace=tmp_path,
            retry_delay=0,
        )
        assert cleaned == ["wps-source-def34.docx"]
        assert [window["title"] for window in computer.windows] == ["Personal notes.docx"]
        assert computer.actions == [
            {"type": "click", "element_ref": "cancel-rename"},
            {"type": "click", "element_ref": "close-export"},
            {"type": "keypress", "key": "w", "modifiers": ["command"]},
            {"type": "click", "element_ref": "discard"},
        ]

    run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_test_owned_wps_cleanup_accepts_owned_export_as_only_catalog_window(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wps-source-fed12.docx"
    source.write_bytes(b"test")

    class FakeComputer:
        def __init__(self) -> None:
            self.windows = [{
                "window_ref": "export",
                "title": "Export as PDF",
                "document_path": str(source),
            }]
            self.focused = ""
            self.actions: list[dict] = []

        async def apps(self):
            return [{"app_ref": "wps", "bundle_id": WPS_BUNDLE, "windows": list(self.windows)}]

        async def call_raw(self, name, **arguments):
            if name == "computer_focus":
                self.focused = arguments["window_ref"]
                return {"error": ""}
            if name == "computer_snapshot":
                tree = (
                    {
                        "children": [{
                            "role": "AXButton",
                            "subrole": "AXCloseButton",
                            "enabled": True,
                            "element_ref": "close-export",
                        }],
                    }
                    if self.focused == "export"
                    else {"role": "AXWindow"}
                )
                return {
                    "error": "",
                    "fresh_output": json.dumps({"snapshot_id": "fresh", "ax_tree": tree}),
                }
            if name == "computer_act":
                action = arguments["actions"][0]
                self.actions.append(action)
                if action.get("element_ref") == "close-export":
                    self.windows = [{
                        "window_ref": "source",
                        "title": source.name,
                        "document_path": str(source),
                    }]
                elif self.focused == "source":
                    self.windows = []
                return {"error": ""}
            raise AssertionError(f"unexpected tool {name}")

    async def exercise() -> None:
        computer = FakeComputer()
        cleaned = await cleanup_test_owned_wps_windows(
            computer,
            owned_names={source.name},
            workspace=tmp_path,
            retry_delay=0,
        )
        assert cleaned == [source.name]
        assert computer.actions == [
            {"type": "click", "element_ref": "close-export"},
            {"type": "keypress", "key": "w", "modifiers": ["command"]},
        ]

    run(exercise())


def test_wps_format_confirmation_selector_fails_closed() -> None:
    expected = {"role": "AXButton", "title": "保存新格式", "element_ref": "expected"}
    cancel = {"role": "AXButton", "title": "取消", "element_ref": "cancel"}
    assert exact_format_confirmation_button({"children": [cancel, expected]}) == expected
    with pytest.raises(AssertionError, match="exactly one trusted"):
        exact_format_confirmation_button({"children": [cancel]})
    with pytest.raises(AssertionError, match="exactly one trusted"):
        exact_format_confirmation_button({"children": [expected, dict(expected)]})


@pytest.mark.skipif(os.name != "posix", reason="WPS acceptance harness verifies POSIX owned paths")
def test_wps_export_selectors_fail_closed(tmp_path: Path) -> None:
    custom = {
        "role": "AXStaticText",
        "value": "自定义文件夹",
        "actions": ["AXPress"],
        "element_ref": "custom",
    }
    start = {"role": "AXButton", "title": "开始输出", "element_ref": "start"}
    tree = {"children": [custom, start]}
    assert exact_ax_control(
        tree,
        roles={"AXStaticText"},
        labels=WPS_CUSTOM_FOLDER_LABELS,
        description="custom folder",
    ) == custom
    with pytest.raises(AssertionError, match="exactly one trusted"):
        exact_ax_control(
            {"children": [custom, dict(custom)]},
            roles={"AXStaticText"},
            labels=WPS_CUSTOM_FOLDER_LABELS,
            description="custom folder",
        )
    with pytest.raises(AssertionError, match="exactly one trusted"):
        exact_ax_control(
            {"children": [{"role": "AXButton", "title": "Cancel"}]},
            roles={"AXButton"},
            labels=WPS_START_EXPORT_LABELS,
            description="start export",
        )

    popup = {
        "role": "AXPopUpButton",
        "title": "自定义文件夹",
        "bounds": {"x": 700, "y": 500, "width": 240, "height": 28},
    }
    folder_button = {
        "role": "AXButton",
        "enabled": True,
        "actions": ["AXPress"],
        "element_ref": "folder",
        "bounds": {"x": 916, "y": 540, "width": 20, "height": 20},
    }
    assert exact_wps_custom_folder_button(
        {"children": [folder_button]}, popup
    ) == folder_button
    with pytest.raises(AssertionError, match="exactly one adjacent trusted"):
        exact_wps_custom_folder_button(
            {"children": [folder_button, dict(folder_button)]}, popup
        )
    with pytest.raises(AssertionError, match="exactly one adjacent trusted"):
        exact_wps_custom_folder_button(
            {
                "children": [{
                    **folder_button,
                    "bounds": {"x": 100, "y": 100, "width": 20, "height": 20},
                }]
            },
            popup,
        )

    expected = tmp_path / "private"
    expected.mkdir(mode=0o700)
    path_node = {"role": "AXStaticText", "label": str(expected) + "/"}
    assert exact_canonical_directory_node({"children": [path_node]}, expected) == path_node
    with pytest.raises(AssertionError, match="exactly one canonical"):
        exact_canonical_directory_node(
            {"children": [{"role": "AXStaticText", "label": str(tmp_path)}]},
            expected,
        )

    remembered_user_directory = tmp_path / "remembered-user-export"
    remembered_user_directory.mkdir(mode=0o700)
    matching_name_wrong_directory = {
        "children": [
            {"role": "AXStaticText", "label": str(remembered_user_directory)},
            {"role": "AXStaticText", "label": "wabcde"},
        ],
    }
    with pytest.raises(AssertionError, match="exactly one canonical"):
        exact_canonical_directory_node(matching_name_wrong_directory, expected)

    export_window = {"window_ref": "export", "title": "输出为PDF"}
    wps = {
        "app_ref": "wps",
        "bundle_id": WPS_BUNDLE,
        "windows": [{"window_ref": "document", "title": "copy.docx"}, export_window],
    }
    other = {
        "app_ref": "other",
        "bundle_id": "com.example.other",
        "windows": [dict(export_window)],
    }
    assert exact_wps_window_refs([other, wps], WPS_EXPORT_WINDOW_TITLES) == (
        "wps",
        "export",
    )
    with pytest.raises(AssertionError, match="exactly one trusted"):
        exact_wps_window_refs([other], WPS_EXPORT_WINDOW_TITLES)
    with pytest.raises(AssertionError, match="exactly one trusted"):
        exact_wps_window_refs([wps, dict(wps)], WPS_EXPORT_WINDOW_TITLES)

    assert approval_path_is_within_workspace(str(expected / "future.pdf"), tmp_path)
    assert not approval_path_is_within_workspace("/Users/example/existing.pdf", tmp_path)


@pytest.mark.real_macos_computer
def test_wps_edit_save_copy_and_export_uses_exact_path_approvals(computer_workspace):
    suffix = uuid.uuid4().hex[:5]
    source = computer_workspace / f"wps-source-{suffix}.docx"
    copy = computer_workspace / f"w{suffix}.docx"
    pdf = computer_workspace / f"w{suffix}.pdf"
    save_alias = Path("/tmp") / f"astrae2e{suffix}"
    save_alias.symlink_to(computer_workspace, target_is_directory=True)
    create_minimal_docx(source, marker="ASTRA_WPS_ORIGINAL")

    async def workflow() -> None:
        computer = ComputerHarness(computer_workspace)
        export_workflow = WPSExportStateMachine(computer_workspace, pdf.stem)

        async def replace_focused_text(snapshot: dict, element_ref: str, value: str) -> dict:
            return await replace_focused_wps_text(
                computer,
                snapshot,
                element_ref,
                value,
                workflow=export_workflow,
            )

        async def observe_after(result, *, title: str) -> dict:
            return await observe_known_wps_action(
                computer,
                result,
                step=f"observe WPS action for {title}",
                workflow=export_workflow,
            )

        async def known_act(snapshot: dict, actions: list[dict], *, step: str) -> dict:
            result = await computer.call_raw(
                "computer_act",
                snapshot_id=snapshot["snapshot_id"],
                actions=actions,
            )
            return await observe_known_wps_action(
                computer, result, step=step, workflow=export_workflow
            )

        async def wait_for_ax(snapshot: dict, predicate, description: str) -> dict:
            deadline = asyncio.get_running_loop().time() + 8
            current = snapshot
            last_error = None
            while asyncio.get_running_loop().time() < deadline:
                if find_nodes(current.get("ax_tree", {}), predicate):
                    return current
                await asyncio.sleep(0.1)
                result = await computer.call_raw("computer_snapshot", scope="target_window")
                if result.get("error"):
                    last_error = result
                    continue
                current = json.loads(result["fresh_output"])
            raise AssertionError(
                f"timed out waiting for {description}; last_error={last_error!r}"
            )

        async def refresh_wps_snapshot(title: str) -> dict:
            deadline = asyncio.get_running_loop().time() + 8
            last_error = None
            while asyncio.get_running_loop().time() < deadline:
                try:
                    app_ref, window_ref, _window = await computer.wait_for_window(
                        bundle_id=WPS_BUNDLE,
                        title=title,
                        timeout=1,
                    )
                except AssertionError as error:
                    last_error = error
                    continue
                focus_result = await computer.call_raw(
                    "computer_focus",
                    app_ref=app_ref,
                    window_ref=window_ref,
                )
                if focus_result.get("error"):
                    last_error = focus_result
                    await asyncio.sleep(0.1)
                    continue
                snapshot_result = await computer.call_raw(
                    "computer_snapshot",
                    scope="target_window",
                )
                if not snapshot_result.get("error"):
                    return json.loads(snapshot_result["fresh_output"])
                last_error = snapshot_result
                await asyncio.sleep(0.1)
            raise AssertionError(
                f"timed out refreshing WPS window {title!r}; last_error={last_error!r}"
            )

        async def refresh_exact_wps_window(titles: set[str]) -> dict:
            expected = {normalized_ax_label(value) for value in titles}
            _state, snapshot = await refresh_exact_wps_window_state(
                computer,
                states={
                    "expected": lambda window: normalized_ax_label(window.get("title"))
                    in expected,
                },
                max_attempts=80,
                retry_delay=0.1,
            )
            return snapshot

        try:
            await open_application("-a", "WPS Office", str(source))
            app_ref, window_ref, window = await computer.wait_for_window(
                bundle_id=WPS_BUNDLE,
                title=source.name,
                timeout=25,
            )
            assert window.get("document_path") in {None, str(source)}
            await asyncio.sleep(1)
            app_ref, window_ref, window = await computer.wait_for_window(
                bundle_id=WPS_BUNDLE,
                title=source.name,
                timeout=10,
            )
            assert window.get("document_path") in {None, str(source)}
            await computer.focus(app_ref, window_ref)
            before = await computer.snapshot()
            editor = select_wps_editor_canvas(
                before["ax_tree"], before.get("capture_bounds", {})
            )
            focused = await known_act(
                before,
                [wps_editor_focus_action(editor)],
                step="focus WPS editor",
            )
            select_result = await computer.call_raw(
                "computer_act",
                snapshot_id=focused["snapshot_id"],
                actions=[{"type": "keypress", "key": "a", "modifiers": ["command"]}],
            )
            selected = await observe_after(select_result, title=source.name)
            selection_settle_result = await retry_preinput_stale_once(
                computer.call_raw,
                lambda: refresh_wps_snapshot(source.name),
                snapshot=selected,
                actions=[{"type": "wait", "duration_ms": 2_000}],
            )
            selected = await observe_known_wps_action(
                computer,
                selection_settle_result,
                step="settle WPS selection before document edit",
                workflow=export_workflow,
            )
            edit_result = await retry_preinput_stale_once(
                computer.call_raw,
                lambda: refresh_wps_snapshot(source.name),
                snapshot=selected,
                actions=wps_editor_replacement_actions("ASTRA_WPS_EDITED"),
            )
            edited = await observe_known_wps_action(
                computer, edit_result, step="edit WPS document", workflow=export_workflow
            )
            edit_settle_result = await retry_preinput_stale_once(
                computer.call_raw,
                lambda: refresh_wps_snapshot(source.name),
                snapshot=edited,
                actions=[{"type": "wait", "duration_ms": 750}],
            )
            edited = await observe_known_wps_action(
                computer,
                edit_settle_result,
                step="settle WPS document edit before Save As",
                workflow=export_workflow,
            )

            save_as_result = await retry_preinput_stale_once(
                computer.call_raw,
                lambda: refresh_wps_snapshot(source.name),
                snapshot=edited,
                actions=wps_modal_transition_actions("s", ["command", "shift"]),
            )
            _save_state, save_panel = await observe_expected_wps_transition(
                computer,
                save_as_result,
                states={
                    "save": lambda window: normalized_ax_label(window.get("title"))
                    in {normalized_ax_label(value) for value in WPS_SAVE_AS_WINDOW_TITLES},
                },
                step="open WPS Save As",
                workflow=export_workflow,
            )
            save_panel = await wait_for_ax(
                save_panel,
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True
                and node.get("value") == source.name,
                "WPS Save As filename field",
            )
            go_to_folder_result = await retry_preinput_stale_once(
                computer.call_raw,
                lambda: refresh_exact_wps_window(WPS_SAVE_AS_WINDOW_TITLES),
                snapshot=save_panel,
                actions=wps_modal_transition_actions("g", ["command", "shift"]),
            )
            go_to_folder = await observe_known_wps_action(
                computer,
                go_to_folder_result,
                step="open WPS Save As Go-to-Folder",
                workflow=export_workflow,
            )
            go_to_folder = await wait_for_ax(
                go_to_folder,
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "WPS Save As Go to Folder field",
            )
            folder_field = first_node(
                go_to_folder["ax_tree"],
                lambda node: node.get("role") == "AXTextField" and node.get("focused") is True,
                "WPS Save As Go to Folder field",
            )
            folder_typed = await replace_focused_text(
                go_to_folder,
                folder_field["element_ref"],
                str(save_alias),
            )
            typed_folder = first_node(
                folder_typed["ax_tree"],
                lambda node: node.get("role") == "AXTextField" and node.get("focused") is True,
                "WPS typed Go to Folder field",
            )
            observed_folder = str(typed_folder.get("value") or "")
            assert observed_folder == str(save_alias), typed_folder
            folder_commit = await computer.call_raw(
                "computer_act",
                snapshot_id=folder_typed["snapshot_id"],
                actions=[
                    {
                        "type": "keypress",
                        "key": "return",
                        "element_ref": typed_folder["element_ref"],
                    },
                ],
            )
            await observe_known_wps_action(
                computer,
                folder_commit,
                step="commit WPS Save As Go-to-Folder",
                workflow=export_workflow,
            )
            save_panel = await refresh_wps_snapshot(source.name)
            save_panel = await wait_for_ax(
                save_panel,
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True
                and node.get("value") == source.name,
                "WPS Save As filename field after exact folder navigation",
            )
            save_name = first_node(
                save_panel["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True
                and node.get("value") == source.name,
                "WPS Save As filename field",
            )
            selected_name_result = await computer.call_raw(
                "computer_act",
                snapshot_id=save_panel["snapshot_id"],
                actions=[{
                    "type": "keypress",
                    "key": "a",
                    "modifiers": ["command"],
                    "element_ref": save_name["element_ref"],
                }],
            )
            selected_name = await observe_known_wps_action(
                computer,
                selected_name_result,
                step="select WPS Save As filename",
                workflow=export_workflow,
            )
            selected_name_field = first_node(
                selected_name["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "WPS selected Save As filename field",
            )
            typed_name = await replace_focused_text(
                selected_name,
                selected_name_field["element_ref"],
                copy.name,
            )
            save_button = first_node(
                typed_name["ax_tree"],
                lambda node: node.get("role") == "AXButton"
                and str(node.get("title") or node.get("label") or "") in {"保存", "Save"},
                "WPS Save button",
            )
            format_titles = {
                normalized_ax_label(value) for value in WPS_FORMAT_WINDOW_TITLES
            }
            copy_path = str(copy.resolve(strict=False))
            post_save_states = {
                "dialog": lambda window: normalized_ax_label(window.get("title"))
                in format_titles,
                "advanced": lambda window: str(window.get("document_path") or "")
                == copy_path
                or normalized_ax_label(copy.name)
                in normalized_ax_label(window.get("title")),
            }
            save_commit = await computer.call_raw(
                "computer_act",
                snapshot_id=typed_name["snapshot_id"],
                actions=[{
                    "type": "click",
                    "element_ref": save_button["element_ref"],
                }],
            )
            format_state, confirmation = await observe_expected_wps_transition(
                computer,
                save_commit,
                states=post_save_states,
                step="commit WPS Save As",
                workflow=export_workflow,
            )
            if format_state == "dialog":
                format_button = exact_format_confirmation_button(confirmation["ax_tree"])
                format_commit = await computer.call_raw(
                    "computer_act",
                    snapshot_id=confirmation["snapshot_id"],
                    actions=[{"type": "click", "element_ref": format_button["element_ref"]}],
                )
                await observe_expected_wps_transition(
                    computer,
                    format_commit,
                    states={"advanced": post_save_states["advanced"]},
                    step="confirm WPS Save As format",
                    workflow=export_workflow,
                )
            await wait_for_path(copy, timeout=20)
            document = await refresh_wps_snapshot(copy.name)

            file_button = exact_ax_control(
                document["ax_tree"],
                roles={"AXMenuBarItem"},
                labels={"文件"},
                description="WPS menu-bar File control",
            )
            file_menu = await known_act(
                document,
                [{"type": "click", "element_ref": file_button["element_ref"]}],
                step="open WPS File menu",
            )
            export = exact_ax_control(
                file_menu["ax_tree"],
                roles={"AXMenuItem"},
                labels=set(WPS_EXPORT_PDF_LABELS),
                description="WPS Export PDF control",
            )
            export_result = await computer.call_raw(
                "computer_act",
                snapshot_id=file_menu["snapshot_id"],
                actions=[{"type": "click", "element_ref": export["element_ref"]}],
            )
            _export_state, export_panel = await observe_expected_wps_transition(
                computer,
                export_result,
                states={
                    "export": lambda window: normalized_ax_label(window.get("title"))
                    in {
                        normalized_ax_label(value)
                        for value in WPS_EXPORT_WINDOW_TITLES
                    },
                },
                step="open WPS PDF export",
                workflow=export_workflow,
            )
            export_panel = await wait_for_ax(
                export_panel,
                lambda node: node.get("role") == "AXPopUpButton"
                and normalized_ax_label(node.get("title") or node.get("label"))
                in {
                    normalized_ax_label(value)
                    for value in WPS_SOURCE_FOLDER_LABELS | WPS_CUSTOM_FOLDER_LABELS
                },
                "WPS PDF output-location popup before any basename write",
            )
            location_popup = exact_ax_control(
                export_panel["ax_tree"],
                roles={"AXPopUpButton"},
                labels=WPS_SOURCE_FOLDER_LABELS | WPS_CUSTOM_FOLDER_LABELS,
                description="WPS PDF output-location popup",
            )
            location_menu = await known_act(
                export_panel,
                [{"type": "click", "element_ref": location_popup["element_ref"]}],
                step="open WPS export location menu",
            )
            custom_labels = {
                normalized_ax_label(value) for value in WPS_CUSTOM_FOLDER_LABELS
            }
            location_menu = await wait_for_ax(
                location_menu,
                lambda node: node.get("role") == "AXStaticText"
                and normalized_ax_label(
                    node.get("title") or node.get("label") or node.get("value")
                ) in custom_labels,
                "WPS custom-folder item",
            )
            custom_folder = exact_ax_control(
                location_menu["ax_tree"],
                roles={"AXStaticText"},
                labels=WPS_CUSTOM_FOLDER_LABELS,
                description="WPS custom-folder item",
            )
            custom_result = await computer.call_raw(
                "computer_act",
                snapshot_id=location_menu["snapshot_id"],
                actions=[wps_center_click_action(custom_folder)],
            )
            custom_selected = await observe_known_wps_action(
                computer,
                custom_result,
                step="select WPS custom export folder",
                workflow=export_workflow,
            )
            custom_selected = await wait_for_ax(
                custom_selected,
                lambda node: node.get("role") == "AXPopUpButton"
                and normalized_ax_label(node.get("title") or node.get("label"))
                in custom_labels,
                "WPS selected custom-folder state",
            )
            selected_popup = exact_ax_control(
                custom_selected["ax_tree"],
                roles={"AXPopUpButton"},
                labels=WPS_CUSTOM_FOLDER_LABELS,
                description="WPS selected custom-folder popup",
            )
            folder_button = exact_wps_custom_folder_button(
                custom_selected["ax_tree"], selected_popup
            )
            chooser_result = await computer.call_raw(
                "computer_act",
                snapshot_id=custom_selected["snapshot_id"],
                actions=[wps_center_click_action(folder_button)],
            )
            _chooser_state, chooser = await observe_expected_wps_transition(
                computer,
                chooser_result,
                states={
                    "chooser": lambda window: normalized_ax_label(window.get("title"))
                    in {
                        normalized_ax_label(value)
                        for value in WPS_DIRECTORY_CHOOSER_TITLES
                    },
                },
                step="open WPS custom export folder chooser",
                workflow=export_workflow,
            )
            chooser = await wait_for_ax(
                chooser,
                lambda node: node.get("role") == "AXButton"
                and normalized_ax_label(node.get("title") or node.get("label"))
                in {normalized_ax_label(value) for value in WPS_DIRECTORY_CONFIRM_LABELS},
                "WPS custom-folder chooser",
            )
            go_to_directory_result = await computer.call_raw(
                "computer_act",
                snapshot_id=chooser["snapshot_id"],
                actions=wps_modal_transition_actions("g", ["command", "shift"]),
            )
            directory_sheet = await observe_known_wps_action(
                computer,
                go_to_directory_result,
                step="open WPS export Go-to-Folder",
                workflow=export_workflow,
            )
            directory_sheet = await wait_for_ax(
                directory_sheet,
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "WPS custom-folder Go to Folder field",
            )
            directory_field = first_node(
                directory_sheet["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "WPS custom-folder Go to Folder field",
            )
            canonical_workspace = computer_workspace.resolve(strict=True)
            typed_directory = await replace_focused_text(
                directory_sheet,
                directory_field["element_ref"],
                str(canonical_workspace),
            )
            committed_directory_result = await computer.call_raw(
                "computer_act",
                snapshot_id=typed_directory["snapshot_id"],
                actions=[{"type": "keypress", "key": "return"}],
            )
            await observe_known_wps_action(
                computer,
                committed_directory_result,
                step="commit WPS export Go-to-Folder",
                workflow=export_workflow,
            )
            # Committing Go-to-Folder can publish a WPS-owned transient after
            # the immediate post-action snapshot.  Re-lock the exact chooser
            # window before resolving its confirmation button.
            chooser = await refresh_exact_wps_window(WPS_DIRECTORY_CHOOSER_TITLES)
            chooser = await wait_for_ax(
                chooser,
                lambda node: node.get("role") == "AXButton"
                and normalized_ax_label(node.get("title") or node.get("label"))
                in {normalized_ax_label(value) for value in WPS_DIRECTORY_CONFIRM_LABELS},
                "WPS custom-folder confirmation button",
            )
            directory_confirm = exact_ax_control(
                chooser["ax_tree"],
                roles={"AXButton"},
                labels=WPS_DIRECTORY_CONFIRM_LABELS,
                description="WPS custom-folder confirmation button",
            )
            directory_confirm_result = await computer.call_raw(
                "computer_act",
                snapshot_id=chooser["snapshot_id"],
                actions=[{
                    "type": "click",
                    "element_ref": directory_confirm["element_ref"],
                }],
            )
            await observe_expected_wps_transition(
                computer,
                directory_confirm_result,
                states={
                    "export": lambda window: normalized_ax_label(window.get("title"))
                    in {
                        normalized_ax_label(value)
                        for value in WPS_EXPORT_WINDOW_TITLES
                    },
                },
                step="confirm WPS export directory",
                workflow=export_workflow,
            )

            export_ready = await refresh_exact_wps_window(WPS_EXPORT_WINDOW_TITLES)
            # WPS remembers a user export directory across sessions.  Prove
            # the request-local destination before opening or committing the
            # output-name dialog, because Return in that dialog is a write.
            export_workflow.verify_destination(export_ready["ax_tree"])
            rename_button = exact_ax_control(
                export_ready["ax_tree"],
                roles={"AXButton"},
                labels=WPS_EXPORT_NAME_BUTTON_LABELS,
                description="WPS PDF rename button",
            )
            rename_result = await computer.call_raw(
                "computer_act",
                snapshot_id=export_ready["snapshot_id"],
                actions=[{"type": "click", "element_ref": rename_button["element_ref"]}],
            )
            renamed = await observe_known_wps_action(
                computer,
                rename_result,
                step="open WPS PDF rename",
                workflow=export_workflow,
            )
            renamed = await wait_for_ax(
                renamed,
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "WPS PDF basename field",
            )
            pdf_field = first_node(
                renamed["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "WPS PDF basename field",
            )
            pdf_typed = await replace_focused_text(
                renamed,
                pdf_field["element_ref"],
                pdf.stem,
            )
            typed_pdf_field = first_node(
                pdf_typed["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("focused") is True,
                "WPS typed PDF basename field",
            )
            assert typed_pdf_field.get("value") == pdf.stem, typed_pdf_field
            rename_commit = await computer.call_raw(
                "computer_act",
                snapshot_id=pdf_typed["snapshot_id"],
                actions=[{
                    "type": "keypress",
                    "key": "return",
                    "element_ref": typed_pdf_field["element_ref"],
                }],
            )
            await observe_known_wps_action(
                computer,
                rename_commit,
                step="commit WPS PDF rename",
                workflow=export_workflow,
            )
            export_workflow.rename_committed(from_snapshot_id=pdf_typed["snapshot_id"])
            export_ready = await refresh_exact_wps_window(WPS_EXPORT_WINDOW_TITLES)
            export_workflow.verify_fresh_main_panel(export_ready)
            start_export = exact_ax_control(
                export_ready["ax_tree"],
                roles={"AXButton"},
                labels=WPS_START_EXPORT_LABELS,
                description="WPS Start Export button",
            )
            export_workflow.start(snapshot_id=export_ready["snapshot_id"])
            start_result = await computer.call_raw(
                "computer_act",
                snapshot_id=export_ready["snapshot_id"],
                actions=[{"type": "click", "element_ref": start_export["element_ref"]}],
            )
            await observe_known_wps_action(
                computer,
                start_result,
                step="start WPS PDF export",
                workflow=export_workflow,
            )
            await wait_for_path(pdf, timeout=30)

            assert pdf.is_file() and not pdf.is_symlink()
            assert pdf.parent.resolve(strict=True) == canonical_workspace
            assert computer_workspace.stat().st_mode & 0o777 == 0o700
            assert "ASTRA_WPS_EDITED" in extract_docx_text(copy)
            assert "ASTRA_WPS_EDITED" in extract_pdf_text(pdf)
            high_impact = [
                item.request for item in computer.approvals
                if item.request.get("kind") == "computer_write"
                and item.request.get("choices") == ["once", "deny"]
            ]
            destination_windows = {
                normalized_ax_label(value)
                for value in (
                    WPS_SAVE_AS_WINDOW_TITLES
                    | WPS_FORMAT_WINDOW_TITLES
                    | WPS_EXPORT_WINDOW_TITLES
                    | WPS_EXPORT_RENAME_WINDOW_TITLES
                )
            }
            destination_requests = [
                request for request in high_impact
                if normalized_ax_label(request["arguments"].get("window"))
                in destination_windows
                and request["arguments"].get("known_path")
            ]
            approved_paths = {
                request["arguments"]["known_path"] for request in destination_requests
            }
            assert approved_paths == {str(copy), str(pdf)}, [
                (
                    item.request.get("choices"),
                    item.request.get("arguments", {}).get("window"),
                    item.request.get("arguments", {}).get("known_path"),
                )
                for item in computer.approvals
                if item.request.get("choices") == ["once", "deny"]
                and item.request.get("arguments", {}).get("known_path")
            ]
            assert all(request["scope"].startswith("computer-write-batch:") for request in high_impact)
        finally:
            primary_error = sys.exc_info()[1]
            try:
                try:
                    if export_workflow.stopped:
                        raise WPSUnknownOutcome(
                            "skipping automated WPS cleanup after unknown outcome"
                        )
                    await cleanup_test_owned_wps_windows(
                        computer,
                        owned_names={source.name, copy.name},
                        workspace=computer_workspace,
                    )
                except Exception as cleanup_error:
                    if primary_error is None:
                        raise
                    primary_error.add_note(
                        "test-owned WPS cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            finally:
                try:
                    await computer.close()
                finally:
                    save_alias.unlink(missing_ok=True)

    run(workflow())
