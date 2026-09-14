"""Side-effect safety evaluation suite.

Tests tool authorization policy, idempotency guards, approval gates,
risk-level enforcement, and repeat-call protection.

Run with:
    pytest tests/test_side_effect_safety_eval.py -v
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.runtime.tools.policy import ToolPolicy, VALID_RISKS
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def _make_tool(name, risk="read", approval="never", idempotent=False, **kw):
    """Create a minimal ToolDef for testing."""
    async def noop(**kwargs):
        return {"ok": True, "tool": name, "args": kwargs}
    return ToolDef(
        name=name,
        description=f"Test tool: {name}",
        parameters={"type": "object", "properties": {}},
        fn=noop,
        risk=risk,
        approval=approval,
        idempotent=idempotent,
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. Policy modes
# ---------------------------------------------------------------------------

class TestPolicyModes:
    def test_permissive_allows_all_risks(self):
        policy = ToolPolicy(mode="permissive")
        for risk in VALID_RISKS:
            allowed, reason = policy.authorize("test_tool", risk)
            assert allowed, f"permissive should allow {risk}: {reason}"

    def test_safe_blocks_execute_and_secret(self):
        policy = ToolPolicy(mode="safe")
        for risk in ("read", "write", "network"):
            allowed, _ = policy.authorize("test_tool", risk)
            assert allowed, f"safe should allow {risk}"
        for risk in ("execute", "secret"):
            allowed, reason = policy.authorize("test_tool", risk)
            assert not allowed, f"safe should block {risk}: {reason}"

    def test_locked_allows_read_and_network_but_blocks_mutation(self):
        policy = ToolPolicy(mode="locked")
        for risk in ("read", "network"):
            allowed, reason = policy.authorize("test_tool", risk)
            assert allowed, reason
        for risk in ("write", "execute", "secret"):
            allowed, reason = policy.authorize("test_tool", risk)
            assert not allowed, f"locked should block {risk}: {reason}"

    def test_invalid_mode_defaults_to_permissive(self):
        policy = ToolPolicy(mode="invalid_mode")
        # from_env normalizes, but direct construction doesn't
        # authorize should still work
        allowed, _ = policy.authorize("test_tool", "read")
        assert allowed

    def test_set_mode_validates(self):
        policy = ToolPolicy()
        with pytest.raises(ValueError, match="Unknown policy mode"):
            policy.set_mode("yolo")
        policy.set_mode("locked")
        assert policy.mode == "locked"


# ---------------------------------------------------------------------------
# 2. Allow / deny lists
# ---------------------------------------------------------------------------

class TestAllowDenyLists:
    def test_denied_tool_blocked_even_in_permissive(self):
        policy = ToolPolicy(mode="permissive")
        policy.deny("dangerous_tool")
        allowed, reason = policy.authorize("dangerous_tool", "read")
        assert not allowed
        assert "denied" in reason

    def test_allowed_tool_bypasses_locked(self):
        policy = ToolPolicy(mode="locked")
        policy.allow("special_tool")
        allowed, reason = policy.authorize("special_tool", "execute")
        assert allowed
        assert "session approval" in reason

    def test_deny_overrides_allow(self):
        policy = ToolPolicy(mode="permissive")
        policy.allow("tool_x")
        policy.deny("tool_x")
        allowed, _ = policy.authorize("tool_x", "read")
        assert not allowed

    def test_allow_removes_deny(self):
        policy = ToolPolicy(mode="permissive")
        policy.deny("tool_y")
        policy.allow("tool_y")
        allowed, _ = policy.authorize("tool_y", "read")
        assert allowed


# ---------------------------------------------------------------------------
# 3. Approval gates
# ---------------------------------------------------------------------------

class TestApprovalGates:
    def test_approval_always_requires_approval(self):
        policy = ToolPolicy(mode="permissive")
        allowed, reason = policy.authorize("write_tool", "write", approval="always")
        assert not allowed
        assert "requires approval" in reason

    def test_approval_never_passes_permissive(self):
        policy = ToolPolicy(mode="permissive")
        allowed, _ = policy.authorize("read_tool", "read", approval="never")
        assert allowed

    def test_session_approval_bypasses_gate(self):
        policy = ToolPolicy(mode="permissive")
        policy.allow("write_tool")
        allowed, reason = policy.authorize("write_tool", "write", approval="always")
        assert allowed
        assert "session approval" in reason


# ---------------------------------------------------------------------------
# 4. Registry enforcement
# ---------------------------------------------------------------------------

class TestRegistryEnforcement:
    def test_register_rejects_invalid_risk(self):
        registry = ToolRegistry()
        tool = _make_tool("bad_risk", risk="destroy")
        with pytest.raises(ValueError, match="Invalid risk"):
            registry.register(tool)

    def test_register_rejects_duplicate(self):
        registry = ToolRegistry()
        registry.register(_make_tool("dup_tool"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_make_tool("dup_tool"))

    def test_call_denied_tool_returns_error(self):
        policy = ToolPolicy(mode="permissive")
        policy.deny("blocked_tool")
        registry = ToolRegistry(policy=policy)
        registry.register(_make_tool("blocked_tool", risk="write"))

        async def _test():
            result = await registry.execute("blocked_tool", {})
            assert result.get("error"), f"Expected error for denied tool: {result}"
        run(_test())

    def test_call_allowed_tool_succeeds(self):
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        registry.register(_make_tool("good_tool"))

        async def _test():
            result = await registry.execute("good_tool", {"x": 1})
            assert not result.get("error"), f"Unexpected error: {result}"
        run(_test())

    def test_call_unknown_tool_returns_error(self):
        registry = ToolRegistry()

        async def _test():
            result = await registry.execute("nonexistent_tool", {})
            assert result.get("error"), f"Expected error for unknown tool: {result}"
        run(_test())


# ---------------------------------------------------------------------------
# 5. Risk level metadata
# ---------------------------------------------------------------------------

class TestRiskMetadata:
    def test_all_valid_risks_accepted(self):
        registry = ToolRegistry()
        for i, risk in enumerate(sorted(VALID_RISKS)):
            registry.register(_make_tool(f"risk_{risk}", risk=risk))
        # All should be registered
        descs = registry.describe()
        names = {d["name"] for d in descs}
        for risk in VALID_RISKS:
            assert f"risk_{risk}" in names

    def test_risk_exposed_in_schema(self):
        registry = ToolRegistry()
        registry.register(_make_tool("risky_tool", risk="execute"))
        descs = registry.describe()
        tool_desc = next(d for d in descs if d["name"] == "risky_tool")
        assert tool_desc.get("risk") == "execute"

    def test_idempotent_flag_exposed(self):
        registry = ToolRegistry()
        registry.register(_make_tool("idem_tool", idempotent=True))
        descs = registry.describe()
        tool_desc = next(d for d in descs if d["name"] == "idem_tool")
        assert tool_desc.get("idempotent") is True


# ---------------------------------------------------------------------------
# 6. Repeat guard / idempotency
# ---------------------------------------------------------------------------

class TestRepeatGuard:
    @pytest.mark.xfail(reason="repeat_guard is enforced at ReAct loop level, not registry.execute()")
    def test_repeat_guard_blocks_identical_calls(self):
        """Consecutive identical calls should be blocked by repeat guard."""
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        call_count = 0

        async def counting_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        tool = ToolDef(
            name="counting_tool",
            description="counts calls",
            parameters={"type": "object", "properties": {}},
            fn=counting_fn,
            repeat_guard=True,
        )
        registry.register(tool)

        async def _test():
            r1 = await registry.execute("counting_tool", {"a": 1})
            assert not r1.get("error"), f"First call failed: {r1}"
            assert "1" in r1.get("output", "")

            # Same args again — should be blocked by repeat guard
            r2 = await registry.execute("counting_tool", {"a": 1})
            is_blocked = (
                "repeat" in str(r2).lower()
                or "duplicate" in str(r2).lower()
                or r2.get("error")
                or "1" in r2.get("output", "")  # cached result, count still 1
            )
            assert is_blocked, f"Repeat guard did not block identical call: {r2}"

            # Different args — should go through
            r3 = await registry.execute("counting_tool", {"a": 2})
            assert not r3.get("error"), f"Different args call failed: {r3}"
        run(_test())

    def test_repeat_guard_disabled_allows_duplicates(self):
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        call_count = 0

        async def counting_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        tool = ToolDef(
            name="no_guard_tool",
            description="no guard",
            parameters={"type": "object", "properties": {}},
            fn=counting_fn,
            repeat_guard=False,
        )
        registry.register(tool)

        async def _test():
            r1 = await registry.execute("no_guard_tool", {"a": 1})
            r2 = await registry.execute("no_guard_tool", {"a": 1})
            assert not r1.get("error")
            assert not r2.get("error")
            # Both should execute (count increments)
            assert "1" in r1.get("output", "")
            assert "2" in r2.get("output", "")
        run(_test())


# ---------------------------------------------------------------------------
# 7. Max calls per turn
# ---------------------------------------------------------------------------

class TestMaxCallsPerTurn:
    @pytest.mark.xfail(reason="max_calls_per_turn is enforced at ReAct loop level, not registry.execute()")
    def test_max_calls_enforced(self):
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        call_count = 0

        async def counting_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        tool = ToolDef(
            name="limited_tool",
            description="limited calls",
            parameters={"type": "object", "properties": {}},
            fn=counting_fn,
            max_calls_per_turn=2,
            repeat_guard=False,
        )
        registry.register(tool)

        async def _test():
            r1 = await registry.execute("limited_tool", {"i": 1})
            assert not r1.get("error"), f"First call failed: {r1}"
            r2 = await registry.execute("limited_tool", {"i": 2})
            assert not r2.get("error"), f"Second call failed: {r2}"
            # Third call should be blocked by max_calls_per_turn
            r3 = await registry.execute("limited_tool", {"i": 3})
            is_blocked = (
                r3.get("error")
                or "limit" in str(r3).lower()
                or "max" in str(r3).lower()
            )
            assert is_blocked, f"Max calls not enforced: {r3}"
        run(_test())
