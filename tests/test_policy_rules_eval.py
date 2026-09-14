"""Pattern-based tool authorization rules evaluation suite.

Tests PolicyRule matching, action enforcement, rule ordering,
argument matchers (path/domain/command), and backward compatibility.

Run with:
    pytest tests/test_policy_rules_eval.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.runtime.tools.policy import ToolPolicy, PolicyRule


# ---------------------------------------------------------------------------
# PolicyRule matching
# ---------------------------------------------------------------------------

class TestPolicyRuleMatching:
    def test_exact_tool_name_match(self):
        rule = PolicyRule(tool_pattern="execute_shell", action="deny")
        assert rule.matches("execute_shell")
        assert not rule.matches("execute_python")

    def test_glob_tool_pattern(self):
        rule = PolicyRule(tool_pattern="git_*", action="allow")
        assert rule.matches("git_status")
        assert rule.matches("git_commit")
        assert not rule.matches("execute_shell")

    def test_wildcard_matches_all(self):
        rule = PolicyRule(tool_pattern="*", action="ask")
        assert rule.matches("anything")
        assert rule.matches("execute_shell")

    def test_no_arg_matchers_matches_any_args(self):
        rule = PolicyRule(tool_pattern="write_file", action="allow")
        assert rule.matches("write_file", {"path": "/any/path"})
        assert rule.matches("write_file", {})
        assert rule.matches("write_file")

    def test_path_arg_matcher(self):
        rule = PolicyRule(
            tool_pattern="write_file",
            action="allow",
            arg_matchers={"path": "/safe/dir/*"},
        )
        assert rule.matches("write_file", {"path": "/safe/dir/file.txt"})
        assert rule.matches("write_file", {"path": "/safe/dir/sub/file.txt"})
        assert not rule.matches("write_file", {"path": "/etc/passwd"})
        assert not rule.matches("write_file", {})  # missing arg → no match

    def test_url_arg_matcher(self):
        rule = PolicyRule(
            tool_pattern="fetch_url",
            action="deny",
            arg_matchers={"url": "*.internal.corp/*"},
        )
        assert rule.matches("fetch_url", {"url": "https://api.internal.corp/data"})
        assert not rule.matches("fetch_url", {"url": "https://example.com"})

    def test_command_arg_matcher(self):
        rule = PolicyRule(
            tool_pattern="execute_shell",
            action="allow",
            arg_matchers={"command": "git *"},
        )
        assert rule.matches("execute_shell", {"command": "git status"})
        assert rule.matches("execute_shell", {"command": "git commit -m 'msg'"})
        assert not rule.matches("execute_shell", {"command": "rm -rf /"})

    def test_multiple_arg_matchers_all_must_match(self):
        rule = PolicyRule(
            tool_pattern="deploy",
            action="allow",
            arg_matchers={"env": "staging", "region": "us-*"},
        )
        assert rule.matches("deploy", {"env": "staging", "region": "us-east"})
        assert not rule.matches("deploy", {"env": "production", "region": "us-east"})
        assert not rule.matches("deploy", {"env": "staging", "region": "eu-west"})

    def test_invalid_action_raises(self):
        with pytest.raises(ValueError, match="Invalid action"):
            PolicyRule(tool_pattern="x", action="maybe")

    def test_invalid_action_error_includes_bad_value_and_allowed_list(self):
        with pytest.raises(ValueError) as exc_info:
            PolicyRule(tool_pattern="x", action="maybe")
        msg = str(exc_info.value)
        # 错误消息必须包含收到的非法值
        assert "maybe" in msg
        # 错误消息必须包含按稳定顺序排列的允许值
        assert "allow, ask, deny" in msg


# ---------------------------------------------------------------------------
# ToolPolicy with rules
# ---------------------------------------------------------------------------

class TestPolicyWithRules:
    def test_deny_rule_blocks_tool(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(tool_pattern="dangerous_tool", action="deny"))
        allowed, reason = policy.authorize("dangerous_tool", "execute")
        assert not allowed
        assert "Denied by rule" in reason

    def test_allow_rule_bypasses_mode(self):
        policy = ToolPolicy(mode="locked")  # locked only allows read
        policy.add_rule(PolicyRule(tool_pattern="special_write", action="allow"))
        allowed, reason = policy.authorize("special_write", "write")
        assert allowed
        assert "rule" in reason

    def test_ask_rule_requires_approval(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(tool_pattern="deploy_*", action="ask"))
        allowed, reason = policy.authorize("deploy_prod", "execute")
        assert not allowed
        assert "Approval required" in reason

        policy.allow("deploy_prod")
        allowed, reason = policy.authorize("deploy_prod", "execute")
        assert allowed
        assert reason == "session approval"

    def test_deny_rule_cannot_be_shadowed_by_earlier_allow(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(tool_pattern="git_*", action="allow", description="git ok"))
        policy.add_rule(PolicyRule(tool_pattern="git_*", action="deny", description="git blocked"))
        allowed, reason = policy.authorize("git_status", "read")
        assert not allowed
        assert "git blocked" in reason

    def test_paths_are_normalized_before_matching(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(
            tool_pattern="write_file",
            action="deny",
            arg_matchers={"path": "/safe/private/*"},
        ))
        allowed, _ = policy.authorize(
            "write_file", "write", args={"path": "/safe/public/../private/secret.txt"}
        )
        assert not allowed

    def test_url_normalization_strips_fragment_and_default_port(self):
        rule = PolicyRule(
            tool_pattern="fetch_url",
            action="deny",
            arg_matchers={"url": "https://example.com/private/*"},
        )
        assert rule.matches(
            "fetch_url", {"url": "HTTPS://EXAMPLE.COM:443/public/../private/a#fragment"}
        )

    def test_resource_scoped_allow_does_not_grant_other_path(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(tool_pattern="write_file", action="ask"))
        scope = policy.allow_call("write_file", {"path": "/workspace/a.txt", "content": "one"})
        assert scope in policy.allowed_calls
        assert policy.authorize(
            "write_file", "write", args={"path": "/workspace/a.txt", "content": "two"}
        )[0]
        assert not policy.authorize(
            "write_file", "write", args={"path": "/workspace/b.txt", "content": "one"}
        )[0]

    def test_rule_with_path_matcher(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(
            tool_pattern="write_file",
            action="deny",
            arg_matchers={"path": "/etc/*"},
            description="no /etc writes",
        ))
        # Denied: matches rule
        allowed, reason = policy.authorize("write_file", "write", args={"path": "/etc/passwd"})
        assert not allowed
        assert "no /etc writes" in reason

        # Allowed: doesn't match rule, falls through to permissive
        allowed, reason = policy.authorize("write_file", "write", args={"path": "/home/user/file.txt"})
        assert allowed

    def test_rule_with_command_matcher(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(
            tool_pattern="execute_shell",
            action="deny",
            arg_matchers={"command": "rm *"},
            description="no rm",
        ))
        allowed, _ = policy.authorize("execute_shell", "execute", args={"command": "rm -rf /"})
        assert not allowed

        allowed, _ = policy.authorize("execute_shell", "execute", args={"command": "ls -la"})
        assert allowed

    def test_no_rules_backward_compatible(self):
        policy = ToolPolicy(mode="permissive")
        allowed, reason = policy.authorize("any_tool", "execute")
        assert allowed
        assert "permissive" in reason

    def test_rules_checked_before_session_sets(self):
        policy = ToolPolicy(mode="permissive")
        policy.allow("my_tool")  # session allow
        policy.add_rule(PolicyRule(tool_pattern="my_tool", action="deny"))  # rule deny
        # Rule takes precedence over session allow
        allowed, reason = policy.authorize("my_tool", "read")
        assert not allowed
        assert "Denied by rule" in reason

    def test_clear_rules(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(tool_pattern="x", action="deny"))
        policy.clear_rules()
        allowed, _ = policy.authorize("x", "read")
        assert allowed


# ---------------------------------------------------------------------------
# Shortcut API
# ---------------------------------------------------------------------------

class TestRuleShortcuts:
    def test_add_rule_shortcut_path(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule_shortcut("write_file", "deny", path="/sys/*", description="no /sys")
        allowed, _ = policy.authorize("write_file", "write", args={"path": "/sys/kernel"})
        assert not allowed

    def test_add_rule_shortcut_url(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule_shortcut("fetch_url", "deny", url="*.evil.com/*")
        allowed, _ = policy.authorize("fetch_url", "network", args={"url": "https://mal.evil.com/payload"})
        assert not allowed

    def test_add_rule_shortcut_command(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule_shortcut("execute_shell", "allow", command="pytest *")
        allowed, _ = policy.authorize("execute_shell", "execute", args={"command": "pytest tests/"})
        assert allowed

    def test_add_rule_shortcut_extra_matchers(self):
        policy = ToolPolicy(mode="permissive")
        policy.add_rule_shortcut("deploy", "ask", env="production")
        allowed, reason = policy.authorize("deploy", "execute", args={"env": "production"})
        assert not allowed
        assert "Approval required" in reason


# ---------------------------------------------------------------------------
# Integration with ToolRegistry
# ---------------------------------------------------------------------------

class TestRegistryIntegration:
    def test_registry_passes_args_to_policy(self):
        import asyncio
        from agent.runtime.tools.registry import ToolDef, ToolRegistry

        policy = ToolPolicy(mode="permissive")
        policy.add_rule(PolicyRule(
            tool_pattern="write_file",
            action="deny",
            arg_matchers={"path": "/protected/*"},
        ))
        registry = ToolRegistry(policy=policy)

        async def fake_write(path: str = "", content: str = ""):
            return f"wrote to {path}"

        registry.register(ToolDef(
            name="write_file",
            description="write a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
            fn=fake_write,
            risk="write",
        ))

        # Denied by rule
        result = asyncio.run(registry.execute("write_file", {"path": "/protected/secret.txt", "content": "x"}))
        assert result.get("error")
        assert "Denied by rule" in result["error"]

        # Allowed (different path)
        result = asyncio.run(registry.execute("write_file", {"path": "/home/user/ok.txt", "content": "x"}))
        assert not result.get("error")
        assert "wrote to" in result.get("output", "")
