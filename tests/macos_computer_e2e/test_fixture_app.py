from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from .fixtures import (
    SECURE_MARKER,
    ComputerHarness,
    create_minimal_docx,
    first_node,
    launched_fixture,
    run,
)


def test_fixture_source_exposes_ordinary_ax_controls_and_deterministic_canvas_counters() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "native/macos-computer-helper/Sources/AstraComputerFixture/main.swift"
    ).read_text(encoding="utf-8")

    for identifier in (
        "astra.button",
        "astra.double_click",
        "astra.scroll",
        "astra.drag",
        "astra.canvas",
        "astra.click_count",
        "astra.double_click_count",
        "astra.scroll_count",
        "astra.drag_count",
        "astra.canvas_count",
    ):
        assert identifier in source
    assert source.count("override func isAccessibilityElement() -> Bool { true }") >= 3
    assert source.count("setAccessibilitySubrole") >= 3
    assert "applicationShouldHandleReopen" in source
    assert "window.makeFirstResponder(secure)" not in source


def test_create_minimal_docx_is_a_dependency_free_ooxml_package(tmp_path):
    output = tmp_path / "fixture.docx"

    create_minimal_docx(output, marker="ASTRA_DOCX_MARKER")

    assert output.is_file()
    if os.name == 'posix':
        assert output.stat().st_mode & 0o077 == 0
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
        }
        assert b"ASTRA_DOCX_MARKER" in archive.read("word/document.xml")


@pytest.mark.real_macos_computer
def test_fixture_uses_public_tools_and_signed_production_helper_without_secure_leak(
    computer_workspace,
    caplog,
):
    async def workflow() -> None:
        computer = ComputerHarness(computer_workspace)
        try:
            status = await computer.status()
            assert status["permissions"] == {"accessibility": True, "screen_recording": True}
            app_ref, window_ref, _window = await computer.wait_for_window(
                app_name="AstraComputerFixture",
                title="Astra Computer Fixture",
            )
            await computer.focus(app_ref, window_ref)
            before = await computer.snapshot()
            serialized = json.dumps(before, ensure_ascii=False)
            assert SECURE_MARKER not in serialized

            text = first_node(
                before["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("subrole") != "AXSecureTextField",
                "fixture non-secure text field",
            )
            clicked = await computer.act(
                before["snapshot_id"],
                [{"type": "click", "element_ref": text["element_ref"]}],
            )
            focused = first_node(
                clicked["ax_tree"],
                lambda node: node.get("role") == "AXTextField"
                and node.get("subrole") != "AXSecureTextField"
                and node.get("focused") is True,
                "focused fixture text field",
            )
            marker = "ASTRA_FIXTURE_PUBLIC_TOOL_LOOP"
            after = await computer.act(
                clicked["snapshot_id"],
                [{"type": "type", "text": marker, "element_ref": focused["element_ref"]}],
            )
            values = json.dumps(after["ax_tree"], ensure_ascii=False)
            assert f"text:{marker}" in values
            leak_surface = json.dumps(
                {"events": computer.events, "approvals": [item.request for item in computer.approvals], "audits": computer.audits},
                ensure_ascii=False,
            ) + caplog.text
            assert SECURE_MARKER not in leak_surface
            assert all("fresh_output" not in json.dumps(event) for event in computer.events)
        finally:
            await computer.close()

    import json

    with launched_fixture():
        run(workflow())
