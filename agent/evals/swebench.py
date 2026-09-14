"""SWE-bench Verified runner for Astra coding agents.

Loads a local JSON manifest exported from the official SWE-bench_Verified
dataset, prepares an isolated checkout per instance, runs the real ReAct
agent against the problem statement, applies the hidden test patch, and
judges the result with pytest using the official FAIL_TO_PASS / PASS_TO_PASS
node lists.

Usage:
    python -m agent.evals.swebench --dry-run
    python -m agent.evals.swebench --instances psf__requests-1142
    python -m agent.evals.swebench --all --model deepseek-flash
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "evals" / "coding" / "swebench_verified_subset.json"
RESULTS_ROOT = PROJECT_ROOT / "evals" / "coding" / "results"

_COMPAT_SHIM = """\
import collections
import collections.abc
for _name in (
    "MutableMapping", "Mapping", "Sequence", "Set", "MappingView",
    "ItemsView", "KeysView", "ValuesView",
):
    if not hasattr(collections, _name):
        setattr(collections, _name, getattr(collections.abc, _name))
"""


def _write_compat_shim(workspace: Path) -> Path:
    shim_dir = workspace / ".swebench-compat"
    shim_dir.mkdir(exist_ok=True)
    (shim_dir / "sitecustomize.py").write_text(_COMPAT_SHIM, encoding="utf-8")
    return shim_dir


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 600.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def load_instances(manifest: Path) -> list[dict[str, Any]]:
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"invalid SWE-bench manifest: {manifest}")
    for item in raw:
        required = {
            "repo", "instance_id", "base_commit", "problem_statement",
            "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS",
        }
        missing = required - set(item)
        if missing:
            raise ValueError(f"{item.get('instance_id', '?')} missing {sorted(missing)}")
    return raw


def instance_workspace(instance: dict[str, Any], work_root: Path) -> Path:
    safe_id = str(instance["instance_id"]).replace("/", "__")
    return work_root / safe_id


def clone_and_checkout(instance: dict[str, Any], work_root: Path, *, force: bool = False) -> Path:
    workspace = instance_workspace(instance, work_root)
    if force and workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    if not (workspace / ".git").exists():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        clone_error = ""
        for attempt, command in enumerate((
            ["git", "clone", "--filter=blob:none", f"https://github.com/{instance['repo']}.git", str(workspace)],
            ["git", "clone", f"https://github.com/{instance['repo']}.git", str(workspace)],
        ), start=1):
            clone = _run(command, cwd=work_root, timeout=1200.0)
            if clone.returncode == 0:
                break
            clone_error = clone.stderr.strip()
            shutil.rmtree(workspace, ignore_errors=True)
            if attempt == 2:
                raise RuntimeError(
                    f"clone failed for {instance['instance_id']}: {clone_error[-2000:]}"
                )
    checkout = _run(["git", "checkout", "--detach", str(instance["base_commit"])], cwd=workspace)
    if checkout.returncode != 0:
        raise RuntimeError(f"checkout failed for {instance['instance_id']}: {checkout.stderr.strip()}")
    return workspace


def apply_test_patch(instance: dict[str, Any], workspace: Path) -> None:
    patch_text = str(instance["test_patch"])
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as patch_file:
        patch_file.write(patch_text)
        patch_path = Path(patch_file.name)
    try:
        check = _run(["git", "apply", "--check", str(patch_path)], cwd=workspace)
        if check.returncode != 0:
            reverse_check = _run(["git", "apply", "--reverse", "--check", str(patch_path)], cwd=workspace)
            if reverse_check.returncode == 0:
                return  # already applied by an earlier dry-run
            raise RuntimeError(f"test patch apply failed: {check.stderr.strip()}")
        result = _run(["git", "apply", "--verbose", str(patch_path)], cwd=workspace)
        if result.returncode != 0:
            raise RuntimeError(f"test patch apply failed: {result.stderr.strip()}")
    finally:
        patch_path.unlink(missing_ok=True)


def install_repo(workspace: Path) -> str:
    pip = str(Path(sys.executable).with_name("pip"))
    if not Path(pip).exists():
        pip = "pip"
    result = _run([pip, "install", "-e", "."], cwd=workspace, timeout=1800.0)
    if result.returncode != 0:
        return f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return ""


def judge_instance(instance: dict[str, Any], workspace: Path) -> dict[str, Any]:
    tests = [*instance["FAIL_TO_PASS"], *instance["PASS_TO_PASS"]]
    if not tests:
        return {"resolved": False, "returncode": -1, "stdout": "", "stderr": "no judge tests"}
    shim_dir = _write_compat_shim(workspace)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(shim_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = _run(
        [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider"],
        cwd=workspace,
        timeout=1800.0,
        env=env,
    )
    return {
        "resolved": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-2000:],
    }


async def run_instance(
    instance: dict[str, Any],
    *,
    work_root: Path,
    model_key: str,
    dry_run: bool,
    install: bool,
    force: bool = False,
) -> dict[str, Any]:
    instance_id = str(instance["instance_id"])
    started = time.monotonic()
    result: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": str(instance["repo"]),
        "model": model_key,
        "status": "error",
        "resolved": False,
        "wall_seconds": 0.0,
        "iterations": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "judge_returncode": -1,
        "judge_stdout": "",
        "judge_stderr": "",
        "install_log": "",
        "error": "",
    }
    workspace = instance_workspace(instance, work_root)
    try:
        workspace = clone_and_checkout(instance, work_root, force=force)
        if install:
            install_log = install_repo(workspace)
            if install_log:
                result["install_log"] = install_log[-4000:]
                result["error"] = f"dependency install failed for {instance_id}"
                result["status"] = "error"
                return result

        if dry_run:
            apply_test_patch(instance, workspace)
            judge = judge_instance(instance, workspace)
            result["status"] = "dry-run"
            result["judge_returncode"] = judge["returncode"]
            result["judge_stdout"] = judge["stdout"]
            result["judge_stderr"] = judge["stderr"]
            result["wall_seconds"] = round(time.monotonic() - started, 2)
            return result

        from agent.core.msg import ContentBlock, Msg
        from agent.evals.coding_bench import _build_agent

        agent = await _build_agent(workspace, model_key)
        task = str(instance["problem_statement"]).strip()
        msg = Msg(
            sender="user",
            role="user",
            content=[ContentBlock.text(task)],
            id=f"swebench-{instance_id}",
        )
        await agent.reply(msg)
        result["iterations"] = int(getattr(agent.context, "iteration_count", 0) or 0)
        result["prompt_tokens"] = int(agent.context.total_prompt_tokens or 0)
        result["completion_tokens"] = int(agent.context.total_completion_tokens or 0)
        result["cache_hit_tokens"] = int(agent.context.total_cache_hit_tokens or 0)
        result["cache_miss_tokens"] = int(agent.context.total_cache_miss_tokens or 0)

        apply_test_patch(instance, workspace)
        judge = judge_instance(instance, workspace)
        result["resolved"] = judge["resolved"]
        result["judge_returncode"] = judge["returncode"]
        result["judge_stdout"] = judge["stdout"]
        result["judge_stderr"] = judge["stderr"]
        result["status"] = "pass" if judge["resolved"] else "fail"
    except Exception as exc:  # noqa: BLE001 — instance failure is a data point
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "error"
    finally:
        result["wall_seconds"] = round(time.monotonic() - started, 2)
    return result


async def run_instances(
    instances: list[dict[str, Any]],
    *,
    work_root: Path,
    model_key: str,
    dry_run: bool,
    install: bool,
    force: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for instance in instances:
        outcome = await run_instance(
            instance,
            work_root=work_root,
            model_key=model_key,
            dry_run=dry_run,
            install=install,
            force=force,
        )
        results.append(outcome)
        print(
            f"[{outcome['instance_id']}] {outcome['status']} "
            f"{outcome['wall_seconds']}s",
            flush=True,
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="local SWE-bench JSON manifest")
    parser.add_argument("--instances", default="", help="comma-separated instance ids")
    parser.add_argument("--all", action="store_true", help="run every instance in the manifest")
    parser.add_argument("--model", default="qwen3.8-max", help="model profile key")
    parser.add_argument("--rounds", type=int, default=1, help="repetitions per instance")
    parser.add_argument("--work-root", default=str(PROJECT_ROOT / ".tmp" / "swebench"), help="checkout root")
    parser.add_argument("--dry-run", action="store_true", help="prepare and judge baseline without calling the model")
    parser.add_argument("--skip-install", action="store_true", help="do not pip install the repository")
    parser.add_argument("--force", action="store_true", help="recreate existing checkouts")
    args = parser.parse_args(argv)

    manifest = Path(args.manifest).expanduser().resolve()
    instances = load_instances(manifest)
    if args.instances:
        wanted = {item.strip() for item in args.instances.split(",") if item.strip()}
        instances = [item for item in instances if item["instance_id"] in wanted]
        if not instances:
            print(f"no matching instances for {args.instances!r}")
            return 2
    elif not args.all:
        print("pass --instances <ids>, --all, or --dry-run with an explicit instance list")
        return 2

    work_root = Path(args.work_root).expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    all_results: list[dict[str, Any]] = []
    for round_index in range(1, max(1, args.rounds) + 1):
        outcomes = asyncio.run(run_instances(
            instances,
            work_root=work_root,
            model_key=args.model,
            dry_run=args.dry_run,
            install=not args.skip_install,
            force=args.force,
        ))
        for outcome in outcomes:
            outcome["round"] = round_index
        all_results.extend(outcomes)

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report_path = RESULTS_ROOT / f"swebench_{args.model}_{stamp}.json"
    report_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport: {report_path}", flush=True)

    resolved = sum(1 for item in all_results if item["resolved"])
    attempted = sum(1 for item in all_results if not args.dry_run)
    print(f"summary: {resolved}/{attempted} resolved" if attempted else f"summary: {len(all_results)} dry-run", flush=True)
    return 0 if args.dry_run or resolved == attempted else 1


if __name__ == "__main__":
    raise SystemExit(main())
