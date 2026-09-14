"""Exercise registered browser tools in an isolated real browser against the local fixture."""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from urllib.request import build_opener, ProxyHandler
from agent.runtime.cdp_backend import CdpBrowserBackend
from agent.runtime.browser_session import BrowserSessionManager
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.browser import register_browser_tools


async def main(args):
    report = {"channel": "browser_cdp", "scope": args.scope, "runs": [], "calls": [], "passed": False}
    backend = CdpBrowserBackend()
    with tempfile.TemporaryDirectory(prefix="astra-browser-cu-") as temp:
        manager = BrowserSessionManager(path=Path(temp) / "browser.db")
        reg = ToolRegistry()
        reg.yolo = True
        register_browser_tools(reg, manager=manager, backend=backend)

        async def call(name, args):
            start = time.monotonic()
            result = await reg.execute(name, args)
            report["calls"].append(
                {"tool": name, "seconds": round(time.monotonic() - start, 3), "code": result.get("code")}
            )
            if result.get("error"):
                raise RuntimeError(str(result))
            output = result.get("output", "")
            report["calls"][-1].update(output_chars=len(output), output_truncated=bool(result.get("output_truncated")))
            if name == "browser_snapshot" and args.get("scope") == "form" and result.get("output_truncated"):
                raise RuntimeError("Compact form observation exceeded the model preview budget")
            print(name, output[:150], flush=True)
            try:
                return json.loads(output)
            except ValueError:
                try:
                    return json.loads(output[output.index("{") :])
                except (ValueError, TypeError):
                    return output

        def oracle():
            with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{args.port}/state", timeout=2) as response:
                return json.load(response)

        async def wait_for_oracle(run):
            # The fixture publishes click events asynchronously. Observe that
            # independent channel without issuing another browser input.
            started = time.monotonic()
            first = None
            polls = 0
            while True:
                actual = await asyncio.to_thread(oracle)
                polls += 1
                if first is None:
                    first = actual
                matched = (
                    actual.get("run") == run
                    and set(actual.get("selected", [])) == {f"q{i}true" for i in range(1, 11)}
                    and actual.get("selectedClicks") == 10
                    and actual.get("submits") == 0
                )
                invalid = actual.get("run") == run and (
                    actual.get("selectedClicks", 0) > 10 or actual.get("submits", 0) != 0
                )
                if matched or invalid or time.monotonic() - started >= 1:
                    return actual, {"matched": matched, "polls": polls, "first": first,
                        "seconds": round(time.monotonic() - started, 3)}
                await asyncio.sleep(0.02)

        try:
            await call("browser_open", {"url": f"http://127.0.0.1:{args.port}/form", "extract": False})
            snapshot_args = {"scope": "form"} if args.scope == "form" else {"role_filter": "radio"}
            state = await call("browser_snapshot", {**snapshot_args, "refresh": True, "include_text": False})
            if args.scope == "form" and len(state.get("groups", [])) != 10:
                raise RuntimeError("Compact observation must retain all ten question groups")
            # Keep the observed refs local to this run.
            for trial in range(3):
                if trial:
                    await call("browser_click", {"selector": "#reset"})
                    state = await call("browser_snapshot", {**snapshot_args, "include_text": False})
                # Refs are obtained only from the tool observation; the local oracle is independent.
                elements = state.get("elements", [])
                choices = [x for x in elements if x.get("name") == "True"]
                if len(choices) != 10:
                    raise RuntimeError("Expected 10 uniquely identified True controls; got " + str(len(choices)))
                start = time.monotonic()
                result = await call(
                    "browser_check", {"checks": [{"selector": "ref:" + x["ref"], "checked": True} for x in choices]}
                )
                actual, observation = await wait_for_oracle(trial)
                passed = result.get("verified") is True and observation["matched"]
                report["runs"].append({"seconds": round(time.monotonic() - start, 3), "passed": passed,
                    "receipt": {key: result.get(key) for key in ("status", "verified", "completed", "clickCount")},
                    "oracle_wait": observation, **actual})
                print(report["runs"][-1], flush=True)
                if not passed:
                    raise RuntimeError("Browser checked verification or independent event oracle failed")
                state = result["after"]
            result = await call(
                "browser_check", {"checks": [{"selector": f"#q{i}true", "checked": True} for i in range(1, 11)]}
            )
            report["idempotent_click_count"] = oracle()["selectedClicks"]
            report["passed"] = report["idempotent_click_count"] == 10 and result.get("verified") is True
        except Exception as error:
            report["error"] = str(error)
            print("ERROR", report["error"], flush=True)
        finally:
            await backend.close_connection()
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--scope", choices=("form", "radio"), default="form")
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
