from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from .fixtures import ComputerHarness, first_node, open_application, run


@pytest.mark.real_macos_computer
def test_terminal_printf_requires_separate_exact_batch_approval_before_enter(computer_workspace):
    terminal_root = computer_workspace / f"terminal-{uuid.uuid4().hex[:10]}"
    terminal_root.mkdir(mode=0o700)
    marker = "ASTRA_TERMINAL_E2E_MARKER"
    command = f"printf '{marker}\\n'"

    async def workflow() -> None:
        computer = ComputerHarness(computer_workspace)
        try:
            await open_application("-a", "Terminal", str(terminal_root))
            app_ref, window_ref, _window = await computer.wait_for_window(
                bundle_id="com.apple.Terminal",
                title=terminal_root.name,
            )
            await asyncio.sleep(0.5)
            await computer.focus(app_ref, window_ref)
            before = await computer.snapshot()
            first_node(
                before["ax_tree"],
                lambda node: node.get("focused") is True
                and node.get("role") in {"AXTextArea", "AXTextField"},
                "focused Terminal input",
            )
            typed = await computer.act(
                before["snapshot_id"],
                [{"type": "type", "text": command}],
            )
            before_enter_requests = len(
                [item for item in computer.approvals if item.request.get("kind") == "computer_write"]
            )
            executed = await computer.act(
                typed["snapshot_id"],
                [{"type": "keypress", "key": "return"}],
            )
            after_enter_requests = [
                item.request for item in computer.approvals if item.request.get("kind") == "computer_write"
            ]
            assert before_enter_requests >= 1
            assert len(after_enter_requests) == before_enter_requests + 1
            assert all(request["choices"] == ["once", "deny"] for request in after_enter_requests)
            assert all(request["scope"].startswith("computer-write-batch:") for request in after_enter_requests)
            assert marker in json.dumps(executed["ax_tree"], ensure_ascii=False)
        finally:
            await computer.close()

    run(workflow())
