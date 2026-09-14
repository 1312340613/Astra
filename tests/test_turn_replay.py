import asyncio
import json
import os

import pytest

from agent.evals.turn_replay import DEFAULT_SUITE, load_suite, run_case


@pytest.mark.parametrize("case", load_suite().cases, ids=lambda c: c.id)
def test_provider_to_session_fault_replay(tmp_path, case):
    if case.browser and os.getenv("ASTRA_BROWSER_REPLAY_E2E") != "1":
        pytest.skip("Set ASTRA_BROWSER_REPLAY_E2E=1 for isolated real Chromium replay")
    report = asyncio.run(run_case(case, tmp_path))
    assert report["passed"], json.dumps(report, indent=2)


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(schema_version=2),
    lambda data: data.update(api_key="should-never-be-loaded"),
    lambda data: data["cases"][0].update(script="no execution"),
    lambda data: data["cases"][0]["responses"][0]["frames"][0].update(delay_ms=999999),
    lambda data: data["cases"].append(data["cases"][0]),
])
def test_replay_is_versioned_and_rejects_unknown_controls(tmp_path, mutation):
    data = json.loads(DEFAULT_SUITE.read_text())
    mutation(data)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_suite(path)


def test_replay_refuses_unbounded_input_and_live_tools(tmp_path):
    path = tmp_path / "large.json"
    path.write_text(" " * 512001)
    with pytest.raises(ValueError, match="512 KB"):
        load_suite(path)
    case = load_suite().cases[0].model_copy(deep=True)
    case.responses[0].frames[0].calls[0].name = "execute_shell"
    with pytest.raises(ValueError, match="offline replay fixture"):
        asyncio.run(run_case(case, tmp_path))
