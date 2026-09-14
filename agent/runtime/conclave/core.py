"""Conclave core — multi-expert research pipeline with SearXNG search."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

SEARXNG_URL = "http://localhost:8080/search"


# ── Expert pool ──────────────────────────────────────────────

EXPERT_POOL = [
    # (name, engine, category, guidance)
    ("Google 专家",   "google",         "综合搜索",   "通才研究员。优先主流英文来源，多来源交叉验证。"),
    ("Bing 专家",     "bing",           "综合搜索",   "补充视角。找到 Google 遗漏的信息。"),
    ("学术专家",      "google scholar", "学术论文",   "学术视角。优先 arXiv/顶会论文，评估方法论严谨性。"),
    ("arXiv 专家",    "arxiv",          "学术论文",   "预印本专家。最新的未发表研究，标注未同行评审。"),
    ("GitHub 专家",   "github",         "代码仓库",   "代码与开源生态。搜 repo/issue/PR/README。"),
    ("知乎专家",      "zhihu",          "中文问答",   "中文高质量问答。高赞深度回答，业内人士观点。"),
    ("百度专家",      "baidu",          "中文搜索",   "中文搜索。百度知道/贴吧/百家号。"),
    ("Wikipedia 专家","wikipedia",      "百科知识",   "百科。概念定义、历史背景、标准规范。"),
    ("新闻专家",      "google news",    "新闻资讯",   "时效追踪。按时间排序，标注发布时间。"),
    ("StackOverflow 专家","stackoverflow","技术问答", "实战问题。被采纳的高票答案和生产级解决方案。"),
    ("Reddit 专家",   "reddit",         "社区讨论",   "真实用户讨论和反馈。关注争议点。"),
    ("B站专家",       "bilibili",       "中文视频",   "技术教程和产品评测视频。"),
]


INTENT_ROUTES = {
    "comparison":  ("对比调研",  ["Google 专家","Bing 专家","学术专家","知乎专家"]),
    "news":        ("时效新闻",  ["新闻专家","Reddit 专家","Bing 专家"]),
    "academic":    ("学术调研",  ["学术专家","arXiv 专家","Wikipedia 专家"]),
    "code":        ("技术方案",  ["GitHub 专家","StackOverflow 专家","Reddit 专家"]),
    "china":       ("中文视角",  ["百度专家","知乎专家","B站专家"]),
    "howto":       ("实践指南",  ["StackOverflow 专家","GitHub 专家","B站专家"]),
    "general":     ("全面调研",  ["Google 专家","Bing 专家","学术专家","知乎专家","新闻专家"]),
}


def detect_intent(question: str) -> str:
    q = question.lower()
    keywords = {
        "对比": "comparison", "vs": "comparison", "区别": "comparison",
        "论文": "academic", "研究": "academic", "学术": "academic",
        "代码": "code", "实现": "code", "开源": "code",
        "新闻": "news", "事件": "news", "最新": "news",
        "怎么做": "howto", "教程": "howto", "入门": "howto",
        "中国": "china", "国内": "china", "中文": "china",
    }
    for kw, intent in keywords.items():
        if kw in q:
            return intent
    return "general"


def searx_search(query: str, engine: str, limit: int = 6) -> list[dict]:
    try:
        resp = requests.get(SEARXNG_URL, params={
            "q": query, "format": "json", "engines": engine,
            "categories": "general,it,science",
        }, timeout=30)
        results = resp.json().get("results", [])[:limit]
        if len(results) < 3 and engine not in ("google", "bing", "baidu"):
            resp = requests.get(SEARXNG_URL, params={
                "q": query, "format": "json",
                "categories": "general,it,science",
            }, timeout=30)
            results = resp.json().get("results", [])[:limit]
        return [{"title": r["title"], "url": r["url"],
                 "snippet": r.get("content", "")[:300]} for r in results]
    except Exception as e:
        logger.warning("SearXNG search failed (engine=%s): %s", engine, e)
        return []


def expert_search(
    name: str,
    engine: str,
    question: str,
    limit: int = 6,
) -> list[dict]:
    """One expert does their research and returns findings."""
    # 1. Search their engine
    results = searx_search(question, engine, limit=limit)
    # 2. If few results, try a broader rephrase
    if len(results) < 2:
        results = searx_search(f"{question} 2026", engine, limit=limit)
    return results


def resolve_experts(intent: str, configured: str | list[str] | None = None) -> list[tuple]:
    """Resolve auto-routed or explicitly configured expert names.

    Matching ignores whitespace and case so TUI labels such as
    ``GitHub 专家`` cannot drift from persisted values such as ``GitHub专家``.
    """
    by_name = {
        "".join(expert[0].split()).casefold(): expert
        for expert in EXPERT_POOL
    }
    if configured is None or configured == "auto":
        _, names = INTENT_ROUTES.get(intent, INTENT_ROUTES["general"])
    elif isinstance(configured, str):
        names = [item.strip() for item in configured.split(",") if item.strip()]
    else:
        names = [str(item).strip() for item in configured if str(item).strip()]
    if not names:
        raise ValueError("Conclave expert list is empty; use 'auto' or choose at least one expert")

    selected = []
    unknown = []
    for name in names[:8]:
        expert = by_name.get("".join(name.split()).casefold())
        if expert is None:
            unknown.append(name)
        elif expert not in selected:
            selected.append(expert)
    if unknown:
        available = ", ".join(expert[0] for expert in EXPERT_POOL)
        raise ValueError(
            f"Unknown Conclave expert(s): {', '.join(unknown)}. Available: {available}"
        )
    return selected


# ── Pipeline ─────────────────────────────────────────────────

@dataclass
class ConclaveResult:
    question: str
    intent: str
    experts_selected: list[str]
    findings: dict[str, list[dict]] = field(default_factory=dict)
    expert_reports: dict[str, str] = field(default_factory=dict)
    discussion_contributions: dict[str, str] = field(default_factory=dict)
    cross_discussion: str = ""
    transcript_path: str = ""
    synthesis: str = ""
    duration_ms: float = 0.0
    stats: dict[str, int] = field(default_factory=dict)


def unique_source_count(findings: dict[str, list[dict]]) -> int:
    """Count distinct source URLs across experts, with a title fallback."""
    seen: set[str] = set()
    for results in findings.values():
        for item in results:
            key = str(item.get("url") or item.get("title") or "").strip().casefold()
            if key:
                seen.add(key)
    return len(seen)


class Conclave:
    """Multi-expert research pipeline.

    Phase 0: Detect intent and select experts.
    Phase 1: Experts run multiple searches and write independent reports.
    Phase 2: Optionally let experts respond in order to reports and prior turns.
    Phase 3: Chairperson synthesises every report and discussion contribution.
    """

    def __init__(self, chairperson_prompt: str | None = None):
        self.chairperson_prompt = chairperson_prompt

    async def run(
        self,
        question: str,
        llm_chat: Any = None,
        stream_events: Any = None,
        *,
        experts: str | list[str] | None = None,
        max_sources_per_expert: int = 5,
        cross_discussion: bool = True,
        search: Any = None,
        expert_llm_chat: Any = None,
        max_search_rounds: int = 2,
        max_parallel_experts: int = 4,
    ) -> ConclaveResult:
        """Run the full conclave pipeline.

        Args:
            question: The user's research question.
            llm_chat: Async callable(messages) → str for chairperson LLM calls.
            stream_events: Optional async callback(event_dict) for live streaming.
            expert_llm_chat: Optional separate callback for expert reports and turns.
        """
        start = time.monotonic()
        result = ConclaveResult(question=question, intent="general", experts_selected=[])

        # ─── Phase 0: Detect intent & select experts ───
        result.intent = detect_intent(question)
        selected = resolve_experts(result.intent, experts)
        result.experts_selected = [expert[0] for expert in selected]
        source_limit = max(1, min(20, int(max_sources_per_expert)))
        # Two distinct searches are the minimum useful expert pass.  Keeping the
        # ceiling at three bounds network cost even when persisted config drifts.
        search_round_limit = max(2, min(3, int(max_search_rounds)))
        parallel_limit = max(1, min(8, int(max_parallel_experts)))
        expert_chat = expert_llm_chat or llm_chat
        expert_semaphore = asyncio.Semaphore(parallel_limit)
        stats = {
            "search_calls": 0,
            "search_failures": 0,
            "expert_llm_calls": 0,
            "expert_failures": 0,
            "discussion_llm_calls": 0,
            "discussion_failures": 0,
            "chairperson_llm_calls": 0,
            "chairperson_failures": 0,
        }
        stats_lock = asyncio.Lock()

        if stream_events:
            await stream_events({
                "type": "conclave_phase",
                "phase": 0,
                "label": f"{INTENT_ROUTES[result.intent][0]}",
                "experts": result.experts_selected,
            })

        # ─── Phase 1: Parallel expert research ───
        if stream_events:
            await stream_events({"type": "conclave_phase", "phase": 1,
                                 "label": f"{len(selected)} 位专家并行搜索中..."})

        async def increment(key: str) -> None:
            async with stats_lock:
                stats[key] += 1

        async def run_search(expert, query: str) -> list[dict]:
            name, engine, _category, _guidance = expert
            await increment("search_calls")
            try:
                if search is not None:
                    return list(await search(expert, query, source_limit) or [])
                return list(await asyncio.to_thread(
                    expert_search,
                    name,
                    engine,
                    query,
                    source_limit,
                ) or [])
            except Exception:
                await increment("search_failures")
                raise

        async def research_one(expert):
            name, _engine, category, _guidance = expert
            started = time.monotonic()
            if stream_events:
                await stream_events({
                    "type": "conclave_expert_started",
                    "expert": name,
                    "category": category,
                })
            async with expert_semaphore:
                findings: list[dict] = []
                queries = self._build_search_queries(
                    question,
                    expert,
                    search_round_limit,
                )
                try:
                    for round_index, query in enumerate(queries, start=1):
                        try:
                            more = await run_search(expert, query)
                        except Exception as exc:
                            logger.warning(
                                "Conclave search failed expert=%s round=%s error=%s",
                                name,
                                round_index,
                                exc,
                            )
                            continue
                        findings = self._merge_findings(findings, more)[:source_limit]

                    if expert_chat:
                        await increment("expert_llm_calls")
                        try:
                            report = await expert_chat([{
                                "role": "user",
                                "content": self._build_expert_report_prompt(
                                    question,
                                    expert,
                                    findings,
                                    queries,
                                ),
                            }])
                        except Exception as exc:
                            await increment("expert_failures")
                            logger.warning(
                                "Conclave expert report failed expert=%s error=%s",
                                name,
                                exc,
                            )
                            report = ""
                    else:
                        report = ""
                    report = report or self._fallback_expert_report(name, findings)
                    report = self._ensure_report_evidence(report, findings)
                except Exception as exc:
                    await increment("expert_failures")
                    logger.warning("Conclave expert failed name=%s error=%s", name, exc)
                    report = self._fallback_expert_report(name, findings, error=str(exc))
                duration_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "Conclave expert complete name=%s searches=%s sources=%s duration_ms=%s",
                    name,
                    len(queries),
                    len(findings),
                    duration_ms,
                )
                if stream_events:
                    await stream_events({
                        "type": "conclave_expert_completed",
                        "expert": name,
                        "sources": len(findings),
                        "searches": len(queries),
                        "duration_ms": duration_ms,
                    })
                return name, findings, report

        tasks = [research_one(e) for e in selected]
        findings_list = await asyncio.gather(*tasks, return_exceptions=True)
        result.findings = {}
        result.expert_reports = {}
        for expert, item in zip(selected, findings_list):
            name = expert[0]
            if isinstance(item, BaseException):
                logger.warning("Conclave expert failed name=%s error=%s", name, item)
                stats["expert_failures"] += 1
                result.findings[name] = []
                result.expert_reports[name] = self._fallback_expert_report(
                    name, [], error=str(item),
                )
            else:
                result.findings[item[0]] = item[1]
                result.expert_reports[item[0]] = item[2]

        if stream_events:
            total = unique_source_count(result.findings)
            await stream_events({"type": "conclave_findings",
                                 "total_sources": total,
                                 "experts": list(result.findings.keys())})

        # ─── Phase 2: ordered expert cross-discussion ───
        if stream_events and cross_discussion:
            await stream_events({"type": "conclave_phase", "phase": 2,
                                 "label": "专家按顺序交叉质询中..."})

        if cross_discussion and expert_chat:
            contributions: list[str] = []
            for expert in selected:
                name = expert[0]
                try:
                    await increment("discussion_llm_calls")
                    contribution = await expert_chat([{
                        "role": "user",
                        "content": self._build_expert_discussion_prompt(
                            question,
                            expert,
                            result.expert_reports,
                            contributions,
                        ),
                    }])
                except Exception as exc:
                    await increment("discussion_failures")
                    logger.warning(
                        "Conclave discussion contribution failed expert=%s error=%s",
                        name,
                        exc,
                    )
                    contribution = f"（{name} 交叉讨论失败：{type(exc).__name__}）"
                contribution = contribution or f"（{name} 没有补充意见）"
                result.discussion_contributions[name] = contribution
                contributions.append(f"## {name}\n{contribution}")
                if stream_events:
                    await stream_events({
                        "type": "conclave_discussion_contribution",
                        "expert": name,
                        "index": len(contributions),
                        "total": len(selected),
                    })
            result.cross_discussion = "\n\n".join(contributions)
        elif cross_discussion:
            result.cross_discussion = "(无 LLM 可用，跳过专家交叉讨论)"
        else:
            result.cross_discussion = "(交叉讨论已关闭)"

        if stream_events and cross_discussion and result.cross_discussion:
            await stream_events({"type": "conclave_discussion",
                                 "content": result.cross_discussion[:500]})

        # ─── Phase 3: Chairperson synthesis ───
        if stream_events:
            await stream_events({"type": "conclave_phase", "phase": 3,
                                 "label": "主席整合最终报告..."})

        synthesis_prompt = self._build_synthesis_prompt(
            question,
            result.intent,
            result.findings,
            result.cross_discussion,
            result.expert_reports,
        )
        if llm_chat:
            stats["chairperson_llm_calls"] += 1
            try:
                synthesis = await llm_chat([{"role": "user", "content": synthesis_prompt}])
            except Exception as exc:
                stats["chairperson_failures"] += 1
                logger.warning(
                    "Conclave chairperson synthesis failed; returning expert reports error=%s",
                    exc,
                    exc_info=True,
                )
                synthesis = ""
            result.synthesis = synthesis or self._fallback_synthesis(
                question, result.findings, result.expert_reports,
            )
        else:
            result.synthesis = self._fallback_synthesis(
                question, result.findings, result.expert_reports,
            )

        result.duration_ms = (time.monotonic() - start) * 1000
        result.stats = dict(stats)
        logger.info(
            "Conclave complete experts=%s sources=%s discussion=%s stats=%s duration_ms=%s",
            len(selected),
            unique_source_count(result.findings),
            cross_discussion,
            result.stats,
            int(result.duration_ms),
        )

        if stream_events:
            total_sources = unique_source_count(result.findings)
            await stream_events({"type": "conclave_done",
                                 "duration_ms": result.duration_ms,
                                 "experts": len(selected),
                                 "sources": total_sources})

        return result

    @staticmethod
    def _merge_findings(existing: list[dict], incoming: list[dict]) -> list[dict]:
        merged = list(existing)
        seen = {
            str(item.get("url") or item.get("title") or "").strip().casefold()
            for item in existing
        }
        for item in incoming:
            key = str(item.get("url") or item.get("title") or "").strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _format_evidence(findings: list[dict], *, limit: int = 8) -> str:
        if not findings:
            return "(没有检索到可引用来源)"
        return "\n".join(
            f"[{index}] {item.get('title') or '(无标题)'}\n"
            f"URL: {item.get('url') or '(无 URL)'}\n"
            f"摘要: {str(item.get('snippet') or '')[:500]}"
            for index, item in enumerate(findings[:limit], start=1)
        )

    @staticmethod
    def _build_search_queries(question: str, expert, rounds: int) -> list[str]:
        _name, _engine, category, guidance = expert
        focus = {
            "学术论文": "peer reviewed evidence methodology limitations",
            "代码仓库": "GitHub implementation issues benchmarks",
            "技术问答": "production failure modes accepted solutions",
            "新闻资讯": "latest primary sources publication date",
            "中文问答": "中文 实际经验 争议",
            "中文搜索": "中文 官方资料 实践",
            "中文视频": "中文 教程 实测",
            "社区讨论": "user feedback counterexamples limitations",
            "百科知识": "definition history standards primary sources",
        }.get(category, "primary sources evidence comparison")
        candidates = [
            question.strip(),
            f"{question.strip()} {focus}",
            f"{question.strip()} counterexamples unresolved questions {guidance}",
        ]
        queries: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = " ".join(candidate.split()).strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                queries.append(normalized[:500])
            if len(queries) >= rounds:
                break
        return queries

    def _build_expert_report_prompt(self, question, expert, findings, queries) -> str:
        name, _engine, category, guidance = expert
        return (
            f"你是 Conclave 的{name}（{category}）。问题：{question}\n职责：{guidance}\n"
            f"已执行查询：{' | '.join(queries)}\n\n"
            f"证据：\n{self._format_evidence(findings, limit=10)}\n\n"
            "写一份可供主席直接使用的独立专家报告，最多 900 个中文字符。必须包含："
            "结论、关键证据及原样 URL、冲突/局限、置信度。只依据给定证据；没有证据时明确说明。"
        )

    def _build_expert_discussion_prompt(
        self,
        question,
        expert,
        expert_reports: dict[str, str],
        prior_contributions: list[str],
    ) -> str:
        name, _engine, category, guidance = expert
        reports = "\n\n".join(
            f"## {report_name}\n{report}"
            for report_name, report in expert_reports.items()
        )
        prior = "\n\n".join(prior_contributions) or "(你是第一位发言者)"
        return (
            f"你是{name}（{category}）。研究问题：{question}\n职责：{guidance}\n\n"
            f"所有专家初始报告：\n{reports}\n\n此前的交叉讨论：\n{prior}\n\n"
            "基于全部初始报告并回应此前发言，补充遗漏、指出冲突、反驳薄弱推论或修正自己的结论。"
            "最多 600 个中文字符；不要复述整份报告，不得虚构来源。"
        )

    def _fallback_expert_report(
        self,
        name: str,
        findings: list[dict],
        *,
        error: str = "",
    ) -> str:
        reason = f"专家推理不可用：{error}" if error else "专家推理不可用，保留检索证据"
        return (
            f"## {name}（降级报告）\n{reason}\n\n"
            f"{self._format_evidence(findings, limit=6)}"
        )

    def _ensure_report_evidence(self, report: str, findings: list[dict]) -> str:
        """Keep the chairperson input auditable if an expert omits its URLs."""
        source_urls = [
            str(item.get("url") or "").strip()
            for item in findings
            if str(item.get("url") or "").strip()
        ]
        if not source_urls or any(url in report for url in source_urls):
            return report
        return (
            f"{report.rstrip()}\n\n### 可核验来源（运行时补充）\n"
            f"{self._format_evidence(findings, limit=6)}"
        )

    def _build_synthesis_prompt(
        self, question: str, intent: str,
        findings: dict[str, list[dict]], discussion: str,
        expert_reports: dict[str, str] | None = None,
    ) -> str:
        parts = [
            f"# 最终报告\n\n问题：{question}\n意图：{INTENT_ROUTES.get(intent, ('全面', []))[0]}\n\n",
            "## 各专家独立报告\n",
        ]
        reports = expert_reports or {}
        if reports:
            for name in findings:
                report = reports.get(name) or "(该专家报告缺失)"
                parts.append(f"\n### {name}\n{report}\n")
        else:
            # Compatibility for direct prompt construction: turn raw evidence
            # into report-shaped inputs before the chairperson sees it.
            parts.append("\n## 去重后的来源证据（兼容降级报告）\n")
        seen_urls: set[str] = set()
        synthesis_source_budget = 12
        for name, results in (findings.items() if not reports else ()):
            unique_results = []
            for item in results:
                if len(seen_urls) >= synthesis_source_budget:
                    break
                key = str(item.get("url") or item.get("title") or "").strip().casefold()
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                unique_results.append(item)
                if len(unique_results) >= 2:
                    break
            sources = "\n".join(
                (
                    f"- {r.get('title') or '(无标题)'}\n"
                    f"  URL: {r.get('url') or '(无 URL)'}\n"
                    f"  证据摘要: {str(r.get('snippet') or '(无摘要)')[:500]}"
                )
                for r in unique_results
            ) if unique_results else "  (本组来源与前面重复或无结果)"
            parts.append(f"\n### {name}\n{sources}\n")
        parts.append(f"\n## 交叉讨论记录\n{discussion}\n")
        parts.append("\n## 报告要求\n")
        parts.append("不要按专家逐组复述来源。你是主席，必须做跨来源判断和收束。\n")
        parts.append("全文控制在 1800 个中文字符以内；关键证据保留 4-8 条，不要罗列所有来源。\n")
        parts.append("宁可少写但完整收束，不要生成到一半被截断；URL 必须逐字复制，不要缩写或补写。\n")
        parts.append("输出必须严格使用以下结构：\n")
        parts.append("# 主席总结陈词\n")
        parts.append("## 核心结论\n")
        parts.append("先用一段可独立阅读的总结直接回答问题。\n")
        parts.append("## 关键证据\n")
        parts.append("按结论组织并引用对应 URL，不超过 8 条。\n")
        parts.append("## 分歧与不确定性\n")
        parts.append("区分已验证事实、来源观点与推测。\n")
        parts.append("## 建议或下一步\n")
        parts.append("仅在问题需要时给出；不需要也保留标题并说明无。\n")
        parts.append("不要虚构来源中没有的信息。用中文 Markdown 输出。\n")
        return "\n".join(parts)

    def _fallback_synthesis(
        self,
        question: str,
        findings: dict[str, list[dict]],
        expert_reports: dict[str, str] | None = None,
    ) -> str:
        if expert_reports:
            reports = "\n\n".join(
                f"### {name}\n{report}"
                for name, report in expert_reports.items()
            )
            return (
                "# 主席总结陈词（降级）\n\n"
                "主席模型没有返回可见总结。以下是完整专家报告，未进行额外推断。\n\n"
                f"## 研究问题\n{question}\n\n## 专家报告\n{reports}"
            )
        parts = [
            "# 主席总结陈词（降级）\n\n",
            "主席模型没有返回可见总结。以下仅保留已检索证据，不将来源列表冒充主席结论。\n",
            f"\n## 研究问题\n{question}\n",
            "\n## 去重后的来源证据\n",
        ]
        seen_urls: set[str] = set()
        for name, results in findings.items():
            selected = []
            for r in results[:5]:
                key = str(r.get("url") or r.get("title") or "").strip().casefold()
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                selected.append(r)
            if not selected:
                continue
            parts.append(f"\n### {name}\n")
            for r in selected:
                parts.append(
                    f"- [{r.get('title') or '(无标题)'}]({r.get('url') or ''})："
                    f"{str(r.get('snippet') or '')[:300]}\n"
                )
        return "\n".join(parts)
