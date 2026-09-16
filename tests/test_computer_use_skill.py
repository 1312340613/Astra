from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from agent.runtime.computer_protocol import PROTOCOL_VERSION
from agent.runtime.skills import SkillStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".astra" / "skills"
SKILL_PATH = SKILLS_ROOT / "operations" / "using-computer-use" / "SKILL.md"
OPERATOR_DOC_PATH = REPO_ROOT / "docs" / "macos-computer-use.md"
REAL_MACHINE_RUNBOOK_PATH = REPO_ROOT / "docs" / "macos-computer-use-real-machine-test-runbook.md"
COMPATIBILITY_REGISTRY_PATH = REPO_ROOT / "config" / "macos_computer_compatibility.json"
APP_STATE_ACCEPTANCE_TEMPLATE_PATH = REPO_ROOT / "docs/macos-app-state-acceptance.md"
COMPATIBILITY_REPORT_RELATIVE_PATH = "docs/macos-computer-compatibility-evidence.md"
COMPATIBILITY_REPORT_PATH = REPO_ROOT / COMPATIBILITY_REPORT_RELATIVE_PATH


def _skill_content() -> str:
    return SkillStore(SKILLS_ROOT).view("using-computer-use")


def _normalized_skill_content() -> str:
    return " ".join(_skill_content().lower().split())


def _markdown_section(content: str, heading: str) -> str:
    heading_match = re.fullmatch(r"(#{1,6})[ \t]+.+", heading)
    assert heading_match is not None, heading
    max_boundary_depth = len(heading_match.group(1))
    match = re.search(
        rf"(?ms)^{re.escape(heading)}\n"
        rf"(.*?)(?=^#{{1,{max_boundary_depth}}}[ \t]+|\Z)",
        content,
    )
    assert match is not None, heading
    return match.group(1)


def test_markdown_section_stops_at_peer_heading_for_every_supported_depth():
    for depth in range(3, 7):
        hashes = "#" * depth
        content = (
            f"{hashes} Target\n"
            "inside\n"
            f"{hashes} Next\n"
            "outside\n"
        )

        assert _markdown_section(content, f"{hashes} Target") == "inside\n"


def test_computer_use_skill_is_discoverable_and_trigger_description_is_narrow():
    listed = {item["name"]: item for item in SkillStore(SKILLS_ROOT).list()}

    item = listed["using-computer-use"]
    assert item["category"] == "operations"
    assert item["files"] == 2  # Compact entry plus case-specific file workflow reference.
    assert str(item["description"]).startswith("Use when")
    assert "macOS" in str(item["description"])
    assert "computer_" in str(item["description"])
    assert not {"approval", "snapshot", "handoff"} & set(
        str(item["description"]).lower().split()
    )
    assert _skill_content().startswith("---\nname: using-computer-use\n")


def test_only_computer_use_skill_file_is_visible_to_git():
    visible = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", str(SKILL_PATH)],
        cwd=REPO_ROOT,
        check=False,
    )
    hidden = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            str(SKILL_PATH.parent / "private-note.md"),
        ],
        cwd=REPO_ROOT,
        check=False,
    )

    assert visible.returncode == 1
    assert hidden.returncode == 0


def test_computer_use_skill_defines_snapshot_and_target_workflow():
    content = _skill_content()
    lower = content.lower()

    for heading in ("## Overview", "## When to Use", "## Quick Reference", "## Common Mistakes"):
        assert heading in content
    # Cap raised from 525 to 900 on 2026-09-02: the skill deliberately grew
    # with background PID-pointer delivery + approval/focus discipline
    # (commit bfc2533). Keep the guard tight enough to catch runaway bloat.
    assert len(re.findall(r"\b[\w*/_-]+\b", content)) < 900
    assert "`computer_apps` → `computer_get_app_state`" in content
    for required in (
        "element ref",
        "target-window",
        "display",
        "explicit once",
        "/computer setup",
        "latest snapshot",
        "one snapshot permits one act",
        "request-local",
        "browser tools",
        "close cleanup",
        "computer_handoff",
        "computer_resume",
        "computer_close",
    ):
        assert required in lower
    assert "`/computer status` is cached and nonactivating" in lower
    assert "`computer_status` queries the helper and refreshes that cache" in lower
    assert "neither status path prompts nor opens system settings" in lower


def test_computer_use_skill_preserves_specific_stale_target_and_approval_warnings():
    content = _skill_content()

    assert "- Reusing a snapshot after focus, window, modal, or app change." in content
    assert (
        "- Inferring another app/version/action or bypassing approval/failures."
        in content
    )


def test_computer_use_skill_preserves_approval_and_secure_boundaries():
    lower = _skill_content().lower()

    for required in (
        "ordinary",
        "session + bundle",
        "high-impact",
        "exact-batch",
        "send",
        "export",
        "upload",
        "delete",
        "install",
        "system settings",
        "terminal enter",
        "password",
        "otp",
        "touch id",
        "payment",
        "prohibited",
        "handoff",
        "yolo",
        "autoapprove",
        "typed text",
        "secure field",
    ):
        assert required in lower
    assert "cannot downgrade" in lower


def test_computer_use_skill_classifies_local_file_writes_as_high_impact():
    content = _normalized_skill_content()

    assert "only in-memory or document-content editing" in content
    for file_write in ("save as", "save a copy", "create or overwrite a local file"):
        assert file_write in content
    assert "high-impact exact-batch once approval" in content


def test_computer_use_skill_scopes_approval_target_by_effect_type():
    content = _normalized_skill_content()

    assert "high-impact file create/read/overwrite/export" in content
    assert "trusted canonical user-selected filesystem destination" in content
    assert "approval target and copy" in content
    assert "non-file high-impact" in content
    for action in ("terminal enter", "send", "submit", "system settings"):
        assert action in content
    assert "trusted app, window, action class, effect, and boundary" in content
    assert "does not require a filesystem destination" in content
    for forbidden in (
        "typed text",
        "secure values",
        "internal refs",
        "session ids",
        "request-local screenshot",
        "cache paths",
        "helper paths",
    ):
        assert forbidden in content


def test_computer_use_skill_never_replays_an_uncertain_semantic_action():
    lower = _skill_content().lower()

    for cause in ("unknown_outcome", "timeout", "cancellation", "helper loss"):
        assert cause in lower
    assert "never retry or replay" in lower
    assert "even if a fresh snapshot looks unchanged" in lower
    assert "fresh snapshot is diagnostic only" in lower
    assert "ask the user to decide" in lower
    assert "resume with a fresh snapshot" in lower


def test_computer_use_skill_defines_last_ack_index_semantics():
    content = _normalized_skill_content()

    assert "`last_ack` is the zero-based index" in content
    assert "last post-guard confirmed action" in content
    assert "`-1` means none" in content
    assert "only actions `0..last_ack` are confirmed" in content
    assert "after `last_ack` has unknown semantic outcome" in content
    assert "must not be automatically replayed" in content


def test_computer_use_skill_deadline_never_weakens_safety_or_closes_unknown_work():
    content = _normalized_skill_content()

    assert "deadline" in content
    for forbidden_shortcut in (
        "cancel an in-flight write",
        "close an unknown session",
        "skip snapshot or approval",
    ):
        assert forbidden_shortcut in content
    assert "between actions" in content
    assert "stop safely" in content
    assert "unknown outcome requires handoff" in content
    assert "user may perform the action personally" in content
    assert "retry does not authorize the agent to replay the original semantic action" in content
    assert "`computer_close` only when no write is in flight" in content


def test_operator_docs_name_the_implemented_background_degraded_error():
    content = OPERATOR_DOC_PATH.read_text(encoding="utf-8")

    assert "`background_action_unsupported`" in content
    assert "`compatibility_disabled`" not in content


def test_operator_docs_do_not_promise_removed_unicode_input_fallback():
    content = OPERATOR_DOC_PATH.read_text(encoding="utf-8")

    assert "falls back to guarded Unicode input" not in content
    assert "AX selected-text is unsupported" in content
    assert "fails closed without synthetic keyboard input" in content


def test_computer_use_skill_links_the_current_compatibility_report():
    assert COMPATIBILITY_REPORT_RELATIVE_PATH in _skill_content()


def test_current_compatibility_report_records_exact_fail_closed_evidence():
    assert COMPATIBILITY_REPORT_PATH.is_file()
    content = COMPATIBILITY_REPORT_PATH.read_text(encoding="utf-8")

    for exact_cell in (
        "`dev.astra.computer-fixture` | `1.0.0`",
        "`com.kingsoft.wpsoffice.mac` | `12.1.26026`",
        "`com.microsoft.VSCode` | `1.134.0`",
        "`com.microsoft.edgemac` | `151.0.4129.107`",
    ):
        assert exact_cell in content
    for required in (
        "AX tree preparation/selection",
        "before input",
        "real pointer did not move",
        "exactly empty",
        '"applications": []',
    ):
        assert required in content


def test_current_docs_never_claim_a_passing_real_compatibility_cell():
    paths = (OPERATOR_DOC_PATH, COMPATIBILITY_REPORT_PATH)
    status_pattern = re.compile(
        r"(?im)^(?:real-desktop compatibility|real compatibility|compatibility cell)"
        r"[^\n:]*:\s*PASS\b"
    )

    for path in paths:
        content = path.read_text(encoding="utf-8") if path.is_file() else ""
        assert status_pattern.search(content) is None, path


def test_computer_use_skill_documents_transactional_app_state_read_recovery():
    quick_reference = _markdown_section(_skill_content(), "## Quick Reference").lower()

    assert re.search(
        r"computer_apps.*→.*computer_get_app_state.*latest catalog",
        quick_reference,
        re.DOTALL,
    )
    assert re.search(r"computer_focus.*explicit advanced", quick_reference, re.DOTALL)
    assert re.search(
        r"computer_snapshot.*refreshes.*bound target", quick_reference, re.DOTALL
    )
    assert "does not focus the app or move the real pointer" in quick_reference
    assert "target or action invalidation" in quick_reference
    assert "authority invalidation" in quick_reference
    assert "smart approval" in quick_reference
    assert "bounded error" in quick_reference
    assert re.search(
        r"unsafe_artifact.*computer_close.*fresh session.*computer_apps.*computer_get_app_state",
        quick_reference,
        re.DOTALL,
    )


def test_catalog_binding_guidance_requires_a_current_bindable_ref():
    for path in (SKILL_PATH, OPERATOR_DOC_PATH):
        content = " ".join(path.read_text(encoding="utf-8").lower().split())

        assert re.search(
            r"choose (?:only )?`bindable=true`(?: only)?",
            content,
        )
        assert "`ax_window_unmatched`" in content
        assert ("stop repeated catalog/bind calls while it is unchanged" in content
                or "状态未变时停止重复绑定" in content)
        assert ("after the panel state changes, refresh the catalog and obtain new refs" in content
                or "面板状态改变后刷新目录获取新引用" in content)
        assert "never reuse" in content
        assert "rebind" in content
        assert "old ref" in content


def test_current_tree_documents_catalog_and_acknowledgement_safety_boundaries():
    skill = _normalized_skill_content()
    for required in (
        "exact refs from the latest catalog",
        "choose only `bindable=true` catalog windows",
        "`ax_window_unmatched` 表示窗口仍在但缺少唯一 ax 匹配",
        "状态未变时停止重复绑定",
        "面板状态改变后刷新目录获取新引用",
        "never reuse or implicitly rebind an old ref",
        "one snapshot permits one act",
        "after `unknown_outcome`, timeout, cancellation, or helper loss, never retry or replay",
        "every action after `last_ack` has unknown semantic outcome",
        "must not be automatically replayed",
    ):
        assert required in skill

    runbook = " ".join(
        REAL_MACHINE_RUNBOOK_PATH.read_text(encoding="utf-8").lower().split()
    )
    assert "`action_result.status=action_acknowledged` only proves that the guarded native action was accepted" in runbook
    assert "`effect_verification=unverified` means the attached fresh snapshot must be inspected" in runbook
    assert "if it does not appear, record `postcondition_failed`, hand off, and do not replay" in runbook


def test_operator_docs_and_runbook_distinguish_deterministic_app_state_evidence():
    operator = _markdown_section(
        OPERATOR_DOC_PATH.read_text(encoding="utf-8"), "### Transactional app-state read"
    ).lower()
    runbook = _markdown_section(
        REAL_MACHINE_RUNBOOK_PATH.read_text(encoding="utf-8"),
        "## 3.1 Transactional app-state boundary",
    ).lower()

    for content in (operator, runbook):
        assert "computer_apps" in content
        assert "computer_get_app_state" in content
        assert "unsafe_artifact" in content
        assert "computer_close" in content
        assert "task 10" in content
    assert "no-focus/no-pointer" in operator
    assert "not a task 10 live result" in operator
    assert "not a task 10 live result" in runbook


def test_runbook_registry_contracts_match_the_tracked_registry() -> None:
    expected = json.loads(COMPATIBILITY_REGISTRY_PATH.read_text(encoding="utf-8"))
    content = REAL_MACHINE_RUNBOOK_PATH.read_text(encoding="utf-8")

    # Every literal registry payload embedded in the runbook must equal the
    # tracked registry exactly (no stale empty-registry examples).
    exact_registry_contracts = re.findall(
        r'^\{"schema_version":\s*\d+.*\}$',
        content,
        flags=re.MULTILINE,
    )
    assert all(json.loads(contract) == expected for contract in exact_registry_contracts)
    # The runbook can reference the single tracked source instead of duplicating
    # its payload, but must verify both source immutability and bundled bytes.
    assert "cmp config/macos_computer_compatibility.json" in content
    assert ".astra/bin/AstraMacComputerHelper.app/Contents/Resources/macos_computer_compatibility.json" in content
    assert "registry-before.sha256" in content
    assert "registry-after.sha256" in content
    assert "registry_matches_head=%s" in content
    assert "$astra_registry_preflight_unchanged" in content
    assert "registry_empty" not in content
    assert "astra_registry_preflight_empty" not in content

    # The empty-registry contract era is over: no stale "must be empty"
    # assertions may remain, and every runbook gate must instead require the
    # registry to match checked-in HEAD (uncommitted test contamination).
    assert '{"schema_version": 2, "applications": []}' not in content
    assert '"applications": []}' not in content
    assert content.count(
        "git diff --quiet HEAD -- config/macos_computer_compatibility.json"
    ) >= 2


def test_runbook_status_preflight_uses_the_current_computer_protocol():
    content = REAL_MACHINE_RUNBOOK_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"'(?P<request>\{[^'\n]*\"request_id\":\"manual-e2e-preflight\"[^'\n]*\})'",
        content,
    )

    assert match is not None
    request = json.loads(match.group("request"))
    assert request == {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "manual-e2e-preflight",
        "operation": "status",
        "payload": {},
    }


def test_runbook_links_task_10_operator_to_the_transactional_template():
    content = REAL_MACHINE_RUNBOOK_PATH.read_text(encoding="utf-8")
    runbook = _markdown_section(
        content,
        "## 3.1 Transactional app-state boundary",
    )

    assert "不 commit，不 push" not in content
    assert (
        "[Task 10 transactional app-state matrix]"
        "(macos-app-state-acceptance.md)"
    ) in runbook
    assert re.search(
        r"Task 10.*(?:populate|update).*matrix.*commit.*measured update",
        runbook,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"final report.*reference.*matrix.*not.*competing output",
        runbook,
        re.IGNORECASE | re.DOTALL,
    )


def test_transactional_app_state_acceptance_template_records_matrix_evidence():
    assert APP_STATE_ACCEPTANCE_TEMPLATE_PATH.is_file()
    content = APP_STATE_ACCEPTANCE_TEMPLATE_PATH.read_text(encoding="utf-8")
    matrix = _markdown_section(content, "## Transactional app-state matrix")
    result_rules = _markdown_section(content, "## Result rules")
    lower = matrix.lower()

    for application in (
        "Fixture",
        "TextEdit",
        "VS Code/Electron",
        "WPS Home",
        "WPS document window",
    ):
        assert application in content
    for field in (
        "Commit/helper hash",
        "Exact app/window refs",
        "stable OS identity",
        "Frontmost PID",
        "Cursor coordinates",
        "PNG validity",
        "target bounds",
        "ordinary AX",
        "Smart detail",
        "truncation reasons",
        "artifact cleanup",
    ):
        assert field.lower() in lower
    for result in ("`PASS`", "`WARN`", "`FAIL`", "`NOT RUN`"):
        assert result in result_rules


def test_wps_ax_omission_is_warn_not_appshot_equivalent_pass():
    rules = _markdown_section(
        APP_STATE_ACCEPTANCE_TEMPLATE_PATH.read_text(encoding="utf-8"), "## Result rules"
    )

    assert re.search(
        r"WPS.*self-drawn document body.*absent from AX.*`WARN`",
        rules,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(r"not an? Appshot-equivalent PASS", rules, re.IGNORECASE)


def test_skill_uses_returned_observation_without_mandatory_extra_capture():
    content = _skill_content()
    assert 'get_app_state 的 id 不能授权 act' not in content
    assert '每个 act 前必重跑' not in content
    assert '返回的新 snapshot_id' in content
    assert 'routing_advice' in content
    assert '空 AX 树' in content
    assert '走无审批路径' not in content
    assert '全局 HID 点击开 compose' not in content


def test_skill_teaches_menu_route_and_concrete_outcome_boundaries():
    content = _skill_content()
    reference = "references/native-file-workflows.md"
    assert reference in content
    content += (SKILLS_ROOT / "operations" / "using-computer-use" / reference).read_text()
    assert 'AXMenuItem' in content
    assert '先确认目标标签当前已选中' in content
    assert 'target_element_ref' in content
    assert 'action_observation' in content
    assert '中文 Unicode 文本' in content
