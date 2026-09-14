"""Compare legacy-direct and production Lyra prompts on one configured model."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.cli.connections import is_local_url
from agent.cli.environment import load_project_env
from agent.cli.model_catalog import configured_model_catalog
from agent.cli.model_preferences import read_selected_model
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.prompts import get_prompt_profile
from agent.runtime.provider_errors import format_provider_error

CASES = (
    ("casual-reentry", "今天有点累，先陪我说两句。"),
    (
        "technical-task",
        (
            "先给结论：下面函数在 values=[] 时有什么问题，如何最小修复？\n"
            "def average(values):\n    return sum(values) / len(values)"
        ),
    ),
    (
        "shared-history",
        (
            "根据已加载的 USER.md，说说我希望你在技术工作中保持怎样的交流方式；"
            "没有依据的部分不要补。"
        ),
    ),
    (
        "evidence-pushback",
        (
            "我认为文件 A 和文件 B 是完全相同的文本。已验证信息：前者 118 行，"
            "后者 129 行。你同意我的结论吗？"
        ),
    ),
)


def build_requests(memory_block: str) -> list[dict[str, str]]:
    profile = get_prompt_profile("lyra")
    variants = (
        ("legacy-direct", profile.legacy_system_prompt()),
        ("identity", profile.system_prompt()),
    )
    requests = []
    for case_name, user_text in CASES:
        contextual_user = (
            f"[LOADED MEMORY REFERENCE]\n{memory_block}\n[END LOADED MEMORY REFERENCE]\n\n"
            f"{user_text}"
        )
        for variant, system_prompt in variants:
            requests.append({
                "case": case_name,
                "variant": variant,
                "system_prompt": system_prompt,
                "user_text": contextual_user,
            })
    return requests


async def run_comparison(client: Any, requests: list[dict[str, str]]) -> list[dict[str, str]]:
    results = []
    for request in requests:
        response = await client.chat_limited(
            [
                {"role": "system", "content": request["system_prompt"]},
                {"role": "user", "content": request["user_text"]},
            ],
            max_tokens=800,
            temperature=0.3,
            reasoning_effort="medium",
        )
        results.append({
            "case": request["case"],
            "variant": request["variant"],
            "response": str(response.get("content") or ""),
        })
    return results


def configured_client() -> tuple[str, LLMClient]:
    load_project_env(PROJECT_ROOT)
    catalog = configured_model_catalog()
    entry = catalog.resolve_persisted(read_selected_model())
    if entry is None:
        raise RuntimeError("The persisted model cannot be resolved from the configured catalog")
    profile = entry.profile
    api_key = profile.api_key()
    if not api_key and (is_local_url(entry.base_url) or not profile.api_key_env):
        api_key = "local"
    if not api_key:
        raise RuntimeError(f"{profile.api_key_env} is not configured for the selected remote model")
    config = LLMConfig(
        provider=profile.provider,
        model=entry.model_id,
        api_key=api_key,
        base_url=entry.base_url,
        capabilities=profile.capabilities,
        **profile.generation_settings(),
    )
    return entry.key, LLMClient(config)


async def _run(output: Path) -> None:
    model_key, client = configured_client()
    memory_block = "USER.md: The example user prefers concise explanations backed by evidence."
    results = await run_comparison(client, build_requests(memory_block))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"model": model_key, "results": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"PERSONA_AB_OK model={model_key} responses={len(results)} report={output}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=".astra/evals/persona-ab/report.json",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(Path(args.output)))
    except Exception as exc:  # noqa: BLE001 - provider implementations raise third-party exceptions
        safe = format_provider_error(exc, component="persona_ab")
        print(f"PERSONA_AB_UNAVAILABLE {safe}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
