from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from .fixtures import ComputerHarness, find_nodes, first_node, open_application, run, wait_for_path


@pytest.mark.real_macos_computer
def test_finder_copy_rename_and_denied_trash_preserves_private_fixture(computer_workspace):
    finder_root = computer_workspace / f"finder-{uuid.uuid4().hex[:10]}"
    finder_root.mkdir(mode=0o700)
    source = finder_root / "finder-source.txt"
    source.write_text("ASTRA_FINDER_SOURCE", encoding="utf-8")
    source.chmod(0o600)
    renamed = finder_root / "finder-renamed.txt"
    deny_trash = False

    def decide(request):
        if deny_trash and request.get("kind") == "computer_write":
            return "deny"
        return "once"

    async def workflow() -> None:
        nonlocal deny_trash
        computer = ComputerHarness(computer_workspace, approval_decider=decide)
        try:
            await open_application(str(finder_root))
            app_ref, window_ref, _window = await computer.wait_for_window(
                bundle_id="com.apple.finder",
                title=finder_root.name,
            )
            await asyncio.sleep(0.5)
            await computer.focus(app_ref, window_ref)
            snapshot = await computer.snapshot()
            file_view = first_node(
                snapshot["ax_tree"],
                lambda node: node.get("role") == "AXOutline"
                and str(node.get("label") or "") in {"List View", "列表视图"},
                "Finder list view",
            )
            file_rows = find_nodes(
                file_view,
                lambda node: node.get("role") == "AXRow"
                and isinstance(node.get("bounds"), dict),
            )
            assert file_rows, "Finder fixture row is missing"
            file_row = max(file_rows, key=lambda node: node["bounds"]["y"])
            bounds = file_row["bounds"]
            selected = await computer.act(
                snapshot["snapshot_id"],
                [{
                    "type": "click",
                    "x": bounds["x"] + min(40, bounds["width"] / 2),
                    "y": bounds["y"] + bounds["height"] / 2,
                }],
            )
            duplicate_result = await computer.call_raw(
                "computer_act",
                snapshot_id=selected["snapshot_id"],
                actions=[{"type": "keypress", "key": "d", "modifiers": ["command"]}],
            )
            duplicate = None
            for _ in range(100):
                candidates = [
                    path for path in finder_root.iterdir()
                    if path.name.startswith("finder-source") and path != source
                ]
                if len(candidates) == 1:
                    duplicate = candidates[0]
                    break
                await asyncio.sleep(0.1)
            assert duplicate is not None, "Finder did not create exactly one fixture copy"
            if duplicate_result.get("error"):
                assert duplicate_result["code"] == "unknown_outcome"
                pasted = await computer.snapshot()
            else:
                pasted = json.loads(duplicate_result["fresh_output"])

            rename_started = await computer.act(
                pasted["snapshot_id"],
                [{"type": "keypress", "key": "return"}],
            )
            rename_field = first_node(
                rename_started["ax_tree"],
                lambda node: node.get("role") == "AXTextField" and node.get("focused") is True,
                "Finder rename field",
            )
            renamed_pending = await computer.act(
                rename_started["snapshot_id"],
                [
                    {"type": "keypress", "key": "a", "modifiers": ["command"], "element_ref": rename_field["element_ref"]},
                    *({"type": "keypress", "key": character} for character in renamed.name),
                ],
            )
            commit_result = await computer.call_raw(
                "computer_act",
                snapshot_id=renamed_pending["snapshot_id"],
                actions=[{"type": "keypress", "key": "return"}],
            )
            await wait_for_path(renamed)
            await wait_for_path(duplicate, present=False)
            if commit_result.get("error"):
                assert commit_result["code"] == "unknown_outcome"
                committed = await computer.snapshot()
            else:
                committed = json.loads(commit_result["fresh_output"])

            deny_trash = True
            denied = await computer.call_raw(
                "computer_act",
                snapshot_id=committed["snapshot_id"],
                actions=[{"type": "keypress", "key": "delete", "modifiers": ["command"]}],
            )
            assert denied["code"] == "approval_denied"
            assert source.read_text(encoding="utf-8") == "ASTRA_FINDER_SOURCE"
            assert renamed.read_text(encoding="utf-8") == "ASTRA_FINDER_SOURCE"

            computer_requests = [item.request for item in computer.approvals if item.request.get("kind") == "computer_write"]
            assert computer_requests
            path_sequence = [request["arguments"].get("known_path") for request in computer_requests]
            trusted_lifecycle = [str(finder_root), str(source), str(duplicate), str(renamed)]
            assert set(path_sequence) == set(trusted_lifecycle), path_sequence
            lifecycle_rank = {path: index for index, path in enumerate(trusted_lifecycle)}
            assert [lifecycle_rank[path] for path in path_sequence] == sorted(
                lifecycle_rank[path] for path in path_sequence
            ), path_sequence
            assert all(request["scope"].startswith("computer-write") for request in computer_requests)
            assert computer_requests[-1]["choices"] == ["once", "deny"]
        finally:
            await computer.close()

    run(workflow())
