"""Real-provider Phase T smoke: build a deterministic 100KB+ HTML at 4K output.

This intentionally exposes only transactional file tools. A passing run proves
that the selected provider can finish a payload larger than one model response,
that the final SHA-256 matches, and that exactly one transaction committed the
formal target.
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


UNIT = "<p>Phase-T 4K transactional smoke payload 0123456789 abcdefghijklmnopqrstuvwxyz.</p>\n"
REPETITIONS = 1280
PREFIX = "<!doctype html>\n<html><body>\n"
SUFFIX = "</body></html>\n"


def expected_payload(repetitions: int = REPETITIONS) -> str:
    return PREFIX + UNIT * repetitions + SUFFIX


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


def committed_count(audit_path: Path, target: Path) -> int:
    if not audit_path.is_file():
        return 0
    target_key = os.path.normcase(str(target.resolve()))
    count = 0
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            event.get("event") == "committed"
            and os.path.normcase(str(event.get("path") or "")) == target_key
        ):
            count += 1
    return count


async def run_smoke(args: argparse.Namespace) -> dict:
    load_project_env(PROJECT_ROOT)
    entry = await resolve_entry(args.model_key, args.discovery_timeout)
    profile = entry.profile
    repetitions = max(1, int(args.repetitions))
    payload = expected_payload(repetitions)
    expected_bytes = payload.encode("utf-8")
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()

    run_id = uuid.uuid4().hex[:12]
    workdir = Path(args.workdir or PROJECT_ROOT / ".astra" / "phase-t-smoke").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / f"phase-t-4k-{run_id}.html"
    session_path = workdir / f"phase-t-4k-{run_id}.jsonl"
    report_path = workdir / f"phase-t-4k-{run_id}.report.json"

    tools = ToolRegistry()
    register_file_tools(tools, workdir=str(workdir))
    config = LLMConfig(
        provider=profile.provider,
        model=entry.model_id,
        api_key=profile.api_key() or os.getenv("LLM_API_KEY", "") or "local-no-key",
        base_url=entry.base_url,
        capabilities=profile.capabilities,
        temperature=profile.temperature,
        max_tokens=4096,
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
        "phase-t-smoke",
        LLMClient(config),
        tools,
        system_prompt=(
            "You are a deterministic release-gate worker. Follow the exact file specification. "
            "Use only the exposed transactional file tools. Never claim success until commit_file_write "
            "returns success. Do not abbreviate, summarize, or replace repeated literal content."
        ),
        max_iterations=args.max_iterations,
        progressive_tools=False,
    )
    agent.tool_allowlist = {
        "begin_file_write",
        "write_file_chunk",
        "commit_file_write",
        "abort_file_write",
        "read_file",
    }
    agent.context.max_prompt_tokens = max(4096, profile.context_limit - 4096)
    agent.context.set_session(str(session_path))

    prompt = (
        f"Create {target.name} in the current workspace using begin_file_write, ordered "
        "write_file_chunk calls, and commit_file_write only.\n"
        "The exact UTF-8 content is:\n"
        f"1. Prefix exactly {PREFIX!r}\n"
        f"2. Then repeat this exact unit exactly {repetitions} times: {UNIT!r}\n"
        f"3. Suffix exactly {SUFFIX!r}\n"
        f"Expected byte size: {len(expected_bytes)}\n"
        f"Expected SHA-256: {expected_hash}\n"
        "Each chunk must contain at most 6000 characters. Use explicit sequential sequence numbers. "
        "The target does not exist; overwrite=false. Do not use write_file, shell, Python, encoded "
        "content, placeholders, ellipses, or a different construction."
    )

    started = time.perf_counter()
    error = ""
    try:
        reply = await asyncio.wait_for(
            agent.reply(Msg(content=[ContentBlock.text(prompt)])),
            timeout=args.timeout,
        )
        reply_text = reply.get_text() if reply is not None else ""
    except Exception as exc:
        reply_text = ""
        error = f"{type(exc).__name__}: {exc}"

    actual_bytes = target.read_bytes() if target.is_file() else b""
    actual_hash = hashlib.sha256(actual_bytes).hexdigest() if actual_bytes else ""
    audit_path = workdir / ".astra" / "file-transactions" / "audit.jsonl"
    commits = committed_count(audit_path, target)
    passed = (
        not error
        and actual_bytes == expected_bytes
        and actual_hash == expected_hash
        and commits == 1
    )
    report = {
        "passed": passed,
        "run_id": run_id,
        "model_key": entry.key,
        "model_id": entry.model_id,
        "base_url": entry.base_url,
        "max_tokens": 4096,
        "repetitions": repetitions,
        "target": str(target),
        "expected_bytes": len(expected_bytes),
        "actual_bytes": len(actual_bytes),
        "expected_sha256": expected_hash,
        "actual_sha256": actual_hash,
        "formal_commit_count": commits,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
        "reply": reply_text[-2000:],
        "session_path": str(session_path),
        "audit_path": str(audit_path),
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
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--idle-timeout", type=float, default=180)
    parser.add_argument("--discovery-timeout", type=float, default=8)
    parser.add_argument("--max-iterations", type=int, default=140)
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    return parser.parse_args()


def main() -> int:
    report = asyncio.run(run_smoke(parse_args()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
