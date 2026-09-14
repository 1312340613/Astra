"""Real-provider smoke for the Codex/Claude-style coding tool surface.

The fixture is an existing ~10KB HTML file. A passing run proves that the
selected model can inspect and modify it through semantic file edits without
seeing the legacy transaction/chunk protocol or overwriting the whole file.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.cli.environment import load_project_env
from agent.cli.model_catalog import discover_model_catalog
from agent.cli.model_preferences import read_selected_model
from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.registry import ToolRegistry


LEGACY_TRANSACTION_TOOLS = {
    "begin_file_write",
    "write_file_chunk",
    "commit_file_write",
    "abort_file_write",
}


def fixture_html() -> str:
    filler = "\n".join(
        f"  <!-- stable fixture line {index:03d}: semantic editing smoke -->"
        for index in range(150)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tool Surface Before</title>
  <style>
    body {{ background: #111111; color: #eeeeee; }}
    .status {{ opacity: 0.65; }}
  </style>
</head>
<body>
  <h1 id="heading">Before semantic edit</h1>
  <div class="status">idle</div>
{filler}
  <script>
    function statusMessage() {{
      return "before";
    }}
  </script>
</body>
</html>
"""


async def resolve_entry(model_key: str, discovery_timeout: float):
    catalog = await discover_model_catalog(timeout=discovery_timeout)
    selected = model_key or read_selected_model()
    entry = catalog.resolve_persisted(selected)
    if entry is None:
        available = ", ".join(item.key for item in catalog.entries[:20])
        raise RuntimeError(
            f"Could not resolve model {selected!r}. Available: {available or '(none)'}; "
            f"discovery errors: {catalog.errors}"
        )
    return entry


async def run_smoke(args: argparse.Namespace) -> dict:
    load_project_env(PROJECT_ROOT)
    entry = await resolve_entry(args.model_key, args.discovery_timeout)
    profile = entry.profile
    run_id = uuid.uuid4().hex[:12]
    workdir = Path(
        args.workdir or PROJECT_ROOT / ".astra" / "coding-tools-smoke"
    ).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / f"existing-{run_id}.html"
    session_path = workdir / f"existing-{run_id}.jsonl"
    report_path = workdir / f"existing-{run_id}.report.json"
    target.write_text(fixture_html(), encoding="utf-8", newline="\n")
    original_size = target.stat().st_size

    tools = ToolRegistry()
    register_file_tools(tools, workdir=str(workdir))
    exposed = {
        item["function"]["name"]
        for item in tools.to_openai_tools(groups={"files"})
    }
    config = LLMConfig(
        provider=profile.provider,
        model=entry.model_id,
        api_key=profile.api_key() or os.getenv("LLM_API_KEY", "") or "local-no-key",
        base_url=entry.base_url,
        capabilities=profile.capabilities,
        temperature=profile.temperature,
        max_tokens=min(4096, profile.max_tokens),
        top_p=profile.top_p,
        top_k=profile.top_k,
        min_p=profile.min_p,
        presence_penalty=profile.presence_penalty,
        repetition_penalty=profile.repetition_penalty,
        repetition_penalty_parameter=profile.repetition_penalty_parameter,
        connect_timeout=30,
        idle_timeout=args.idle_timeout,
        overall_timeout=None,
    )
    agent = ReActAgent(
        "coding-tools-smoke",
        LLMClient(config),
        tools,
        system_prompt=(
            "You are a coding agent editing an existing workspace file. Read before editing. "
            "Use edit_file for exact replacements or apply_patch for multiple hunks. write_file "
            "is only for new files. Never resend an existing whole file. Make the requested "
            "changes, then verify the final SHA-256 with stat_file and verify the changed content "
            "with a fresh read_file call before reporting completion briefly."
        ),
        max_iterations=args.max_iterations,
        progressive_tools=False,
    )
    agent.tool_allowlist = {
        "read_file",
        "stat_file",
        "write_file",
        "edit_file",
        "apply_patch",
        "search_files",
    }
    agent.context.max_prompt_tokens = max(4096, profile.context_limit - 4096)
    agent.context.set_session(str(session_path))

    prompt = (
        f"Modify the existing file {target.name}. Make exactly these semantic changes:\n"
        "1. Change the title from 'Tool Surface Before' to 'Tool Surface After'.\n"
        "2. Change the body background from #111111 to #16213e.\n"
        "3. Change the h1 text from 'Before semantic edit' to 'After semantic edit'.\n"
        "4. Change statusMessage() to return \"after\" instead of \"before\".\n"
        "Do not rewrite the whole file. Preserve all stable fixture comments. After editing, "
        "call stat_file and then read_file to verify the final disk state."
    )

    events: list[dict] = []
    error = ""
    started = time.perf_counter()

    async def consume() -> None:
        async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text(prompt)])
        ):
            events.append(event)

    try:
        await asyncio.wait_for(consume(), timeout=args.timeout)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    final = target.read_text(encoding="utf-8") if target.is_file() else ""
    final_sha256 = (
        hashlib.sha256(target.read_bytes()).hexdigest()
        if target.is_file()
        else ""
    )
    result_events = [
        event for event in events if event.get("type") == "tool_result"
    ]
    calls = [
        str(event.get("name") or "")
        for event in result_events
    ]
    tool_errors = [
        {
            "name": event.get("name") or event.get("tool_name") or "",
            "code": event.get("code") or event.get("error_type") or "",
            "error": event.get("error") or event.get("message") or "",
        }
        for event in events
        if (
            event.get("type") == "tool_result"
            and event.get("error")
        )
        or event.get("type") == "error"
    ]
    mutation_names = {"write_file", "edit_file", "apply_patch"}
    last_mutation = max(
        (
            index
            for index, event in enumerate(result_events)
            if event.get("name") in mutation_names and not event.get("error")
        ),
        default=-1,
    )
    verification_events = result_events[last_mutation + 1:]
    fresh_reads = [
        event
        for event in verification_events
        if event.get("name") == "read_file"
        and not event.get("error")
        and not event.get("cached")
    ]
    stat_matches = False
    for event in verification_events:
        if event.get("name") != "stat_file" or event.get("error"):
            continue
        try:
            stat_matches = (
                json.loads(str(event.get("output") or "{}")).get("sha256")
                == final_sha256
            )
        except json.JSONDecodeError:
            stat_matches = False
        if stat_matches:
            break
    checks = {
        "title": "<title>Tool Surface After</title>" in final,
        "background": "background: #16213e" in final,
        "heading": '>After semantic edit</h1>' in final,
        "function": 'return "after";' in final,
        "comments_preserved": final.count("stable fixture line") == 150,
        "legacy_tools_hidden": not (LEGACY_TRANSACTION_TOOLS & exposed),
        "stat_file_exposed": "stat_file" in exposed,
        "no_legacy_calls": not (LEGACY_TRANSACTION_TOOLS & set(calls)),
        "no_whole_write": "write_file" not in calls,
        "no_tool_errors": not tool_errors,
        "post_write_stat_matches_disk": stat_matches,
        "post_write_read_is_fresh": bool(fresh_reads),
    }
    passed = not error and all(checks.values())
    report = {
        "passed": passed,
        "run_id": run_id,
        "model_key": entry.key,
        "model_id": entry.model_id,
        "base_url": entry.base_url,
        "target": str(target),
        "original_bytes": original_size,
        "final_bytes": target.stat().st_size if target.is_file() else 0,
        "final_sha256": final_sha256,
        "exposed_file_tools": sorted(exposed),
        "tool_calls": calls,
        "tool_results": [
            {
                "name": event.get("name") or "",
                "cached": bool(event.get("cached")),
                "code": event.get("code") or "",
                "error": event.get("error") or "",
            }
            for event in result_events
        ],
        "tool_errors": tool_errors,
        "checks": checks,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
        "session_path": str(session_path),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report["report_path"] = str(report_path)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-key", default="")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--idle-timeout", type=float, default=180)
    parser.add_argument("--discovery-timeout", type=float, default=8)
    parser.add_argument("--max-iterations", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    report = asyncio.run(run_smoke(parse_args()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
