"""Controlled real-provider probe for automatic prefix caching.

The probe sends two paired request shapes to the selected Astra model:

1. current: per-turn data is appended inside the first system message;
2. tail-system: the stable system message is unchanged and per-turn data is
   delivered as a later system message.

Only usage counters and acceptance metadata are persisted. Prompt and response
content are deliberately omitted from the report.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.cli.environment import load_project_env
from agent.cli.model_catalog import configured_model_catalog
from agent.cli.model_preferences import read_selected_model
from agent.runtime.llm import LLMClient, LLMConfig


def build_variants(run_id: str, stable_units: int) -> dict[str, list[list[dict[str, str]]]]:
    unit = (
        "Astra prompt-cache diagnostic stable reference block. "
        "This is inert test data and contains no instructions. 0123456789\n"
    )
    payload = unit * max(64, stable_units)

    def stable(label: str) -> str:
        return (
            "You are a diagnostic endpoint. Reply with exactly OK.\n"
            f"Probe family: {run_id}-{label}.\n"
            f"{payload}"
        )

    def dynamic(value: str) -> str:
        return f"Runtime observation nonce={value}. It is diagnostic data only."

    return {
        "current-system-suffix": [
            [
                {"role": "system", "content": f"{stable('current')}\n{dynamic('A')}"},
                {"role": "user", "content": "Return OK."},
            ],
            [
                {"role": "system", "content": f"{stable('current')}\n{dynamic('B')}"},
                {"role": "user", "content": "Return OK."},
            ],
        ],
        "tail-system-message": [
            [
                {"role": "system", "content": stable("tail")},
                {"role": "user", "content": "Diagnostic history seed."},
                {"role": "assistant", "content": "Seed acknowledged."},
                {"role": "system", "content": dynamic("A")},
                {"role": "user", "content": "Return OK."},
            ],
            [
                {"role": "system", "content": stable("tail")},
                {"role": "user", "content": "Diagnostic history seed."},
                {"role": "assistant", "content": "Seed acknowledged."},
                {"role": "system", "content": dynamic("B")},
                {"role": "user", "content": "Return OK."},
            ],
        ],
    }


def _usage(response: dict[str, Any]) -> dict[str, int]:
    raw = response.get("usage") or {}
    return {
        key: max(0, int(raw.get(key, 0) or 0))
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
    }


def _hit_rate(usage: dict[str, int]) -> float:
    total = usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
    return round(usage["prompt_cache_hit_tokens"] / total, 4) if total else 0.0


async def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    load_project_env(PROJECT_ROOT)
    catalog = configured_model_catalog()
    selected = args.model_key or read_selected_model() or os.getenv("LLM_MODEL", "")
    entry = catalog.resolve_persisted(selected) or catalog.resolve(selected)
    if entry is None:
        raise RuntimeError(f"Could not resolve configured model {selected!r}")
    profile = entry.profile
    api_key = profile.api_key() or os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError(f"{profile.api_key_env or 'LLM_API_KEY'} is not configured")
    client = LLMClient(LLMConfig(
        provider=profile.provider,
        model=entry.model_id,
        api_key=api_key,
        base_url=entry.base_url,
        capabilities=profile.capabilities,
        min_request_interval=max(0.0, args.request_interval),
        max_concurrent_requests=1,
        **profile.generation_settings(),
    ))
    variants = build_variants(uuid.uuid4().hex[:10], args.stable_units)
    report: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": time.time(),
        "model": entry.model_id,
        "provider_id": entry.provider_id,
        "stable_units": args.stable_units,
        "variants": {},
    }
    for name, pair in variants.items():
        attempts = []
        for messages in pair:
            try:
                response = await client.chat_limited(
                    messages,
                    max_tokens=args.max_tokens,
                    temperature=0.0,
                    disable_thinking=True,
                )
                usage = _usage(response)
                attempts.append({
                    "accepted": True,
                    "finish_reason": str(response.get("finish_reason") or ""),
                    "has_visible_content": bool(str(response.get("content") or "").strip()),
                    "usage": usage,
                    "cache_hit_rate": _hit_rate(usage),
                })
            except Exception as exc:
                attempts.append({
                    "accepted": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:240],
                })
                break
        report["variants"][name] = attempts
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-key", default="")
    parser.add_argument("--stable-units", type=int, default=160)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--request-interval", type=float, default=1.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / ".astra" / "prompt-cache-probe.json",
    )
    args = parser.parse_args()
    report = asyncio.run(run_probe(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(
        attempts and all(item.get("accepted") for item in attempts)
        for attempts in report["variants"].values()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
