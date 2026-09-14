"""Review an isolated copy of legacy candidates with the configured model.

This is an acceptance harness, not a production migration. Original candidates,
skills and memory are read-only. Observation wrappers are test inputs, not a
claim that environment facts belong in the skill library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.cli.environment import load_project_env  # noqa: E402
from agent.cli.model_catalog import configured_model_catalog  # noqa: E402
from agent.runtime.llm import LLMClient, LLMConfig  # noqa: E402
from agent.runtime.skill_curation import SkillCurator  # noqa: E402
from agent.runtime.skill_learning import LearnedSkills  # noqa: E402
from agent.runtime.skills import SkillStore  # noqa: E402


def snapshot_candidates(source: Path, output: Path) -> list[dict]:
    # SQLite's backup API includes committed WAL contents consistently.
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        with sqlite3.connect(output / "legacy.snapshot.db") as copy:
            db.backup(copy)
            copy.row_factory = sqlite3.Row
            rows = [dict(row) for row in copy.execute(
                "SELECT id,session_id,kind,payload_json,reason,status FROM learning_proposals WHERE status='pending' ORDER BY id"
            )]
    for row in rows:
        row["payload"] = json.loads(row.pop("payload_json"))
    (output / "candidates.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def seed_fixture(rows: list[dict], learned: LearnedSkills, original_skills: SkillStore) -> list[dict]:
    mapping = []
    for row in rows:
        payload = row["payload"]
        name = str(payload.get("name") or "legacy-" + row["id"].replace("_", "-"))
        content = str(payload.get("content") or "")
        if row["kind"] == "skill_patch":
            current = original_skills.raw_file(name, payload.get("file_path") or "SKILL.md") or ""
            old = str(payload.get("old_string") or "")
            if old and current.count(old) == 1:
                content = current.replace(old, str(payload.get("new_string") or ""), 1)
            else:
                content = "Historical patch with an unavailable/nonmatching target; do not invent the result.\n" + json.dumps(payload, ensure_ascii=False)
        if not content.startswith("---\n"):
            label = "Historical environment observation; assess whether this belongs in a working handbook" if row["kind"] == "observation" else "Historical procedure candidate; check the supplied provenance"
            content = f"---\nname: {name}\ndescription: {label}\n---\n\n" + content
        learned.manage("create", name, content=content, source={
            "legacy_id": row["id"], "kind": row["kind"], "session_id": row["session_id"],
            "reason": row["reason"], "evidence": str(payload.get("evidence") or ""),
            "notice": "Isolated test wrapper for an unverified historical candidate. Not a production migration.",
        })
        mapping.append({"id": row["id"], "name": name, "kind": row["kind"]})
    return mapping


async def run(args: argparse.Namespace) -> None:
    source = Path(args.installation).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    load_project_env(source)
    # Resolve configuration only; no discovery requests to other providers.
    settings = json.loads((source / ".astra" / "settings.json").read_text(encoding="utf-8"))
    entry = configured_model_catalog().resolve_persisted(args.model or settings.get("selected_model"))
    if entry is None:
        raise ValueError("Configured model could not be resolved")
    profile = entry.profile
    client = LLMClient(LLMConfig(provider=profile.provider, model=entry.model_id,
        api_key=profile.api_key() or os.getenv("LLM_API_KEY", "") or "local-no-key",
        base_url=entry.base_url, capabilities=profile.capabilities, max_tokens=4096))
    class CapturedModel:
        calls = 0

        async def chat_limited(self, messages, **kwargs):
            self.calls += 1
            response = await client.chat_limited(messages, **kwargs)
            evidence = {key: response.get(key) for key in ("content", "finish_reason", "usage")}
            (output / f"model-response-{self.calls}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            return response

    llm = CapturedModel()
    rows = snapshot_candidates(source / ".astra" / "learning.db", output)
    learned = LearnedSkills(SkillStore(output / "skills"))
    mapping = seed_fixture(rows, learned, SkillStore(source / ".astra" / "skills"))
    report = {"model": entry.key, "input_count": len(rows), "mapping": mapping,
              "production_data": "read_only", "quality_review": "pending human inspection", "runs": []}
    try:
        for _ in range(len(rows) + 1):
            result = await SkillCurator(llm, learned).review(lambda message: print(message, flush=True))
            public = {key: result[key] for key in ("id", "time", "kind", "status", "actions", "source")}
            report["runs"].append(public)
            print(json.dumps({"run": result["id"], "actions": len(result["actions"]), "review": result["source"]["review"]}, ensure_ascii=False), flush=True)
            if result["source"]["review"]["remaining"] == 0:
                break
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Inspect full original/revised snapshots under {learned.home / 'runs'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", required=True)
    parser.add_argument("--output", required=True, help="New empty directory for isolated artifacts")
    parser.add_argument("--model", default="")
    asyncio.run(run(parser.parse_args()))
