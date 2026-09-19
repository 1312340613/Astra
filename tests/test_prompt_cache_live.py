"""Key-gated live cache test following dsh's request-cache e2e method.

Set ASTRA_PROMPT_CACHE_LIVE_TEST=1 to run against the configured provider.
The first request may not hit cache; every later identical-prefix request
must report prompt_cache_hit_tokens > 0.
"""

import argparse
import asyncio
import os

import pytest

from scripts.prompt_cache_probe import run_probe


@pytest.mark.skipif(
    os.getenv("ASTRA_PROMPT_CACHE_LIVE_TEST") != "1",
    reason="set ASTRA_PROMPT_CACHE_LIVE_TEST=1 to run the live provider cache test",
)
@pytest.mark.allow_application_environment
def test_tail_stable_prefix_hits_provider_cache_on_second_request(tmp_path):
    args = argparse.Namespace(
        model_key="",
        stable_units=160,
        max_tokens=16,
        request_interval=1.0,
        output=tmp_path / "prompt-cache-probe.json",
    )
    report = asyncio.run(run_probe(args))

    attempts = report["variants"]["tail-system-message"]
    assert len(attempts) == 2
    assert all(attempt["accepted"] for attempt in attempts)
    assert attempts[0]["usage"]["prompt_cache_hit_tokens"] == 0
    assert attempts[1]["usage"]["prompt_cache_hit_tokens"] > 0
    assert attempts[1]["cache_hit_rate"] > 0
