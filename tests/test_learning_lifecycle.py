"""Offline end-to-end contracts for candidate learning and real tool receipts."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli.legacy_learning_commands import execute_learning_command
from agent.core.msg import ContentBlock, Msg
from agent.runtime.learning import LearningReviewer, LearningStore, format_review_outcome
from agent.runtime.learning_evidence import check_result, normalize_checks
from agent.runtime.learning_lifecycle import LearningLifecycle
from agent.runtime.learning_queue import candidate_readiness, format_learning_queue, learning_reminder
from agent.runtime.memory import MemoryStore
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.learning import register_learning_tools
from agent.runtime.tools.policy import PolicyRule
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.tools.skills import register_skill_tools


SKILL = "# Configuration verifier\n\n1. Read the requested config.\n2. Verify its value before reporting completion."
CONTRACT = {"claim_type": "procedure", "trigger": "Checking a changed project configuration",
            "benefit": "Avoid claiming that a request was already completed",
            "verification_plan": "Read the actual configuration on a later task and assert the expected value."}


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("patch", [None, {}, {"benefit": "Catch missing configuration values"}])
def test_revision_preserves_omitted_contract_fields(rig, background, patch):
    item = propose(rig)
    trial(rig, item)
    changed = skill_proposal(SKILL + "\n3. Check for missing values.")
    if background:
        rig.llm.proposal = {**changed, "candidate_id": item["id"]}
        if patch is not None:
            rig.llm.proposal["learning"] = patch
        revised = asyncio.run(rig.reviewer.review_outcome(
            [{"role": "user", "content": "Check absent values as well"}], "later-session",
        ))["proposals"][0]
    else:
        kwargs = {"contract": patch} if patch is not None else {}
        revised = invoke(rig, "revise", candidate_id=item["id"], proposal=changed, **kwargs)
    assert revised["learning"]["contract"] == {**CONTRACT, **(patch or {})}
    assert revised["revision"] == 2 and revised["trials"] == []
    # Preserving the plan must not preserve the old validation proof.
    with pytest.raises(ValueError, match="validation"):
        rig.lifecycle.activate(item["id"])
    _, finished, _ = trial(rig, revised, request="independent-later-request", case="new missing value")
    assert finished["status"] == "passed"


@pytest.mark.parametrize("field,value", [
    ("trigger", ""), ("benefit", "   "), ("verification_plan", None),
    ("claim_type", ""), ("trigger", 123),
])
def test_invalid_contract_revision_is_atomic(rig, field, value):
    item = propose(rig)
    trial(rig, item)
    before = rig.lifecycle.show(item["id"])
    with pytest.raises(AssertionError, match="Invalid learning|claim_type|must be string"):
        invoke(rig, "revise", candidate_id=item["id"], proposal=skill_proposal(SKILL + "\n3. More checks."),
               contract={field: value})
    assert rig.lifecycle.show(item["id"]) == before


def test_replacement_preserves_contract_of_applied_candidate(rig):
    original = propose(rig)
    trial(rig, original)
    invoke(rig, "activate", candidate_id=original["id"])
    replacement = invoke(rig, "revise", candidate_id=original["id"],
                         proposal=skill_proposal(SKILL + "\n3. More checks."))
    assert replacement["learning"]["contract"] == CONTRACT
    assert replacement["learning"]["replaces"] == original["id"]
    assert replacement["status"] == "pending" and not replacement["trials"]


def test_queue_reports_real_count_and_distinguishes_next_actions(rig):
    item = propose(rig)
    assert candidate_readiness(rig.lifecycle, item["id"])["state"] == "awaiting_validation"
    trial(rig, item)
    assert candidate_readiness(rig.lifecycle, item["id"])["state"] == "ready"
    for n in range(65):
        rig.store.stage("legacy", "observation", {"content": f"legacy-{n}"}, "old evidence")
    output = format_learning_queue(rig.lifecycle)
    assert "Pending proposals: 66" in output and "Showing 50 of 66" in output
    assert "needs_review: 50" in output
    status, error = asyncio.run(execute_learning_command(
        rig.store, rig.reviewer, rig.memory, rig.skills, [], session_id="a", messages=[],
    ))
    assert not error and "Pending proposals: 66" in status
    assert rig.store.count("pending") == 66 and rig.store.count("applied") == 0


def test_queue_explains_incomplete_plan_and_observation_summary(rig):
    normalized = LearningReviewer._normalize(skill_proposal())
    item = rig.lifecycle.propose(normalized, "legacy-api")
    assert candidate_readiness(rig.lifecycle, item["id"])["state"] == "needs_details"
    rig.agent.context.messages = [{"role": "tool", "name": "read_fixture", "content": "enabled=true"}]
    raw = {"kind": "observation", "payload": {"content": "The whole application is correctly configured.",
           "evidence": "enabled=true", "evidence_role": "tool"}, "reason": "An overbroad summary"}
    fact = propose(rig, raw, {**CONTRACT, "claim_type": "fact"})
    state = candidate_readiness(rig.lifecycle, fact["id"])
    assert state["state"] == "needs_details" and "literal" in state["reason"]
    assert rig.store.get(fact["id"])["payload"]["content"] == raw["payload"]["content"]


def test_reminder_is_scoped_related_and_does_not_change_learning(rig):
    item = propose(rig)
    before = rig.lifecycle.show(item["id"])
    text = asyncio.run(learning_reminder(rig.lifecycle, "Checking changed project configuration"))
    assert item["id"] in text and "UNVERIFIED REFERENCE DATA" in text
    assert "# Configuration verifier" not in text
    for query in ("", "你好", "the with and", "Who wrote Hamlet?", "/learn pending"):
        assert asyncio.run(learning_reminder(rig.lifecycle, query)) == ""
    assert rig.lifecycle.show(item["id"]) == before
    assert not rig.llm.calls
    rig.store.set_mode("off")
    assert asyncio.run(learning_reminder(rig.lifecycle, "Checking changed project configuration")) == ""


def test_reminder_timeout_and_cancellation_leave_the_event_loop_responsive(rig, monkeypatch):
    import threading
    import agent.runtime.learning_queue as queue

    started, release = threading.Event(), threading.Event()
    def delayed(*_args):
        started.set()
        release.wait(timeout=2)
        return "late data"
    monkeypatch.setattr(queue, "_reminder", delayed)
    monkeypatch.setattr(queue, "REMINDER_TIMEOUT", 0.02)

    async def run():
        lookup = asyncio.create_task(queue.learning_reminder(rig.lifecycle, "changed project configuration"))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert started.is_set() and not release.is_set()
            assert await asyncio.wait_for(lookup, timeout=0.5) == ""
        finally:
            release.set()
    asyncio.run(run())
    started.clear()
    release.clear()
    async def cancel():
        lookup = asyncio.create_task(queue.learning_reminder(rig.lifecycle, "changed project configuration"))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert started.is_set()
            lookup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await lookup
        finally:
            release.set()
    asyncio.run(cancel())


@pytest.mark.parametrize("disabled", ["tools", "allowlist", "mode", "reminders"])
def test_react_does_not_inject_learning_into_disabled_modes(rig, monkeypatch, disabled):
    propose(rig)
    class Model:
        async def chat_stream(self, messages, tools, **kwargs):
            assert all("RELATED LEARNING CANDIDATES" not in str(m.get("content")) for m in messages)
            yield {"type": "done", "content": "Done", "usage": None}
    registry = ToolRegistry()
    agent = ReActAgent("agent", Model(), registry, skill_store=rig.skills, max_iterations=1)
    agent._learning_reviewer = rig.reviewer
    agent.context.set_session(str(rig.root / "isolated-session.json"))
    register_learning_tools(registry, rig.lifecycle, agent_getter=lambda: agent)
    if disabled == "tools":
        agent.tools_enabled = False
    elif disabled == "allowlist":
        agent.tool_allowlist = {"read_fixture"}
    elif disabled == "mode":
        rig.store.set_mode("off")
    else:
        monkeypatch.setenv("LEARNING_CANDIDATE_REMINDERS", "0")
    async def run():
        return [e async for e in agent._run_react_loop(Msg(content=[ContentBlock.text("Checking changed project configuration")]))]
    events = asyncio.run(run())
    assert not any(e.get("type") == "error" for e in events)
    assert not rig.llm.calls


def test_queue_keeps_manual_skill_changes_separate(rig):
    original = LearningReviewer._normalize(skill_proposal())["payload"]["content"]
    rig.skills.create("config-verifier", original)
    item = propose(rig, {"kind": "skill_patch", "payload": {"name": "config-verifier",
        "old_string": "Read the requested config.", "new_string": "Read and validate the requested config."}, "reason": "Clarify the step"})
    assert candidate_readiness(rig.lifecycle, item["id"])["state"] == "needs_review"
    trial(rig, item)
    assert candidate_readiness(rig.lifecycle, item["id"])["state"] == "needs_review"
    assert rig.skills.raw_file("config-verifier", "SKILL.md") == original


def test_queue_recognizes_inconclusive_and_stale_candidates(rig):
    item = propose(rig)
    rig.agent._context_index_request_id = "validation-incomplete"
    opened = invoke(rig, "trial", candidate_id=item["id"], case="new configuration absent",
                    checks=[{"tool": "read_fixture", "args": {"path": "missing.txt"}, "contains": "configured"}])
    invoke(rig, "finish", trial_id=opened["trial_id"])
    assert candidate_readiness(rig.lifecycle, item["id"])["state"] == "trial_incomplete"
    rig.skills.create("config-verifier", LearningReviewer._normalize(skill_proposal())["payload"]["content"])
    assert candidate_readiness(rig.lifecycle, item["id"])["state"] == "needs_revision"


def test_search_filters_scope_before_capping_candidates(rig):
    local = propose(rig)
    with rig.store._connection() as db:
        row = db.execute("SELECT * FROM learning_candidates WHERE proposal_id=?", (local["id"],)).fetchone()
        meta = json.loads(row["metadata_json"])
        meta["scope"]["workspace"] = "another-workspace"
        for n in range(301):
            pid = f"lr_{n:08x}"
            db.execute("INSERT INTO learning_proposals SELECT ?,session_id,kind,payload_json,reason,status,before_json,result_json,created_at,'2999' FROM learning_proposals WHERE id=?", (pid, local["id"]))
            db.execute("INSERT INTO learning_candidates VALUES (?,1,?,?)", (pid, str(n), json.dumps(meta)))
    reminder = asyncio.run(learning_reminder(rig.lifecycle, "Checking changed project configuration"))
    assert local["id"] in reminder
    assert "lr_00000000" not in reminder


class FakeLLM:
    def __init__(self, proposal=None):
        self.proposal = proposal
        self.calls = 0
        self.prompts = []

    async def chat(self, messages):
        self.calls += 1
        self.prompts.append(messages)
        return {"content": json.dumps({"summary": "Potential reusable configuration check",
                                       "proposals": [self.proposal] if self.proposal else []})}


def skill_proposal(content=SKILL):
    return {"kind": "skill_create", "payload": {"name": "config-verifier", "content": content},
            "reason": "Verify the actual config before claiming completion"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEARNING_REVIEW_MODE", "review")
    store = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    llm = FakeLLM()
    reviewer = LearningReviewer(llm, store, memory, skills)
    lifecycle = reviewer.lifecycle
    registry = ToolRegistry()
    agent = SimpleNamespace(context=SimpleNamespace(session_path=str(tmp_path / "session-a.json"), messages=[]),
                            _context_index_request_id="request-discovery")
    register_learning_tools(registry, lifecycle, agent_getter=lambda: agent)
    register_skill_tools(registry, skills, learning_getter=lambda: lifecycle, session_id=lambda: "session-a")
    registry.register(ToolDef(name="read_fixture", description="Read the test artifact", parameters={
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        fn=lambda path: Path(path).read_text(), risk="read", approval="never"))
    return SimpleNamespace(store=store, memory=memory, skills=skills, llm=llm,
                           lifecycle=lifecycle, registry=registry, agent=agent, reviewer=reviewer, root=tmp_path)


def invoke(rig, action, **kwargs):
    result = asyncio.run(rig.registry.execute("learning", {"action": action, **kwargs}))
    assert not result.get("error"), result
    return json.loads(result["output"])


def propose(rig, proposal=None, contract=None):
    return invoke(rig, "propose", proposal=proposal or skill_proposal(), contract=contract or CONTRACT)


def trial(rig, candidate, *, request="request-validation", case="held-out configuration alpha",
          value="configuration is valid", expected="configuration is valid", phase="validation", variant="candidate"):
    rig.agent._context_index_request_id = request
    fixture = rig.root / (case.replace(" ", "-") + ".txt")
    fixture.write_text(value)
    checks = [{"tool": "read_fixture", "args": {"path": str(fixture)}, "contains": expected}]
    opened = invoke(rig, "trial", candidate_id=candidate["id"], case=case, checks=checks, phase=phase, variant=variant)
    observed = asyncio.run(rig.registry.execute("read_fixture", {"path": str(fixture)}))
    assert not observed.get("error"), observed
    finished = invoke(rig, "finish", trial_id=opened["trial_id"])
    return opened, finished, checks


def test_model_can_learn_verify_activate_and_rollback_without_a_reviewer_call(rig):
    item = propose(rig)
    assert item["status"] == "pending"
    initial_skills = rig.skills.list()
    assert [entry["name"] for entry in initial_skills] == ["astra-core"]
    assert "config-verifier" not in rig.skills.catalog_prompt()
    assert not rig.memory.recall_records("configuration", kinds=("observation",))
    before = rig.memory.list_core("memory")
    opened, result, _ = trial(rig, item)
    assert opened["revision"] == 1 and "UNVERIFIED TRIAL" in opened["notice"]
    assert SKILL in opened["content"]
    assert result["status"] == "passed"
    applied = invoke(rig, "activate", candidate_id=item["id"])
    assert applied["status"] == "applied"
    assert "config-verifier" in rig.skills.catalog_prompt()
    assert applied["result"]["learning_verification"]["trial_ids"] == [opened["trial_id"]]
    assert rig.llm.calls == 0
    rolled = invoke(rig, "rollback", candidate_id=item["id"])
    assert rolled["status"] == "rolled_back" and rig.skills.list() == initial_skills
    assert rig.memory.list_core("memory") == before


@pytest.mark.parametrize("claim,evidence,role", [
    ("The renamed model was installed in all five registries and all tests passed.", "那就把先这个换成新名称吧", "user"),
    ("Related-session injection caused the agent to change its personality.", "context_index_built: injected=3", "tool"),
])
def test_quote_provenance_does_not_prove_completion_or_causality(rig, claim, evidence, role):
    rig.agent.context.messages = [{"role": role, "content": evidence}]
    raw = {"kind": "observation", "content": claim, "evidence": evidence, "evidence_role": role, "reason": "inference"}
    rig.llm.proposal = raw
    outcome = asyncio.run(rig.reviewer.review_outcome(rig.agent.context.messages, "session-a"))
    item = outcome["proposals"][0]
    assert item["status"] == "pending" and item["learning"]["contract"]["claim_type"] == "hypothesis"
    assert rig.memory.recall_records(claim, kinds=("observation",)) == []
    with pytest.raises(ValueError, match="validation"):
        rig.lifecycle.activate(item["id"])
    assert rig.store.apply_additive(item["id"], rig.memory, rig.skills)["status"] == "pending"
    rendered = format_review_outcome(outcome)
    assert claim not in rendered and "unverified" in rendered and "NOT applied" not in rendered


def test_literal_fact_has_narrow_proof_and_duplicates_do_not_inflate_confidence(rig):
    literal = "The configuration selects deepseek-flash."
    rig.agent.context.messages = [{"role": "tool", "name": "read_fixture", "content": literal}]
    raw = {"kind": "observation", "payload": {"content": literal, "evidence": literal, "evidence_role": "tool"}, "reason": "Observed config"}
    item = propose(rig, raw, {**CONTRACT, "claim_type": "fact"})
    trial(rig, item, value=literal, expected=literal)
    applied = invoke(rig, "activate", candidate_id=item["id"])
    records = rig.memory.recall_records("deepseek-flash", kinds=("observation",))
    assert len(records) == 1
    assert records[0].confidence == 0.8
    assert records[0].metadata["learning_verification"]["candidate_id"] == item["id"]
    assert records[0].metadata["evidence_count"] == 1
    assert records[0].metadata["ttl_days"] == 90
    duplicate = propose(rig, raw, {**CONTRACT, "claim_type": "fact"})
    assert duplicate["id"] == item["id"] and duplicate["status"] == "applied"
    assert rig.memory.recall_records("deepseek-flash", kinds=("observation",))[0].metadata["evidence_count"] == 1
    assert applied["result"]["record_id"] == records[0].record_id


def test_fact_cannot_expand_beyond_an_observed_literal(rig):
    literal = "injected=3"
    rig.agent.context.messages = [{"role": "tool", "content": literal}]
    item = propose(rig, {"kind": "observation", "payload": {
        "content": "Memory injection causes behavior changes.", "evidence": literal, "evidence_role": "tool"}, "reason": "Claim"},
        {**CONTRACT, "claim_type": "fact"})
    trial(rig, item, value=literal, expected=literal)
    with pytest.raises(ValueError, match="literal"):
        rig.lifecycle.activate(item["id"])


def test_same_generation_request_only_allows_training(rig):
    item = propose(rig)
    with pytest.raises(AssertionError, match="used for learning"):
        trial(rig, item, request="request-discovery")
    _, result, _ = trial(rig, item, request="request-discovery", phase="training")
    assert result["status"] == "passed"
    with pytest.raises(ValueError, match="held-out"):
        rig.lifecycle.activate(item["id"])


def test_training_input_cannot_be_renamed_as_validation(rig):
    item = propose(rig)
    _, _, checks = trial(rig, item, phase="training")
    rig.agent._context_index_request_id = "request-other"
    with pytest.raises(AssertionError, match="used for learning"):
        invoke(rig, "trial", candidate_id=item["id"], case="new misleading label", checks=checks)


def test_observed_generation_tool_input_is_not_held_out(rig):
    args = {"path": str(rig.root / "original.txt")}
    rig.agent.context.messages = [{"role": "assistant", "tool_calls": [{"id": "first", "function": {
        "name": "read_fixture", "arguments": json.dumps(args)}}]}, {"role": "tool", "tool_call_id": "first", "content": "valid"}]
    item = propose(rig)
    rig.agent._context_index_request_id = "new-request"
    with pytest.raises(AssertionError, match="used for learning"):
        invoke(rig, "trial", candidate_id=item["id"], case="same file new label", checks=[{"tool": "read_fixture", "args": args, "contains": "valid"}])


def test_failed_candidate_needs_revision_and_old_trials_do_not_validate_new_revision(rig):
    item = propose(rig)
    _, outcome, old_checks = trial(rig, item, value="invalid")
    assert outcome["status"] == "failed"
    trial(rig, item, request="request-other", case="unrelated successful sample")
    with pytest.raises(ValueError, match="counterexample"):
        rig.lifecycle.activate(item["id"])
    revised = invoke(rig, "revise", candidate_id=item["id"], proposal=skill_proposal(SKILL + "\n3. Handle missing values."), contract=CONTRACT)
    assert revised["revision"] == 2 and revised["id"] == item["id"] and revised["trials"] == []
    assert "held-out" in rig.lifecycle.activation_issue(item["id"])
    rig.agent._context_index_request_id = "new-independent-request"
    with pytest.raises(AssertionError, match="used for learning"):
        invoke(rig, "trial", candidate_id=item["id"], case="rename failed sample", checks=old_checks)
    trial(rig, revised, request="request-independent", case="new config absent field")
    assert invoke(rig, "activate", candidate_id=item["id"])["revision"] == 2
    with rig.store._connection() as db:
        assert db.execute("SELECT count(*) FROM learning_versions WHERE proposal_id=?", (item["id"],)).fetchone()[0] == 2


def test_model_cannot_forge_a_receipt_or_reuse_a_previous_tool_result(rig):
    item = propose(rig)
    fixture = rig.root / "earlier.txt"
    fixture.write_text("valid configuration")
    asyncio.run(rig.registry.execute("read_fixture", {"path": str(fixture)}))
    rig.agent._context_index_request_id = "validation-request"
    opened = invoke(rig, "trial", candidate_id=item["id"], case="file without fresh read", checks=[{
        "tool": "read_fixture", "args": {"path": str(fixture)}, "contains": "valid configuration"}])
    forged = asyncio.run(rig.registry.execute("learning", {"action": "finish", "trial_id": opened["trial_id"], "status": "passed"}))
    assert forged["error"]
    assert invoke(rig, "finish", trial_id=opened["trial_id"])["status"] == "inconclusive"
    assert "held-out" in rig.lifecycle.activation_issue(item["id"])


def test_wrong_arguments_and_cached_results_never_validate(rig):
    item = propose(rig)
    rig.agent._context_index_request_id = "validation-request"
    checks = [{"tool": "read_fixture", "args": {"path": "intended.json"}, "contains": "PASS"}]
    opened = invoke(rig, "trial", candidate_id=item["id"], case="exact argument binding", checks=checks)
    rig.lifecycle.observe("session-a", "validation-request", "read_fixture", {"path": "different.json"}, {"output": "PASS"})
    rig.lifecycle.observe("session-a", "validation-request", "read_fixture", {"path": "intended.json"}, {"output": "PASS", "cached": True})
    assert invoke(rig, "finish", trial_id=opened["trial_id"])["status"] == "inconclusive"


@pytest.mark.parametrize("name", ["context_open", "context_inspect", "session_search", "activity_read", "memory", "skill_view", "learning", "run_code"])
def test_retrieval_or_learning_outputs_cannot_corroborate_themselves(name):
    with pytest.raises(ValueError, match="non-retrieval"):
        normalize_checks([{"tool": name, "args": {"query": "anything"}, "contains": "PASS"}])


@pytest.mark.parametrize("output,extras", [
    ('{"status":"running","process_id":"p1"}', {}),
    ('{"exit_code":1,"output":"PASS"}', {}),
    ('{"running":true,"output":"PASS"}', {}),
    ('PASS\n[exit code: 2]', {}),
    ('PASS', {"error": "permission denied"}),
    ('PASS', {"verified": False}),
    ('PASS', {"persistent_cache": True}),
])
def test_process_start_failure_denial_and_replay_are_not_success(output, extras):
    check = {"tool": "execute_shell", "args": {"command": "run-check"}, "contains": "PASS"}
    assert check_result(check, {"output": output, **extras})["status"] == "inconclusive"


def test_json_assertions_are_typed_and_output_is_not_retained():
    check = {"tool": "read_fixture", "args": {"path": "config.json"}, "json_path": ["enabled"], "equals": True}
    assert check_result(check, {"output": '{"enabled":1}'})["status"] == "failed"
    receipt = check_result(check, {"output": '{"enabled":true,"private":"unrelated contents"}'})
    assert receipt["status"] == "passed" and "unrelated contents" not in json.dumps(receipt)
    assert check_result(check, {"output": '{}'})["status"] == "failed"


def test_trial_budget_request_end_and_cross_request_finish(rig):
    item = propose(rig)
    for index in range(2):
        trial(rig, item, case=f"budget case {index}")
    with pytest.raises(AssertionError, match="two trials"):
        trial(rig, item, case="budget case three")
    rig.agent._context_index_request_id = "unfinished-request"
    opened = invoke(rig, "trial", candidate_id=item["id"], case="interrupted example", checks=[{
        "tool": "read_fixture", "args": {"path": "config.json"}, "contains": "valid"}])
    rig.agent._context_index_request_id = "wrong-request"
    with pytest.raises(AssertionError, match="originating"):
        invoke(rig, "finish", trial_id=opened["trial_id"])
    rig.lifecycle.end_request("session-a", "unfinished-request")
    assert rig.lifecycle.show(item["id"])["trials"][0]["status"] == "inconclusive"


def test_abandoned_trial_expires_after_restart_without_claiming_success(rig):
    item = propose(rig)
    rig.agent._context_index_request_id = "crashed-request"
    opened = invoke(rig, "trial", candidate_id=item["id"], case="restart example", phase="training", checks=[{
        "tool": "read_fixture", "args": {"path": "config.json"}, "contains": "valid"}])
    with rig.store._connection() as db:
        row = db.execute("SELECT trial_json FROM learning_trials WHERE id=?", (opened["trial_id"],)).fetchone()
        body = json.loads(row[0])
        body["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        db.execute("UPDATE learning_trials SET trial_json=? WHERE id=?", (json.dumps(body), opened["trial_id"]))
    reopened = LearningLifecycle(LearningStore(rig.store.path), rig.memory, rig.skills)
    assert reopened.show(item["id"])["trials"][0]["status"] == "inconclusive"


def test_scope_is_enforced_at_trial_and_activation_and_search(rig):
    item = propose(rig)
    foreign = {**item["learning"]["scope"], "platform": "Windows" if item["learning"]["scope"]["platform"] != "Windows" else "Darwin"}
    assert rig.lifecycle.search(scope=foreign) == []
    with pytest.raises(ValueError, match="workspace and platform"):
        rig.lifecycle.start_trial(item["id"], session_id="s", request_id="r", case="other platform case", checks=[{
            "tool": "read_fixture", "args": {"path": "config.json"}, "contains": "valid"}], scope=foreign)
    trial(rig, item)
    assert "workspace/platform" in rig.lifecycle.activation_issue(item["id"], scope=foreign)


def test_stale_target_and_intervening_edits_are_never_overwritten(rig):
    item = propose(rig)
    trial(rig, item)
    rig.skills.create("config-verifier", "---\nname: config-verifier\ndescription: Manual edit\n---\n" + SKILL)
    with pytest.raises(ValueError, match="Target changed"):
        rig.lifecycle.activate(item["id"])
    rig.skills.restore_file("config-verifier", "SKILL.md", None)
    invoke(rig, "activate", candidate_id=item["id"])
    rig.skills.patch("config-verifier", "requested config", "user's newer config")
    with pytest.raises(ValueError, match="overwrite"):
        rig.lifecycle.rollback(item["id"])
    assert "newer config" in rig.skills.view("config-verifier")


def test_verified_skill_revision_keeps_old_version_until_activation_and_can_restore_it(rig):
    original = propose(rig)
    trial(rig, original)
    invoke(rig, "activate", candidate_id=original["id"])
    old_content = rig.skills.view("config-verifier")
    raw = {"kind": "skill_patch", "payload": {"name": "config-verifier", "old_string": "requested config",
           "new_string": "requested config and version", "file_path": "SKILL.md"}, "reason": "Verify the version too"}
    replacement = invoke(rig, "revise", candidate_id=original["id"], proposal=raw, contract=CONTRACT)
    assert replacement["id"] != original["id"]
    assert replacement["learning"]["replaces"] == original["id"]
    assert rig.skills.view("config-verifier") == old_content
    opened, _, _ = trial(rig, replacement, request="next-request", case="independent version field")
    assert "config and version" in opened["content"]
    invoke(rig, "activate", candidate_id=replacement["id"])
    assert "config and version" in rig.skills.view("config-verifier")
    invoke(rig, "rollback", candidate_id=replacement["id"])
    assert rig.skills.view("config-verifier") == old_content


def test_manual_skill_patch_remains_reviewable_even_after_successful_trial(rig):
    rig.skills.create("config-verifier", "---\nname: config-verifier\ndescription: Manual skill\n---\n" + SKILL)
    raw = {"kind": "skill_patch", "payload": {"name": "config-verifier", "old_string": "requested config",
           "new_string": "requested config and version", "file_path": "SKILL.md"}, "reason": "Verify version"}
    item = propose(rig, raw)
    trial(rig, item)
    with pytest.raises(ValueError, match="manual"):
        rig.lifecycle.activate(item["id"])
    output, error = asyncio.run(execute_learning_command(rig.store, rig.reviewer, rig.memory, rig.skills,
                                ["approve", item["id"]], session_id="session-a", messages=[]))
    assert not error and "Applied" in output


def test_skill_manage_saves_directly_without_legacy_candidate(rig):
    text = "---\nname: config-verifier\ndescription: Check configuration with evidence\n---\n" + SKILL
    result = asyncio.run(rig.registry.execute("skill_manage", {"action": "create", "origin": "auto", "name": "config-verifier", "content": text}))
    assert not result.get("error"), result
    assert json.loads(result["output"])["status"] == "saved"
    assert "config-verifier" in [entry["name"] for entry in rig.skills.list()]
    assert rig.store.count() == 0


def test_learning_never_bypasses_existing_tool_denials(rig):
    item = propose(rig)
    rig.registry.policy.add_rule(PolicyRule("read_fixture", "deny"))
    rig.agent._context_index_request_id = "validation-request"
    opened = invoke(rig, "trial", candidate_id=item["id"], case="denied file probe", checks=[{
        "tool": "read_fixture", "args": {"path": "config.json"}, "contains": "valid"}])
    result = asyncio.run(rig.registry.execute("read_fixture", {"path": "config.json"}))
    assert result["error"]
    assert invoke(rig, "finish", trial_id=opened["trial_id"])["status"] == "inconclusive"


def test_review_uses_only_new_original_evidence_and_failure_does_not_advance(rig):
    original = [{"role": "user", "content": "Please check this config"}]
    asyncio.run(rig.reviewer.review_outcome(original, "session-a"))
    repeated = asyncio.run(rig.reviewer.review_outcome(original, "session-a"))
    assert repeated["skipped"] == "no-new-evidence" and rig.llm.calls == 1
    recalled = [*original, {"role": "tool", "name": "context_open", "content": "An old remembered config"}]
    assert asyncio.run(rig.reviewer.review_outcome(recalled, "session-a"))["skipped"] == "no-new-evidence"
    new = [*recalled, {"role": "user", "content": "New independent fact"}]

    class Malformed:
        async def chat(self, messages):
            return {"content": "incomplete JSON"}

    rig.reviewer.llm = Malformed()
    with pytest.raises(ValueError, match="unprocessed"):
        asyncio.run(rig.reviewer.review_outcome(new, "session-a"))
    rig.reviewer.llm = rig.llm
    asyncio.run(rig.reviewer.review_outcome(new, "session-a"))
    prompt = rig.llm.prompts[-1][1]["content"].split("Conversation:")[1]
    assert "New independent fact" in prompt and "Please check this config" not in prompt
    assert "An old remembered config" not in prompt


def test_mode_off_prevents_autonomous_learning_but_preserves_user_inspection(rig):
    item = propose(rig)
    rig.store.set_mode("off")
    assert invoke(rig, "show", candidate_id=item["id"])["id"] == item["id"]
    denied = asyncio.run(rig.registry.execute("learning", {"action": "discard", "candidate_id": item["id"]}))
    assert "Learning is off" in denied["error"]
    assert asyncio.run(rig.reviewer.review_outcome([{"role": "user", "content": "fact"}], "session-a"))["proposals"] == []
    assert rig.llm.calls == 0


def test_improvement_requires_two_paired_independent_cases(rig):
    item = propose(rig, contract={**CONTRACT, "claim_type": "improvement"})
    for case in ("missing registry alpha", "missing registry beta"):
        trial(rig, item, request="baseline-" + case, case=case, value="wrong result", variant="baseline")
        trial(rig, item, request="candidate-" + case, case=case)
        if "alpha" in case:
            assert "two distinct" in rig.lifecycle.activation_issue(item["id"])
    assert invoke(rig, "activate", candidate_id=item["id"])["status"] == "applied"


def test_same_evidence_is_one_candidate_across_sessions_and_search_is_scoped(rig):
    first = propose(rig)
    rig.agent.context.session_path = str(rig.root / "session-b.json")
    second = propose(rig)
    assert first["id"] == second["id"]
    result = asyncio.run(rig.registry.execute("learning_search", {"query": "configuration"}))
    assert [c["id"] for c in json.loads(result["output"])["candidates"]] == [first["id"]]
    assert len(rig.store.list("pending")) == 1


def test_learning_search_ignores_category_matches_and_keeps_specific_tool_identifiers(rig):
    def candidate(name, content, trigger):
        return propose(rig, proposal={'kind':'skill_create','payload':{
            'name':name,'category':'operations','content':content},'reason':trigger},
            contract={'claim_type':'procedure','trigger':trigger,'benefit':'Avoid repeating diagnosis',
                      'verification_plan':'Verify the requested observable state in a later relevant task.'})
    irrelevant=candidate('mps-float-workaround','# MPS precision\n\n1. Use float32 when float64 is unsupported.\n2. Verify an image is produced.',
                         'MPS float64 unsupported on this device')
    relevant=candidate('choice-verifier','# Choice verification\n\n1. Use browser_check for requested states.\n2. Inspect its checked receipt.',
                       'A browser form requires checked-state changes')
    found=rig.lifecycle.search('browser_check 不支持 Unsupported operation 表单勾选 替代 click CSS selector')
    assert [item['id'] for item in found]==[relevant['id']]
    assert 'browser_check' in found[0]['matched_terms']
    assert irrelevant['id'] in {item['id'] for item in rig.lifecycle.search()}
    assert rig.lifecycle.search('operation')==[]
    assert rig.lifecycle.search('float3')==[]  # no prefix hit inside float32


def test_learning_payload_reports_nested_type_errors_without_creating_a_candidate(rig):
    malformed={'kind':'skill_create','payload':{'name':42,'content':SKILL},'reason':'Verify configuration'}
    result=asyncio.run(rig.registry.execute('learning',{'action':'propose','proposal':malformed,'contract':CONTRACT}))
    assert result['code']=='invalid_arguments'
    assert 'proposal.payload.name' in result['error']
    assert rig.store.list('pending')==[]
    assert propose(rig)['status']=='pending'


def test_automatic_review_refines_a_candidate_and_reports_no_fake_new_write(rig):
    item = propose(rig)
    raw = {**skill_proposal(SKILL + "\n3. Check absent values."), "candidate_id": item["id"], "learning": CONTRACT}
    rig.llm.proposal = raw
    messages = [{"role": "user", "content": "Missing configuration values also need handling"}]
    outcome = asyncio.run(rig.reviewer.review_outcome(messages, "session-b"))
    assert outcome["proposals"][0]["id"] == item["id"]
    assert outcome["proposals"][0]["revision"] == 2
    repeat = asyncio.run(rig.reviewer.review_outcome(messages, "session-c"))
    assert repeat["proposals"] == [] and repeat["unchanged"][0]["id"] == item["id"]
    assert "Automatically saved" not in format_review_outcome(repeat)


def test_validation_feedback_is_specific_to_version_and_deduplicated_input(rig):
    item = propose(rig)
    trial(rig, item)
    invoke(rig, "activate", candidate_id=item["id"])
    trial(rig, item, request="later-repeat")
    assert rig.lifecycle.search()[0]["validation"] == {"passed": 1, "failed": 0}
    trial(rig, item, request="later-counterexample", case="different actual input", value="wrong config")
    assert rig.lifecycle.search()[0]["validation"] == {"passed": 1, "failed": 1}
    # A counterexample is retained; it does not declare the whole method
    # permanently invalid or change every recalled memory's confidence.
    assert rig.store.get(item["id"])["status"] == "applied"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_native_python_exit_status_is_verified_not_just_stdout(rig, exit_code):
    from agent.runtime.tools.code import register_code_tools
    from agent.sandbox.local import LocalSandbox

    register_code_tools(rig.registry, LocalSandbox(timeout=10, workdir=str(rig.root)))
    item = propose(rig)
    rig.agent._context_index_request_id = "native-validation"
    args = {"code": f"print('validation passed')\nraise SystemExit({exit_code})", "foreground_yield_ms": 0}

    async def run():
        opened = await rig.registry.execute("learning", {"action": "trial", "candidate_id": item["id"],
            "case": "native python process", "checks": [{"tool": "execute_python", "args": args, "contains": "validation passed"}]})
        assert not opened.get("error"), opened
        trial_id = json.loads(opened["output"])["trial_id"]
        await rig.registry.execute("execute_python", args)
        return await rig.registry.execute("learning", {"action": "finish", "trial_id": trial_id})

    result = asyncio.run(run())
    assert not result.get("error"), result
    assert json.loads(result["output"])["status"] == ("passed" if exit_code == 0 else "inconclusive")


def test_full_react_tool_chain_saves_and_reads_skill_without_candidate_protocol(rig):
    class Model:
        calls = 0
        async def chat_stream(self, messages, tools, **kwargs):
            names = {tool["function"]["name"] for tool in tools}
            assert not {"learning_search", "learning"}.intersection(names)
            assert not any("RELATED LEARNING CANDIDATES" in str(m.get("content")) for m in messages)
            self.calls += 1
            if self.calls == 1:
                name, args = "skill_manage", {"action": "create", "origin": "auto", "name": "config-verifier",
                    "content": "---\nname: config-verifier\ndescription: Check configuration with evidence\n---\n" + SKILL}
            elif self.calls == 2:
                assert "config-verifier" in agent._available_skill_names
                name, args = "skill_view", {"name": "config-verifier"}
            else:
                yield {"type": "done", "content": "Experience saved; no independent verification claimed.", "usage": None}
                return
            yield {"type": "tool_calls", "calls": [{"id": f"call-{self.calls}", "name": name, "arguments": json.dumps(args)}],
                   "content": "", "reasoning_content": "", "usage": None}

    registry = ToolRegistry()
    model = Model()
    agent = ReActAgent("agent", model, registry, skill_store=rig.skills, max_iterations=5)
    agent._learning_reviewer = rig.reviewer
    agent.context.set_session(str(rig.root / "session-react.json"))
    register_skill_tools(registry, rig.skills, session_id=lambda: "session-react",
                         messages=lambda: list(agent.context.messages))
    async def run():
        return [event async for event in agent._run_react_loop(Msg(content=[ContentBlock.text("Save this reusable configuration check")]))]
    events = asyncio.run(run())
    assert not [event for event in events if event.get("type") == "error"]
    assert rig.store.count() == 0
    assert "Configuration verifier" in rig.skills.view("config-verifier")
    assert not rig.llm.calls


def test_activated_skill_scope_is_enforced_by_the_catalog(rig, monkeypatch):
    item = propose(rig)
    trial(rig, item)
    invoke(rig, "activate", candidate_id=item["id"])
    assert rig.skills.list()
    other = rig.root / "another-project"
    other.mkdir()
    monkeypatch.chdir(other)
    assert [item for item in rig.skills.list() if item["category"] != "builtin"] == []
    # Explicit inspection remains possible; catalog visibility is scoped.
    assert "Configuration verifier" in rig.skills.view("config-verifier")


def test_verified_observation_scope_covers_lexical_semantic_and_direct_readers(rig):
    from agent.runtime.context_index.lexical import LexicalQuery
    from agent.runtime.context_index.query import plan_query
    from agent.runtime.context_index.record_source import RecordSource
    from agent.runtime.context_index.semantic_index import canonical_items
    from agent.runtime.context_index.semantic_reader import SemanticReader
    from agent.runtime.context_index.workspace import WorkspaceIdentity
    from agent.runtime.learning_scope import learning_scope

    content = "Astra configuration selects deepseek-flash."
    proof = {"scope": learning_scope()}
    local = rig.memory.add_record(kind="observation", content=content, metadata={"learning_verification": proof})
    foreign_scope = {**learning_scope(), "workspace": str(rig.root / "another-project")}
    foreign = rig.memory.add_record(kind="observation", content=content + " another installation", metadata={"learning_verification": {"scope": foreign_scope}})
    foreign_platform = {**learning_scope(), "platform": "OtherPlatform"}
    wrong_os = rig.memory.add_record(kind="observation", content=content + " other OS", metadata={"learning_verification": {"scope": foreign_platform}})
    assert [record.record_id for record in rig.memory.recall_records("Astra configuration")] == [local.record_id]
    plan = plan_query("Astra configuration", datetime.now(timezone.utc) + timedelta(seconds=1))
    workspace = WorkspaceIdentity("local", str(rig.root), rig.root.name)
    source = RecordSource(rig.memory.path)
    assert [candidate.identity for candidate in source.recommend(plan, workspace, frozenset()).relevance] == [local.record_id]
    indexed = canonical_items(rig.memory.path, "memory")
    assert {item.item_id for item in indexed} == {local.record_id, foreign.record_id, wrong_os.record_id}
    hits = [(item.item_id, 0.95) for item in indexed]
    revisions = {item.item_id: item.revision for item in indexed}
    candidates = SemanticReader._records(rig.memory.path, hits, revisions, plan, workspace, "session", frozenset(), LexicalQuery.from_text(plan.query))
    assert [candidate.identity for candidate in candidates] == [local.record_id]


def test_subdirectory_uses_same_git_checkout_learning_scope(rig, monkeypatch):
    from agent.runtime.learning_scope import learning_scope

    (rig.root / ".git").mkdir()
    expected = learning_scope()
    subdirectory = rig.root / "package"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)
    assert learning_scope() == expected


def test_automatic_patch_cannot_remove_a_verified_skill_scope(rig):
    item = propose(rig)
    trial(rig, item)
    invoke(rig, "activate", candidate_id=item["id"])
    old = next(line for line in rig.skills.view("config-verifier").splitlines() if line.startswith("astra_learning_scope:"))
    raw = {"kind": "skill_patch", "payload": {"name": "config-verifier", "old_string": old,
           "new_string": old.replace("astra_learning_scope", "ignored_learning_scope"), "file_path": "SKILL.md"}, "reason": "Attempt to broaden scope"}
    revised = invoke(rig, "revise", candidate_id=item["id"], proposal=raw, contract=CONTRACT)
    assert "scope marker" in rig.lifecycle.activation_issue(revised["id"])


def test_new_support_file_does_not_bypass_manual_skill_protection(rig):
    rig.skills.create("config-verifier", "---\nname: config-verifier\ndescription: Manual skill\n---\n" + SKILL)
    raw = {"kind": "skill_write_file", "payload": {"name": "config-verifier", "file_path": "scripts/check.py",
           "content": "print('check')"}, "reason": "Add a helper"}
    item = propose(rig, raw)
    assert "manual" in rig.lifecycle.activation_issue(item["id"])
    assert rig.skills.raw_file("config-verifier", "scripts/check.py") is None


@pytest.mark.parametrize("background", [False, True])
def test_later_session_can_revise_original_provenance_without_counting_new_proof(rig, background):
    quote = "Please switch to deepseek-flash"
    raw = {"kind": "observation", "payload": {"content": "The project may use deepseek-flash.",
           "evidence": quote, "evidence_role": "user"}, "reason": "Candidate from original request"}
    rig.agent.context.messages = [{"role": "user", "content": quote}]
    item = propose(rig, raw, {**CONTRACT, "claim_type": "hypothesis"})
    rig.agent.context.messages = [{"role": "user", "content": "Clarify this candidate's scope"}]
    rig.agent.context.session_path = str(rig.root / "session-later.json")
    rig.agent._context_index_request_id = "later-session-request"
    revised_raw = {**raw, "payload": {**raw["payload"], "content": "This project may select deepseek-flash; verify the actual config."}}
    if background:
        rig.llm.proposal = {**revised_raw, "candidate_id": item["id"], "learning": {**CONTRACT, "claim_type": "hypothesis"}}
        outcome = asyncio.run(rig.reviewer.review_outcome(rig.agent.context.messages, "session-later"))
        revised = outcome["proposals"][0]
    else:
        revised = invoke(rig, "revise", candidate_id=item["id"], proposal=revised_raw, contract={**CONTRACT, "claim_type": "hypothesis"})
    assert revised["revision"] == 2 and revised["id"] == item["id"]
    assert revised["payload"]["evidence"] == quote
    assert revised["trials"] == []
    assert rig.memory.recall_records("deepseek-flash") == []
    assert "held-out" in rig.lifecycle.activation_issue(item["id"])


def test_revision_cannot_replace_original_evidence_with_an_invented_quote(rig):
    quote = "Please check the configuration"
    raw = {"kind": "observation", "payload": {"content": "The config may need revision.", "evidence": quote, "evidence_role": "user"}, "reason": "Candidate"}
    rig.agent.context.messages = [{"role": "user", "content": quote}]
    item = propose(rig, raw)
    rig.agent.context.messages = []
    raw["payload"]["evidence"] = "The user confirmed that all changes were successfully deployed"
    result = asyncio.run(rig.registry.execute("learning", {"action": "revise", "candidate_id": item["id"], "proposal": raw, "contract": CONTRACT}))
    assert "exact original" in result["error"]
    assert rig.lifecycle.show(item["id"])["revision"] == 1
