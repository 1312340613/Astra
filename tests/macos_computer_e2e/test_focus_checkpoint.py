"""Real AppKit focus movement through the signed candidate and public CU tools."""

import json
import plistlib
import shutil
import subprocess
import uuid

import pytest

from .fixtures import ComputerHarness, first_node, fixture_binary, observe_focused_text_field, run


@pytest.mark.real_macos_computer
def test_tab_checkpoint_keeps_suffix_out_of_new_field(computer_workspace):
    bundle = computer_workspace / "AstraCheckpointFixture.app"
    executable_dir = bundle / "Contents/MacOS"
    executable_dir.mkdir(parents=True)
    executable = executable_dir / "AstraComputerFixture"
    shutil.copy2(fixture_binary(), executable)
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "dev.astra.checkpoint-fixture",
        "CFBundleExecutable": executable.name, "CFBundleName": "AstraCheckpointFixture",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1", "LSUIElement": False,
    }))
    process = subprocess.Popen([executable], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def workflow():
        computer = ComputerHarness(computer_workspace)
        try:
            before = await observe_focused_text_field(
                computer, bundle_id="dev.astra.checkpoint-fixture",
                description="initial ordinary field")
            marker = "ASTRA_UNSENT_SUFFIX"
            result = await computer.call("computer_act", snapshot_id=before["snapshot_id"],
                interaction_mode="foreground_takeover", actions=[
                    {"type": "keypress", "key": "Tab"}, {"type": "type", "text": marker},
                ])
            after = json.loads(result["fresh_output"])
            receipt = after["action_result"]
            assert receipt["last_acknowledged_action"] == 0
            assert receipt["observation_required"] is True
            assert receipt["next_action_index"] == 1
            assert len(receipt["outcomes"]) == 1
            assert receipt["effect_verification"] == "unverified"
            assert marker not in json.dumps(after["ax_tree"])
            destination = first_node(after["ax_tree"], lambda n: n.get("role") == "AXTextField"
                and n.get("focused") is True, "new ordinary field")
            assert "Checkpoint target" in json.dumps(destination)
            # New input needs a new snapshot-bound action; the suffix is never replayed.
            verified = await computer.call("computer_act", snapshot_id=after["snapshot_id"],
                interaction_mode="foreground_takeover", actions=[{"type": "type", "text": "ASTRA_FRESH_INPUT"}])
            final = json.loads(verified["fresh_output"])
            assert "ASTRA_FRESH_INPUT" in json.dumps(final["ax_tree"])
            assert marker not in json.dumps(final["ax_tree"])
            (computer_workspace / "checkpoint-evidence.json").write_text(json.dumps({
                "helper_status": await computer.status(), "checkpoint_receipt": receipt,
                "destination_observed": True, "unsent_suffix_absent": True,
                "fresh_input_observed": True,
            }, indent=2), encoding="utf-8")
        finally:
            await computer.close()

    try:
        run(workflow())
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.real_macos_computer
def test_edge_local_page_checkpoint_preserves_unexecuted_text(computer_workspace):
    title = f"Astra CU checkpoint {uuid.uuid4().hex[:12]}"
    page = computer_workspace / "checkpoint.html"
    page.write_text(f"""<!doctype html><html><head><title>{title}</title></head>
      <body><h1>{title}</h1><label>Source <input id="source" autofocus></label>
      <label>Checkpoint target <input id="destination"></label></body></html>""", encoding="utf-8")
    subprocess.run(["open", "-na", "Microsoft Edge", "--args", "--new-window", page.as_uri()],
                   check=True, capture_output=True, timeout=15)

    async def workflow():
        computer = ComputerHarness(computer_workspace)
        target_observed = False
        closed = False
        try:
            before = await observe_focused_text_field(
                computer, bundle_id="com.microsoft.edgemac", title=title,
                description="focused browser source input")
            first_node(before["ax_tree"], lambda n: n.get("role") == "AXTextField"
                and n.get("focused") is True and "Source" in json.dumps(n), "focused local source field")
            target_observed = True
            result = await computer.call("computer_act", snapshot_id=before["snapshot_id"],
                interaction_mode="foreground_takeover", actions=[
                    {"type": "keypress", "key": "Tab"}, {"type": "type", "text": "ASTRA_UNSENT_SUFFIX"},
                ])
            after = json.loads(result["fresh_output"])
            receipt = after["action_result"]
            assert receipt["last_acknowledged_action"] == 0
            assert receipt["observation_required"] is True
            assert len(receipt["outcomes"]) == 1
            assert "ASTRA_UNSENT_SUFFIX" not in json.dumps(after["ax_tree"])
            destination = first_node(after["ax_tree"], lambda n: n.get("role") == "AXTextField"
                and n.get("focused") is True, "focused browser destination input")
            assert "Checkpoint target" in json.dumps(destination)
            typed = await computer.call("computer_act", snapshot_id=after["snapshot_id"],
                interaction_mode="foreground_takeover", actions=[{"type": "type", "text": "ASTRA_BROWSER_FRESH"}])
            final = json.loads(typed["fresh_output"])
            assert "ASTRA_BROWSER_FRESH" in json.dumps(final["ax_tree"])
            assert "ASTRA_UNSENT_SUFFIX" not in json.dumps(final["ax_tree"])
            (computer_workspace / "checkpoint-evidence.json").write_text(json.dumps({
                "helper_status": await computer.status(), "checkpoint_receipt": receipt,
                "destination_observed": True, "unsent_suffix_absent": True, "fresh_input_observed": True,
            }, indent=2), encoding="utf-8")
            await computer.call_raw("computer_act", snapshot_id=final["snapshot_id"],
                interaction_mode="foreground_takeover", actions=[{"type": "keypress", "key": "w", "modifiers": ["command"]}])
            closed = True
            assert not any(title in str(w.get("title", "")) for a in await computer.apps()
                           for w in a.get("windows", []))
        finally:
            if target_observed and not closed:
                try:
                    fresh = await computer.snapshot()
                    await computer.call_raw("computer_act", snapshot_id=fresh["snapshot_id"],
                        interaction_mode="foreground_takeover",
                        actions=[{"type": "keypress", "key": "w", "modifiers": ["command"]}])
                except (AssertionError, RuntimeError):
                    pass
            await computer.close()

    run(workflow())
