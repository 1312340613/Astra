"""Coding benchmark: real-agent tasks judged by pytest.

Each task is a real change pulled from agent-system git history. The
runner creates an isolated git worktree checked out at the task's start
commit, drives the ReAct agent (a real model whose file/code/git tools
are bound to that workspace) to implement the task, then runs the task's
judge tests in the workspace. Judge tests are invisible to the agent.

Usage:
    python -m agent.evals.coding_bench --task llm-chat-max-tokens --model qwen3.8-max
    python -m agent.evals.coding_bench --all --model deepseek-flash --rounds 3
    python -m agent.evals.coding_bench --task llm-chat-max-tokens --dry-run

Dry-run skips the LLM call and only proves the worktree setup and judge
pipeline work, so the harness itself can be validated without a key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASKS_ROOT = PROJECT_ROOT / "evals" / "coding" / "tasks"
RESULTS_ROOT = PROJECT_ROOT / "evals" / "coding" / "results"


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _load_setup(task_dir: Path) -> dict[str, Any]:
    raw = json.loads((task_dir / "setup.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid setup.json in {task_dir}")
    return raw


def _discover_tasks() -> list[Path]:
    if not TASKS_ROOT.exists():
        return []
    return sorted(
        child for child in TASKS_ROOT.iterdir()
        if child.is_dir() and (child / "setup.json").exists()
    )


def _setup_worktree(start_commit: str, target: Path) -> None:
    """Create an isolated worktree at the task's start commit."""
    if target.exists():
        _run(["git", "worktree", "remove", "--force", str(target)], cwd=PROJECT_ROOT)
    result = _run(["git", "worktree", "add", "--detach", str(target), start_commit], cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"worktree add failed: {result.stderr.strip()}")


def _remove_worktree(target: Path) -> None:
    _run(["git", "worktree", "remove", "--force", str(target)], cwd=PROJECT_ROOT)


async def _build_agent(workdir: Path, model_key: str) -> Any:
    """Assemble a minimal coding agent bound to the task workspace."""
    from agent.cli.backend import load_project_env
    from agent.cli.mode_preferences import apply_reasoning_effort
    from agent.cli.model_catalog import configured_model_catalog
    from agent.runtime.llm import LLMClient, LLMConfig
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolRegistry
    from agent.runtime.tools.files import register_file_tools
    from agent.runtime.tools.code import register_code_tools
    from agent.runtime.tools.git import register_git_tools
    from agent.runtime.tools.time import register_time_tools
    from agent.runtime.prompts import DEFAULT_SYSTEM_PROMPT
    from agent.sandbox.local import LocalSandbox

    load_project_env(PROJECT_ROOT)
    catalog = configured_model_catalog()
    entry = catalog.resolve(model_key) or catalog.resolve_persisted(model_key)
    if entry is None:
        raise ValueError(f"unknown model: {model_key}")
    profile = entry.profile
    api_key = profile.api_key()
    if not api_key:
        raise ValueError(f"{profile.api_key_env} not set for model {model_key}")
    llm_config = apply_reasoning_effort(LLMConfig(
        provider=profile.provider,
        model=entry.model_id,
        api_key=api_key or "local",
        base_url=entry.base_url,
        capabilities=profile.capabilities,
        **profile.generation_settings(),
    ))
    llm = LLMClient(llm_config)
    tools = ToolRegistry()
    sandbox = LocalSandbox(timeout=120, workdir=str(workdir))
    register_file_tools(tools, workdir=str(workdir), sandbox=sandbox)
    register_code_tools(tools, sandbox, task_store=None)
    register_git_tools(tools, workdir=str(workdir))
    register_time_tools(tools)
    agent = ReActAgent(
        name="coding-bench",
        llm_client=llm,
        tool_registry=tools,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        max_iterations=40,
    )
    agent.context.set_session(str(workdir / ".bench-session.jsonl"))
    agent.begin_session()
    return agent


async def _run_task_round(
    task_dir: Path,
    work_root: Path,
    model_key: str,
    dry_run: bool,
) -> dict[str, Any]:
    setup = _load_setup(task_dir)
    task_id = task_dir.name
    start_commit = str(setup["start_commit"])
    task_text = (task_dir / "task.md").read_text(encoding="utf-8").strip()
    workspace = work_root / task_id

    _setup_worktree(start_commit, workspace)
    started = time.monotonic()
    result: dict[str, Any] = {
        "task": task_id,
        "model": model_key,
        "start_commit": start_commit,
        "status": "error",
        "wall_seconds": 0.0,
        "iterations": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "judge_stdout": "",
        "error": "",
    }
    try:
        if dry_run:
            result["status"] = "dry-run"
            return result

        agent = await _build_agent(workspace, model_key)
        from agent.core.msg import ContentBlock, Msg

        msg = Msg(sender="user", role="user", content=[ContentBlock.text(task_text)], id=f"bench-{task_id}")
        await agent.reply(msg)
        result["iterations"] = getattr(agent.context, "iteration_count", 0)
        result["prompt_tokens"] = agent.context.total_prompt_tokens
        result["completion_tokens"] = agent.context.total_completion_tokens

        judge_file = task_dir / "verify" / "test_task.py"
        judge_result = _run(
            [sys.executable, "-m", "pytest", str(judge_file), "-q", "-p", "no:cacheprovider"],
            cwd=workspace,
            timeout=180.0,
        )
        result["judge_stdout"] = judge_result.stdout[-4000:] + judge_result.stderr[-2000:]
        result["status"] = "pass" if judge_result.returncode == 0 else "fail"
    except Exception as exc:  # noqa: BLE001 — a round failure is a data point
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["wall_seconds"] = round(time.monotonic() - started, 2)
        _remove_worktree(workspace)
    return result


async def _run_all(
    tasks: list[Path],
    work_root: Path,
    model_key: str,
    rounds: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for task_dir in tasks:
        for round_index in range(1, rounds + 1):
            outcome = await _run_task_round(task_dir, work_root, model_key, dry_run)
            outcome["round"] = round_index
            results.append(outcome)
            summary = (
                f"[{outcome['task']} r{round_index}] {outcome['status']} "
                f"{outcome['wall_seconds']}s"
            )
            print(summary, flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="task id (directory name under evals/coding/tasks)")
    parser.add_argument("--all", action="store_true", help="run every task")
    parser.add_argument("--model", default="qwen3.8-max", help="model profile key")
    parser.add_argument("--rounds", type=int, default=1, help="repetitions per task")
    parser.add_argument("--work-root", default=str(PROJECT_ROOT / ".tmp" / "coding-bench"), help="worktree root")
    parser.add_argument("--dry-run", action="store_true", help="validate pipeline without calling the model")
    args = parser.parse_args(argv)

    if args.task and args.all:
        print("--task and --all are mutually exclusive")
        return 2
    if args.all:
        tasks = _discover_tasks()
    elif args.task:
        candidate = TASKS_ROOT / args.task
        tasks = [candidate] if (candidate / "setup.json").exists() else []
        if not tasks:
            print(f"unknown task: {args.task}")
            return 2
    else:
        print("pass --task <id> or --all")
        return 2

    work_root = Path(args.work_root).expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    results = asyncio.run(_run_all(tasks, work_root, args.model, max(1, args.rounds), args.dry_run))

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report_path = RESULTS_ROOT / f"report_{args.model}_{stamp}.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport: {report_path}", flush=True)

    passed = sum(1 for item in results if item["status"] == "pass")
    print(f"summary: {passed}/{len(results)} passed", flush=True)
    return 0 if args.dry_run or passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
