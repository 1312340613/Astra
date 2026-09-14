from scripts.prompt_cache_probe import build_variants


def test_prompt_cache_probe_builds_paired_current_and_tail_shapes():
    variants = build_variants("run", stable_units=64)

    current_first, current_second = variants["current-system-suffix"]
    assert current_first[0]["role"] == "system"
    assert current_first[0]["content"] != current_second[0]["content"]
    assert "nonce=A" in current_first[0]["content"]
    assert "nonce=B" in current_second[0]["content"]

    tail_first, tail_second = variants["tail-system-message"]
    assert tail_first[0] == tail_second[0]
    assert [message["role"] for message in tail_first] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
    ]
    assert tail_first[3]["content"] != tail_second[3]["content"]
