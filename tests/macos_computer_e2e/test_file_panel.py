"""Real file-panel acceptance on a private local page, without uploading a file."""

import asyncio
import json
import subprocess
import uuid

import pytest

from .fixtures import ComputerHarness, first_node, run


@pytest.mark.real_macos_computer
@pytest.mark.parametrize("selection_mode", ["element", "image"])
def test_edge_local_file_panel_selects_fixture(computer_workspace, selection_mode):
    title = f"Astra file panel {uuid.uuid4().hex[:12]}"
    source = computer_workspace / "finder-source.txt"
    page = computer_workspace / "file-panel.html"
    page.write_text(f"""<!doctype html><html lang="zh-CN"><head><title>{title}</title>
      <meta name="google" content="notranslate"></head><body><h1>{title}</h1>
      <button onclick="document.getElementById('file').click()">选择测试文件</button>
      <input id="file" type="file" hidden onchange="document.getElementById('result').textContent =
        'Selected: ' + this.files[0].name"><p id="result">尚未选择</p></body></html>""", encoding="utf-8")
    subprocess.run(["open", "-na", "Microsoft Edge", "--args", "--new-window", page.as_uri()],
                   check=True, capture_output=True, timeout=15)

    async def workflow():
        computer = ComputerHarness(computer_workspace)
        evidence = {}

        async def observe(app_ref, window_ref):
            result = await computer.call("computer_get_app_state", app_ref=app_ref, window_ref=window_ref)
            return json.loads(result["fresh_output"])

        async def current(identity):
            apps = await computer.apps()
            targets = [(a, w) for a in apps if a.get("bundle_id") == "com.microsoft.edgemac"
                       for w in a.get("windows", []) if w.get("window_identity_ref") == identity]
            assert len(targets) == 1
            app, window = targets[0]
            return await observe(app["app_ref"], window["window_ref"])

        async def act(state, key, actions, coordinate_space="window_logical"):
            result = await computer.call_raw("computer_act", snapshot_id=state["snapshot_id"],
                interaction_mode="foreground_takeover", actions=actions, coordinate_space=coordinate_space)
            evidence[key] = result
            assert not result.get("error") or result.get("code") == "unknown_outcome", result
            return result

        async def new_window(previous):
            for _ in range(2):
                apps = await computer.apps()
                candidates = [(app, w) for app in apps if app.get("bundle_id") == "com.microsoft.edgemac"
                    for w in app.get("windows", []) if w.get("window_identity_ref")
                    and w["window_identity_ref"] not in previous and w.get("bindable") is True]
                if len(candidates) == 1:
                    app, window = candidates[0]
                    return window, await observe(app["app_ref"], window["window_ref"])
                await asyncio.sleep(0.3)
            raise AssertionError("no uniquely bindable new file panel")

        try:
            app_ref, window_ref, parent = await computer.wait_for_window(
                bundle_id="com.microsoft.edgemac", title=title)
            apps = await computer.apps()
            prior = {w.get("window_identity_ref") for app in apps if app.get("bundle_id") == "com.microsoft.edgemac"
                     for w in app.get("windows", [])}
            targets = [(a, w) for a in apps if a.get("bundle_id") == "com.microsoft.edgemac"
                       for w in a.get("windows", []) if title in str(w.get("title"))]
            assert len(targets) == 1
            app, parent = targets[0]
            before = await observe(app["app_ref"], parent["window_ref"])
            button = first_node(before["ax_tree"], lambda n: n.get("role") == "AXButton"
                                and "选择测试文件" in str(n.get("title") or n.get("label")), "local file button")
            await act(before, "open_receipt", [{"type": "click", "element_ref": button["element_ref"]}])
            # Never replay a modal-opening action whose acknowledgement was lost.
            panel, state = await new_window(prior)
            evidence["panel_catalog"] = panel
            evidence["panel_state"] = state
            assert state["ax_tree"]["role"] == "AXSheet"
            assert panel["window_identity_ref"] != parent["window_identity_ref"]
            await act(state, "goto_receipt", [{"type": "keypress", "key": "g", "modifiers": ["command", "shift"]}])
            nested, state = await new_window(prior | {panel["window_identity_ref"]})
            evidence["nested_catalog"] = nested
            evidence["nested_state"] = state
            assert state["ax_tree"]["role"] == "AXSheet"
            field = first_node(state["ax_tree"], lambda n: n.get("role") == "AXTextField"
                               and n.get("focused") is True, "Go to Folder field")
            await act(state, "type_receipt", [
                {"type": "keypress", "key": "a", "modifiers": ["command"], "element_ref": field["element_ref"]},
                {"type": "type", "text": str(source), "element_ref": field["element_ref"]},
            ])
            state = await current(nested["window_identity_ref"])
            field = first_node(state["ax_tree"], lambda n: n.get("role") == "AXTextField"
                               and n.get("focused") is True, "path after actual input")
            assert field.get("value") == str(source), "acknowledgement alone is not text delivery"
            evidence["path_verified"] = True
            await act(state, "confirm_path_receipt", [{"type": "keypress", "key": "return"}])
            state = await current(panel["window_identity_ref"])
            evidence["selected_panel_state"] = state
            assert source.name in json.dumps(state["ax_tree"])
            opened = first_node(state["ax_tree"], lambda n: n.get("role") == "AXButton"
                and (n.get("title") or n.get("label")) in {"Open", "打开"}
                and n.get("enabled") is True, "enabled file Open button")
            click = {"type": "click", "element_ref": opened["element_ref"]}
            if selection_mode == "image":
                bounds = opened["bounds"]
                pixels, logical = state["published_image_size"], state["logical_size"]
                click = {"type": "click", "target_element_ref": opened["element_ref"],
                         "x": (bounds["x"] + bounds["width"] / 2) * pixels["width"] / logical["width"],
                         "y": (bounds["y"] + bounds["height"] / 2) * pixels["height"] / logical["height"]}
            await act(state, "select_file_receipt", [click],
                      coordinate_space="image_pixels" if selection_mode == "image" else "window_logical")
            final = await current(parent["window_identity_ref"])
            assert "Selected: " + source.name in json.dumps(final["ax_tree"])
            evidence["selection_verified"] = True
            evidence["helper_status"] = await computer.status()
            await act(final, "close_fixture_receipt", [{"type": "keypress", "key": "w", "modifiers": ["command"]}])
            assert not any(title in str(w.get("title", "")) for a in await computer.apps()
                           for w in a.get("windows", []))
        finally:
            (computer_workspace / "file-panel-evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            await computer.close()

    run(workflow())
