"""Context Index relevance benchmark orchestrator.

Pipeline:
sessions.db → cases.jsonl → broker replay → local-LLM judge labels → metrics report.

Privacy: panel/query text only travels to the configured local endpoint; the
JSONL report stores handles-free aggregate rows (case_id, slot, source, reason,
label) and no body text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from urllib.parse import quote
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from agent.evals.context_index_eval import load_shadow_cases
from agent.runtime.context_index import WorkspaceIdentity, create_context_index_broker
from agent.runtime.context_index.session_source import content_fingerprint

from .cases import extract_cases
from .judge import OMLXTransport, judge_case
from .metrics import RowLabel, summarize

Transport = Callable[[str, str], str | None]


@dataclass
class _BenchmarkPreferences:
    mode: str = "all"
    char_budget: int = 900


def _snapshot_sqlite(source: Path, target: Path) -> None:
    """Freeze committed SQLite state (including WAL); publish only complete copies."""
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".db", dir=target.parent)
    os.close(fd)
    scratch = Path(temporary)
    try:
        uri = f"file:{quote(str(source.resolve()), safe='/')}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as origin:
            with closing(sqlite3.connect(scratch)) as snapshot:
                origin.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode=DELETE")
        # A competing replay may already have published the first snapshot.
        try:
            os.link(scratch, target)
        except FileExistsError:
            pass
    finally:
        scratch.unlink(missing_ok=True)


def freeze_replay_dbs(dest_dir: Path) -> dict[str, str]:
    """Copy the replay source DBs once; comparisons then read frozen snapshots.

    基线当日 S2 槽在两次重放间漂移（sessions.db 被真实使用追加）证明对比实验
    必须冻结。返回 factory 识别的环境变量覆盖映射（缺失的源记为空串）。
    """

    from agent.runtime import session_recall
    from agent.runtime import activity_store

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    envs: dict[str, str] = {}
    defaults = {
        "ASTRA_CONTEXT_INDEX_SESSIONS_DB": str(session_recall.DB_PATH),
        "ASTRA_CONTEXT_INDEX_ACTIVITY_DB": str(activity_store.default_activity_db_path()),
    }
    for env_name, default_path in defaults.items():
        source = Path(os.getenv(env_name, "").strip() or default_path)
        if not source.is_file():
            envs[env_name] = ""
            continue
        target = dest / source.name
        # 冻结必须幂等：已存在的副本是「实验开始那一刻」的快照，
        # 重跑不得覆盖（否则实时 db 的追加会渗进对照组）。
        # 新实验请用新目录，或显式删除旧冻结。
        if not target.exists():
            _snapshot_sqlite(source, target)
        envs[env_name] = str(target)
    return envs


def _freeze_vectors(dest_dir: Path) -> None:
    """冻结向量库与回放 db 同点：content_hash 对不上的命中天然弃（领先/滞后都安全）。"""
    from agent.runtime.context_index.vector_index import default_vectors_db_path

    source = default_vectors_db_path()
    if not source.is_file():
        return
    dest = Path(dest_dir) / "context-vectors.db"
    if not dest.exists():
        _snapshot_sqlite(source, dest)
    os.environ["ASTRA_CONTEXT_INDEX_VECTORS_DB"] = str(dest)


def run_benchmark(
    *,
    cases_path: Path,
    fixture_root: str,
    transport: Transport,
    broker: Any | None = None,
    freeze_dir: Path | None = None,
    ranking_override: str | None = None,
) -> tuple[dict[str, Any], list[RowLabel]]:
    """Replay every case, judge its panel, and aggregate metrics.

    ``ranking_override`` points at an alternate ranking.py for A/B runs: the
    swap only replaces broker-module bindings, so judge/transport/frozen DBs
    stay identical across arms (worktree copies drift on all of those).
    """

    import importlib.util
    from contextlib import contextmanager, nullcontext

    @contextmanager
    def _override(path: str) -> Iterator[None]:
        from agent.runtime.context_index import broker as broker_module

        spec = importlib.util.spec_from_file_location(
            # 子模块命名让普通模块的 __package__ 解析为 agent.runtime.context_index，
            # override 文件里的相对导入（from .models import …）才能命中真包；
            # 切勿给 spec 传 submodule_search_locations——那会把它变成 package、
            # 让 "." 前缀多解析一层而失败。
            "agent.runtime.context_index.ranking_override",
            path,
        )
        if spec is None or spec.loader is None:
            raise ValueError("ranking override module cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Keep preview rendering identical across ranking arms. Overrides use
        # select_rows(session, activity, plan, memory) under the same budget.
        names = ("select_rows",)
        originals = {name: getattr(broker_module, name) for name in names}
        for name in names:
            setattr(broker_module, name, getattr(module, name))
        try:
            yield
        finally:
            for name, value in originals.items():
                setattr(broker_module, name, value)

    cases = load_shadow_cases(Path(cases_path))
    ctx = _override(ranking_override) if ranking_override else nullcontext()
    with ctx:
        if broker is None:
            if freeze_dir is not None:
                os.environ.update({k: v for k, v in freeze_replay_dbs(freeze_dir).items() if v})
            broker = create_context_index_broker(_BenchmarkPreferences(), fixture_root)
        # 整个回放循环必须在 override 作用域内跑完：broker.build 是在这里
        # 才调用 select_rows，早退出 with 会让 A/B 形同虚设。
        return _replay_all(cases, broker=broker, fixture_root=fixture_root, transport=transport)


def _replay_all(
    cases: Sequence[Any],
    *,
    broker: Any,
    fixture_root: str,
    transport: Transport,
) -> tuple[dict[str, Any], list[RowLabel]]:
    panels: dict[str, list[RowLabel]] = {}
    rows_all: list[RowLabel] = []
    unjudged = 0
    empty_panels = 0
    for case in cases:
        workspace = WorkspaceIdentity(
            key=case.workspace_key,
            root=fixture_root,
            label=case.workspace_label,
        )
        asyncio.run(
            broker.build(
                case.user_text,
                f"bench-{case.case_id}",
                case.session_id or f"bench-{case.case_id}",
                workspace,
                case.local_time,
                frozenset({content_fingerprint(case.user_text)}),
                **({"query_message_id": case.message_id} if getattr(broker, "native_memory", False) else {}),
            )
        )
        trace = getattr(broker, "last_trace", None)
        displayed = list(trace.displayed) if trace is not None else []
        if not displayed:
            empty_panels += 1
            continue
        labels = judge_case(
            case.user_text,
            [(row.handle, row.source, row.description) for row in displayed],
            transport=transport,
        )
        if labels is None:
            unjudged += 1
            continue
        panel: list[RowLabel] = []
        for position, row in enumerate(displayed, start=1):
            if row.handle not in labels:
                continue
            labelled = RowLabel(
                source=row.source,
                slot=row.slot,
                reason=row.reason,
                position=position,
                label=labels[row.handle],
                case_id=case.case_id,
            )
            panel.append(labelled)
            rows_all.append(labelled)
        if panel:
            panels[case.case_id] = panel
    summary = summarize(panels)
    summary["requested_cases"] = len(cases)
    summary["unjudged_cases"] = unjudged
    summary["empty_panel_cases"] = empty_panels
    return summary, rows_all


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Context Index 相关性基线报告",
        "",
        "> 回放近似声明：候选集基于重放时刻的 sessions.db 终态（无时间旅行）；",
        "> 对比实验请固定同一 cases.jsonl。judge 为本地模型，标签非人工真值。",
        "",
        "## 总览",
        "",
        f"- 请求数 {summary['requested_cases']}｜有效面板 {summary['case_count']}"
        f"｜空面板 {summary['empty_panel_cases']}｜判失败 {summary['unjudged_cases']}",
        f"- **nDCG@6 = {summary['ndcg_at_6']}**｜precision@2 = {summary['precision_at_2']}"
        f"｜零命中请求率 = {summary['zero_hit_rate']}",
        "",
        "## 按推荐原因",
        "",
        "| reason | rows | mean_label(0-2) | relevant_rate |",
        "|---|---|---|---|",
    ]
    for reason, stats in summary["by_reason"].items():
        lines.append(
            f"| {reason} | {stats['count']} | {stats['mean_label']} | {stats['relevant_rate']} |"
        )
    lines += ["", "## 按槽位", "", "| slot | rows | mean_label | relevant_rate |", "|---|---|---|---|"]
    for slot, stats in summary["by_slot"].items():
        lines.append(f"| {slot} | {stats['count']} | {stats['mean_label']} | {stats['relevant_rate']} |")
    lines += ["", "## 按来源", "", "| source | rows | mean_label | relevant_rate |", "|---|---|---|---|"]
    for source, stats in summary["by_source"].items():
        lines.append(f"| {source} | {stats['count']} | {stats['mean_label']} | {stats['relevant_rate']} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Context Index relevance benchmark")
    parser.add_argument("--db", type=Path, help="sessions.db to sample turns from (writes --cases)")
    parser.add_argument("--cases", type=Path, required=True, help="case JSONL (input or written)")
    parser.add_argument("--fixture", required=True, help="workspace root used for broker replay")
    parser.add_argument("--max-cases", type=int, default=100)
    parser.add_argument("--base-url", default=os.getenv("BENCH_OMLX_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--model", default=os.getenv("BENCH_OMLX_MODEL", ""))
    parser.add_argument("--report-md", type=Path, default=None)
    parser.add_argument("--report-jsonl", type=Path, default=None)
    parser.add_argument("--no-freeze", action="store_true",
                        help="replay against live DBs (not reproducible; debug only)")
    parser.add_argument("--ranking-override", default=None,
                        help="path to an alternate ranking.py for A/B (frozen DBs + same toolchain)")
    parser.add_argument("--embedding", choices=("on", "off"), default="off",
                        help="enable the stage-2 embedding recall channel for this replay")
    args = parser.parse_args(argv)

    if args.db is not None:
        records = extract_cases(args.db, max_cases=args.max_cases)
        args.cases.parent.mkdir(parents=True, exist_ok=True)
        args.cases.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
        )
    transport = OMLXTransport(
        args.base_url,
        args.model,
        api_key=os.getenv("BENCH_OMLX_KEY", ""),
    )
    freeze_dir = None if args.no_freeze else args.cases.parent / "replay-frozen"
    # 显式双向：默认翻 on 后，对照臂必须真的把通道关死，否则 A/B 两组同开。
    os.environ["ASTRA_CONTEXT_INDEX_EMBEDDING"] = args.embedding
    if args.embedding == "on" and freeze_dir is not None:
        _freeze_vectors(freeze_dir)
    summary, rows = run_benchmark(
        cases_path=args.cases, fixture_root=args.fixture, transport=transport, freeze_dir=freeze_dir,
        ranking_override=args.ranking_override,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.report_md is not None:
        args.report_md.parent.mkdir(parents=True, exist_ok=True)
        args.report_md.write_text(_render_markdown(summary), encoding="utf-8")
    if args.report_jsonl is not None:
        args.report_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with args.report_jsonl.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        {
                            "case_id": row.case_id,
                            "source": row.source,
                            "slot": row.slot,
                            "reason": row.reason,
                            "position": row.position,
                            "label": row.label,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint.
    raise SystemExit(main())
