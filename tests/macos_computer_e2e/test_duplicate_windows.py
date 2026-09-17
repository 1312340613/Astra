"""Same-title, same-geometry Edge windows retain distinct native identities."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
import uuid

import pytest

from .fixtures import ComputerHarness, first_node, run


@pytest.mark.real_macos_computer
def test_edge_identical_window_titles_bind_and_act_on_exact_window(computer_workspace):
    title = f"Astra duplicate windows {uuid.uuid4().hex[:12]}"

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            marker = "SECOND" if self.path == "/second" else "FIRST"
            body = (f'<!doctype html><html lang="zh-CN"><head><title>{title}</title>'
                    '<meta name="google" content="notranslate"></head><body>'
                    f'<h1>{marker}</h1><label><input type="checkbox">{marker} check</label>'
                    '</body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    # Fixture setup/cleanup touches only the two uniquely named test windows.
    # Binding, observation, focus takeover and input below use production tools.
    setup = computer_workspace / "duplicate-window-setup.swift"
    setup.write_text('''import AppKit
import ApplicationServices
let title = CommandLine.arguments[1]
let cleanup = CommandLine.arguments[2] == "cleanup"
func attr(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}
for app in NSWorkspace.shared.runningApplications where app.bundleIdentifier == "com.microsoft.edgemac" {
    let root = AXUIElementCreateApplication(app.processIdentifier)
    let windows = (attr(root, kAXWindowsAttribute) as? [AXUIElement] ?? []).filter {
        let name = attr($0, kAXTitleAttribute) as? String
        return name == title || name == title + " - Microsoft Edge"
    }
    if !cleanup && windows.count != 2 { fatalError("expected two fixture windows") }
    for window in windows {
        if cleanup {
            if let button = attr(window, kAXCloseButtonAttribute), CFGetTypeID(button) == AXUIElementGetTypeID() {
                let close = unsafeBitCast(button, to: AXUIElement.self)
                precondition(AXUIElementPerformAction(close, kAXPressAction as CFString) == .success)
            }
        } else {
            var point = CGPoint(x: 80, y: 80)
            var size = CGSize(width: 1100, height: 620)
            precondition(AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString,
                AXValueCreate(.cgPoint, &point)!) == .success)
            precondition(AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString,
                AXValueCreate(.cgSize, &size)!) == .success)
        }
    }
}
''')
    setup_binary = computer_workspace / "duplicate-window-setup"
    subprocess.run(["swiftc", str(setup), "-o", str(setup_binary)], check=True, capture_output=True, timeout=30)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = {}

    async def workflow():
        computer = ComputerHarness(computer_workspace)
        try:
            evidence["helper_status"] = await computer.status()
            for path in ("first", "second"):
                subprocess.run(["open", "-na", "Microsoft Edge", "--args", "--new-window",
                                f"http://127.0.0.1:{server.server_port}/{path}"],
                               check=True, capture_output=True, timeout=15)
                await asyncio.sleep(0.8)  # Fixture creation, not action replay.
            subprocess.run([str(setup_binary), title, "position"],
                           check=True, capture_output=True, timeout=10)
            await asyncio.sleep(0.3)
            apps = await computer.apps()
            windows = [(app, window) for app in apps if app.get("bundle_id") == "com.microsoft.edgemac"
                       for window in app.get("windows", []) if window.get("title") == title]
            evidence["catalog"] = [w for _, w in windows]
            assert len(windows) == 2
            assert windows[0][1]["bounds"] == windows[1][1]["bounds"]
            assert all(w["bindable"] for _, w in windows), evidence["catalog"]
            identities = [w["window_identity_ref"] for _, w in windows]
            assert len(set(identities)) == 2
            # The catalog is in visible stacking order. The covered fixture must
            # remain blocked, while the front fixture can be observed and used.
            front_app, front = windows[0]
            back_app, back = windows[1]
            covered = await computer.call_raw("computer_get_app_state", app_ref=back_app["app_ref"],
                                              window_ref=back["window_ref"])
            evidence["covered"] = covered
            assert covered.get("code") == "overlay_blocked", covered
            result = await computer.call("computer_get_app_state", app_ref=front_app["app_ref"],
                                         window_ref=front["window_ref"])
            state = json.loads(result["fresh_output"])
            checkbox = first_node(state["ax_tree"], lambda n: n.get("role") == "AXCheckBox"
                                  and "SECOND check" in str(n.get("label") or n.get("title")), "front checkbox")
            result = await computer.call("computer_act", snapshot_id=state["snapshot_id"],
                interaction_mode="foreground_takeover", actions=[{
                    "type": "click", "element_ref": checkbox["element_ref"], "checked": True}])
            evidence["action"] = result
            assert result["computer_receipt"]["verification_state"] == "verified", result
            # Closing only the observed front fixture exposes the second equal
            # title; it must keep its original identity and untouched checkbox.
            state = json.loads(result["fresh_output"])
            closed = await computer.call_raw("computer_act", snapshot_id=state["snapshot_id"],
                interaction_mode="foreground_takeover",
                actions=[{"type": "keypress", "key": "w", "modifiers": ["command"]}])
            evidence["close_front"] = closed
            assert not closed.get("error") or closed.get("code") == "unknown_outcome", closed
            apps = await computer.apps()
            remaining = [(app, w) for app in apps if app.get("bundle_id") == "com.microsoft.edgemac"
                         for w in app.get("windows", []) if w.get("title") == title]
            assert len(remaining) == 1
            app, window = remaining[0]
            assert window["window_identity_ref"] == back["window_identity_ref"]
            result = await computer.call("computer_get_app_state", app_ref=app["app_ref"],
                                         window_ref=window["window_ref"])
            state = json.loads(result["fresh_output"])
            untouched = first_node(state["ax_tree"], lambda n: n.get("role") == "AXCheckBox"
                                   and "FIRST check" in str(n.get("label") or n.get("title")), "back checkbox")
            assert untouched.get("checked") is False
            evidence["back_unchanged"] = True
        finally:
            (computer_workspace / "duplicate-window-evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2))
            await computer.close()

    try:
        run(workflow())
    finally:
        subprocess.run([str(setup_binary), title, "cleanup"], check=True, capture_output=True, timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
