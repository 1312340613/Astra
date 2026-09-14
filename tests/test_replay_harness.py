import asyncio
import json
from pathlib import Path

import pytest

from agent.evals.replay import (
    DEFAULT_SUITE,
    ReplayCase,
    ReplayRunner,
    load_replay_suite,
    main,
)


def test_default_persona_replay_suite_is_valid_and_unique():
    cases = load_replay_suite(DEFAULT_SUITE)

    assert len(cases) == 16
    assert len({case.case_id for case in cases}) == len(cases)
    assert {case.category for case in cases} == {
        "persona-structure",
        "session-migration",
        "session-continuity",
        "context-compression",
        "multi-agent-transcript",
    }


def test_default_persona_replay_suite_passes_without_external_model(tmp_path):
    cases = load_replay_suite(DEFAULT_SUITE)

    report = asyncio.run(ReplayRunner(tmp_path / "artifacts").run(cases, suite_path=DEFAULT_SUITE))

    assert report.passed is True
    assert report.passed_count == 16
    assert report.failed_count == 0
    stored = json.loads((tmp_path / "artifacts" / "report.json").read_text(encoding="utf-8"))
    assert stored["pass_rate"] == 1.0
    assert stored["results"][0]["id"] == cases[0].case_id


def test_replay_failure_contains_bounded_actual_value(tmp_path):
    case = ReplayCase.from_dict({
        "id": "intentional-failure",
        "category": "test",
        "profile": "lyra",
        "messages": [{"role": "user", "content": "hello"}],
        "assertions": [{"target": "persona_id", "op": "equals", "value": "different-profile"}],
    })

    result = asyncio.run(ReplayRunner(tmp_path).run_case(case))

    assert result.passed is False
    assert result.error == ""
    assert result.assertions[0].actual == "lyra"


def test_replay_suite_schema_rejects_unknown_fields_with_line_number(tmp_path):
    suite = tmp_path / "invalid.jsonl"
    suite.write_text(
        json.dumps({
            "id": "invalid",
            "category": "test",
            "profile": "lyra",
            "messages": [{"role": "user", "content": "hello"}],
            "assertions": [{"target": "persona_id", "op": "equals", "value": "lyra"}],
            "future_field": True,
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"invalid\.jsonl:1: unknown case field"):
        load_replay_suite(suite)


def test_replay_cli_supports_case_filter_and_writes_report(tmp_path):
    artifacts = tmp_path / "cli-artifacts"

    exit_code = main([
        "--suite",
        str(DEFAULT_SUITE),
        "--case",
        "legacy-work-session-migrates",
        "--artifacts",
        str(artifacts),
        "--json",
    ])

    assert exit_code == 0
    assert Path(artifacts / "report.json").is_file()
