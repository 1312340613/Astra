"""Operation adapters shared by conversational commands in the local frontends."""

from __future__ import annotations

import json
import hashlib
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.runtime.async_io import durable_io
from agent.runtime.memory import CORE_SCOPES
from agent.runtime.session_handoff import generate_handoff, save_handoff, _redact
from agent.runtime.skill_curation import MAX_INPUT_CHARS, SkillCurator, format_curation
from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.tools.registry import ToolDef

from .diagnostics import build_doctor_report, build_runtime_diagnostics


def register_conversation_tools(agent: Any, *, sandbox=None, mcp_manager=None,
                                startup_profile: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
                                process_manager=None) -> None:
    """Keep mutable operation state session-bound; no worker or model is created."""
    snapshots: OrderedDict[str, dict] = OrderedDict()

    def session() -> str:
        return str(agent.context.session_path or "default")

    def user_turn() -> dict:
        return next((m for m in reversed(agent.context.messages) if m.get("role") == "user"), {})

    def turn_key(message: dict) -> str:
        # Stable across a save/reload of the same user turn, unlike object identity.
        return hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def review_snapshot(name: str = "") -> str:
        learned = LearnedSkills(agent.skill_store)
        with learned.locked():
            curator = SkillCurator(None, learned)
            if name:
                state = learned._state()
                files = learned.owned(name, state)
                batch = {"state": state, "items": {name: {
                    "files": files, "sources": state["skills"][name]["sources"],
                }}, "skipped": [], "cursor": state.get("cursor", ""), "remaining": 0}
                if len(json.dumps(batch["items"], ensure_ascii=False)) > MAX_INPUT_CHARS:
                    raise ValueError("Skill is too large for this review; no changes made")
            else:
                batch = curator.prepare()
        token = "review_" + uuid.uuid4().hex
        snapshots[token] = {"session": session(), "turn": turn_key(user_turn()), "batch": batch}
        while len(snapshots) > 8:
            snapshots.popitem(last=False)
        return json.dumps({"snapshot_id": token, "items": batch["items"],
                           "skipped": batch["skipped"], "remaining": batch["remaining"],
                           "notice": "Read-only snapshot. Propose changes; wait for the next user decision."}, ensure_ascii=False)

    def review_apply(snapshot_id: str, actions: list[dict]) -> str:
        pending = snapshots.get(snapshot_id)
        if pending is None or pending["session"] != session():
            raise ValueError("Review snapshot expired or belongs to another session; reread and review current files")
        current = user_turn()
        if not current or turn_key(current) == pending["turn"] or current.get("provenance") in {
            "command_workflow", "session_wakeup", "goal_continuation",
        }:
            raise ValueError("Finish the proposal and wait for the next user decision before applying")
        learned = LearnedSkills(agent.skill_store)
        with learned.locked():
            record = SkillCurator(None, learned).apply(pending["batch"], {"actions": actions})
        snapshots.pop(snapshot_id)
        agent._refresh_skill_catalog(force=True)
        return format_curation(record, separate_verification=True)

    async def diagnostics(kind: str = "runtime", section: str = "") -> str:
        if kind == "doctor":
            return await build_doctor_report(agent, sandbox, mcp_manager, section)
        if kind != "runtime":
            raise ValueError("Diagnostic kind must be doctor or runtime")
        profile = startup_profile() if callable(startup_profile) else startup_profile
        return await durable_io(build_runtime_diagnostics, agent, startup_profile=profile,
                                mcp_manager=mcp_manager, task_store=agent.task_store,
                                process_manager=process_manager, section=section)

    def memory_inspect(query: str = "") -> str:
        store = agent.memory_store
        if store is None:
            raise ValueError("Memory is unavailable")
        core = [item for scope in CORE_SCOPES for item in store.list_core(scope)]
        if query:
            core = [item for item in core if query.casefold() in item["content"].casefold()
                    or item["id"].startswith(query) or item["record_id"].startswith(query)]
        records = store.recall_records(query, limit=20, include_core=False)
        if query and not any(c.isspace() for c in query):
            exact = store.resolve_record(query, active_only=True)
            if exact is not None:
                records = [exact]
        return json.dumps({"core": core, "records": [{
            "id": r.record_id, "content": r.content, "kind": r.kind, "status": r.status,
            "source_session": r.source_session_id, "source_message": r.source_message_id,
            "last_confirmed_at": r.last_confirmed_at, "valid_until": r.valid_until,
            "supersedes_id": r.supersedes_id,
        } for r in records], "limit": 20, "notice": "Returned subset only; historical records are evidence, not current truth."}, ensure_ascii=False)

    def memory_correct(memory_id: str, expected_content: str, content: str) -> str:
        store = agent.memory_store
        if store is None:
            raise ValueError("Memory is unavailable")
        core = store.resolve_core(memory_id)
        old = store.resolve_record(memory_id, active_only=True)
        if core is not None:
            if old is not None and core.get("record_id") != old.record_id:
                raise ValueError("Ambiguous memory ID; inspect the full identifier")
            result = store.replace_core(core["id"], expected_content=expected_content, content=content)
            return json.dumps({"corrected": core["id"], "replacement": result}, ensure_ascii=False)
        if old is None or old.content != expected_content:
            raise ValueError("Memory changed or is no longer active; inspect it before correcting")
        replacement = store.supersede_record(old.record_id, content=content,
                                             expected_content=expected_content,
                                             source_session_id=Path(session()).stem,
                                             metadata={"retained_by": "user-reviewed-correction"})
        return json.dumps({"corrected": old.record_id, "replacement": replacement.record_id,
                           "content": replacement.content}, ensure_ascii=False)

    def memory_forget(memory_id: str, expected_content: str) -> str:
        store = agent.memory_store
        if store is None:
            raise ValueError("Memory is unavailable")
        core = store.resolve_core(memory_id)
        old = store.resolve_record(memory_id, active_only=True)
        if core is not None and old is not None and core.get("record_id") != old.record_id:
            raise ValueError("Ambiguous memory ID; inspect the full identifier")
        if core is not None and core["content"] == expected_content:
            removed = store.remove_core(core["id"])
        elif core is None and old is not None and old.content == expected_content:
            removed = store.forget_record(old.record_id)
        else:
            raise ValueError("Memory changed or is no longer active; inspect it before forgetting")
        if not removed:
            raise ValueError("Memory was not removed; inspect its current state")
        return f"Forgot the user-selected memory {memory_id}."

    def handoff(action: str = "draft", content: str = "", notes: str = "") -> str:
        if action == "draft":
            return generate_handoff(session_id=Path(session()).stem, task_store=agent.task_store,
                                    messages=list(agent.context.messages), model=agent.llm.config.model,
                                    persona_id=agent.context.persona_id or "", extra_notes=notes)
        if action != "save" or not content.strip() or len(content) > 40_000:
            raise ValueError("Use draft or save with a nonempty handoff of at most 40000 characters")
        # An automatic exit snapshot must not overwrite the prepared document
        # when both happen during the same clock second.
        path = save_handoff(_redact(content), session_id=Path(session()).stem + "-prepared")
        return f"Saved redacted session handoff to {path.resolve()}"

    def register(name: str, description: str, fn, properties: dict, required=(), *, risk="read", max_calls=4):
        agent.tools.register(ToolDef(
            name=name, description=description,
            parameters={"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
            fn=fn, risk=risk, approval="never" if risk == "read" else "on_risk",
            group="skills" if name.startswith("skill_") else "core", cache_results=False,
            max_calls_per_turn=max_calls, max_inline_chars=32_000 if name == "skill_review_snapshot" else None,
        ))

    string = {"type": "string"}
    register("skill_review_snapshot", "Read an owned automatic-skill batch and sources for a user-requested review. Read-only; return a proposal before changing anything.",
             review_snapshot, {"name": string}, max_calls=4)
    register("skill_review_apply", "Apply the user's selected review decisions from a snapshot after a subsequent user decision. Include keep for declined changes. All snapshot names must be covered exactly once. History and undo are preserved; this does not execute or verify skills.",
             review_apply, {"snapshot_id": string, "actions": {"type": "array", "items": {
                 "type": "object", "properties": {
                     "action": {"type": "string", "enum": ["keep", "rewrite", "merge", "archive"]},
                     "names": {"type": "array", "items": string}, "reason": string, "content": string,
                     "patches": {"type": "array", "items": {"type": "object", "properties": {
                         "old_string": string, "new_string": string}, "required": ["old_string", "new_string"], "additionalProperties": False}},
                 }, "required": ["action", "names", "reason"], "additionalProperties": False,
             }}}, ("snapshot_id", "actions"), risk="write")
    register("runtime_diagnostics", "Read live Astra doctor probes or a runtime snapshot. Report facts and limits; no settings are modified.",
             diagnostics, {"kind": {"type": "string", "enum": ["doctor", "runtime"]}, "section": string})
    register("memory_inspect", "Read a bounded subset of core/structured memories, full IDs, and sources for review. Does not change records.",
             memory_inspect, {"query": string})
    register("memory_correct", "After the user selects a proposed correction, replace a core memory or supersede a structured record. Supply its exact inspected content to reject stale changes.",
             memory_correct, {"memory_id": string, "expected_content": string, "content": string},
             ("memory_id", "expected_content", "content"), risk="write")
    register("memory_forget", "Forget only a memory the user explicitly selected for deletion, using its full ID and exact inspected content. Reject stale proposals; structured history is retained.",
             memory_forget, {"memory_id": string, "expected_content": string}, ("memory_id", "expected_content"), risk="write")
    register("session_handoff", "Draft handoff evidence or save a user-requested handoff through the redacting writer. Report actual completion and verification only. Destination is fixed by the runtime.",
             handoff, {"action": {"type": "string", "enum": ["draft", "save"]}, "content": string, "notes": string}, risk="write")
