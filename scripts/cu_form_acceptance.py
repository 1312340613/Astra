"""Run native tools against the explicitly opened local CU form fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
from urllib.request import build_opener, ProxyHandler

from agent.runtime.computer_backend import ComputerSessionManager
from agent.runtime.macos_computer import HelperTransport, MacComputerBackend
from agent.runtime.tools.computer import register_computer_tools
from agent.runtime.tools.registry import ToolRegistry


def nodes(tree, depth=0):
    if isinstance(tree, dict):
        yield tree, depth
        for child in tree.get("children", []):
            yield from nodes(child, depth + 1)


async def run_native(args):
    report = {"channel": "native", "runs": [], "calls": [], "passed": False}
    transport = HelperTransport()
    backend = MacComputerBackend(transport)
    temporary = tempfile.TemporaryDirectory(prefix="astra-cu-form-")
    manager = ComputerSessionManager(backend, cache_root=Path(temporary.name) / "cache")
    registry = ToolRegistry()
    # This runner is restricted below to the named, user-authorized local fixture.
    registry.yolo = True
    register_computer_tools(registry, manager)

    async def call(name, parameters):
        started = time.monotonic()
        result = await registry.execute(name, parameters)
        report["calls"].append(
            {"tool": name, "seconds": round(time.monotonic() - started, 3), "code": result.get("code"),
             **({"computer_receipt": result["computer_receipt"]} if "computer_receipt" in result else {})}
        )
        if result.get("error"):
            raise RuntimeError(f"{name}: {result.get('code')}: {result.get('error')}")
        if name == "computer_act" and any("checked" in action for action in parameters.get("actions", [])):
            receipt = result.get("computer_receipt", {})
            if receipt.get("next_step") != "continue_from_fresh_observation" or receipt.get("goal_verified_count") != 10:
                raise RuntimeError("The model-facing receipt must confirm all ten checked goals")
        return json.loads(result.get("fresh_output") or result["output"])

    def oracle():
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{args.port}/state", timeout=2) as response:
            return json.load(response)

    try:
        catalog = await call("computer_apps", {})
        targets = [
            (app["app_ref"], window["window_ref"])
            for app in catalog["apps"]
            if app.get("bundle_id") == "com.microsoft.edgemac"
            for window in app["windows"]
            if window.get("bindable") and "Astra CU Form Reliability" in window.get("title", "")
        ]
        if not targets:
            raise RuntimeError("Open the local Astra CU Form Reliability fixture in Edge")
        state = await call("computer_get_app_state", dict(zip(("app_ref", "window_ref"), targets[0])))
        for trial in range(args.runs):
            controls = state.get("form_controls", {}).get("elements", [])
            choices = [
                node
                for node in controls
                if node.get("role") == "AXRadioButton" and (node.get("label") or node.get("title")) == "True"
            ]
            if len(choices) != 10:
                raise RuntimeError(f"Native observation exposed {len(choices)} of 10 target choices")
            if trial or any(node.get("checked") for node in choices):
                reset = [
                    node
                    for node, _ in nodes(state["ax_tree"])
                    if node.get("role") == "AXButton" and (node.get("label") or node.get("title")) == "Reset fixture"
                ]
                if len(reset) != 1:
                    raise RuntimeError("Reset control must be unique in the native observation")
                state = await call(
                    "computer_act",
                    {
                        "snapshot_id": state["snapshot_id"],
                        "actions": [{"type": "click", "element_ref": reset[0]["element_ref"]}],
                    },
                )
                choices = [
                    node
                    for node in state["form_controls"]["elements"]
                    if node.get("role") == "AXRadioButton" and (node.get("label") or node.get("title")) == "True"
                ]
            started = time.monotonic()
            state = await call(
                "computer_act",
                {
                    "snapshot_id": state["snapshot_id"],
                    "actions": [{"type": "click", "element_index": node["index"], "checked": True} for node in choices],
                },
            )
            elapsed = time.monotonic() - started
            await asyncio.sleep(0.05)
            actual = oracle()  # Independent test oracle; never used to locate native targets.
            passed = (
                set(actual.get("selected", [])) == {f"q{i}true" for i in range(1, 11)}
                and actual.get("selectedClicks") == 10
                and actual.get("submits") == 0
            )
            passed = passed and state.get("choice_verification", {}).get("status") == "verified"
            report["runs"].append({"trial": trial + 1, "seconds": round(elapsed, 3), "passed": passed, **actual})
            print(json.dumps(report["runs"][-1]), flush=True)
            if not passed:
                raise RuntimeError("The native state verification and independent oracle must both pass")
        # An already satisfied batch must produce no additional click events.
        choices = [
            node
            for node in state["form_controls"]["elements"]
            if node.get("role") == "AXRadioButton" and (node.get("label") or node.get("title")) == "True"
        ]
        await call(
            "computer_act",
            {
                "snapshot_id": state["snapshot_id"],
                "actions": [{"type": "click", "element_index": node["index"], "checked": True} for node in choices],
            },
        )
        report["idempotent_click_count"] = oracle().get("selectedClicks")
        report["passed"] = report["idempotent_click_count"] == 10
    except Exception as error:
        report["error"] = str(error)
        print(report["error"], flush=True)
        print(transport.stderr, flush=True)
    finally:
        await manager.close()
        temporary.cleanup()
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run_native(args)) else 1)


if __name__ == "__main__":
    main()
