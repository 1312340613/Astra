from agent.runtime.micro_compact import micro_compact_tool_results


def _chain(call_id: str, name: str, content: str):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "name": name, "arguments": "{}"}],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def test_microcompact_clears_only_old_read_results():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "old"}]
    messages += _chain("old-read", "read_file", "A" * 2_000)
    messages += _chain("recent-old-read", "read_file", "D" * 2_000)
    messages += _chain("old-write", "apply_patch", "B" * 2_000)
    messages += [{"role": "assistant", "content": "done"}, {"role": "user", "content": "current"}]
    messages += _chain("current-read", "read_file", "C" * 2_000)

    compacted, stats = micro_compact_tool_results(
        messages,
        tool_risk=lambda name: "write" if name == "apply_patch" else "read",
        char_budget=1_000,
        keep_recent=1,
    )

    by_id = {message.get("tool_call_id"): message for message in compacted if message.get("role") == "tool"}
    assert "Old tool result content cleared" in by_id["old-read"]["content"]
    assert by_id["recent-old-read"]["content"] == "D" * 2_000
    assert by_id["old-write"]["content"] == "B" * 2_000
    assert by_id["current-read"]["content"] == "C" * 2_000
    assert stats.cleared_results == 1
    assert stats.shadowed_tokens > stats.replacement_tokens > 0
    assert stats.saved_tokens == stats.shadowed_tokens - stats.replacement_tokens
    assert messages[3]["content"] == "A" * 2_000  # durable input is untouched


def test_microcompact_preserves_error_results_and_protocol():
    messages = [{"role": "user", "content": "old"}]
    messages += _chain("failed", "read_file", "Status: error\nTraceback\n" + "X" * 3_000)
    messages += _chain("safe", "read_file", "Y" * 3_000)
    messages += [{"role": "user", "content": "current"}]

    compacted, stats = micro_compact_tool_results(
        messages,
        char_budget=1_000,
        keep_recent=1,
    )

    tool_ids = [message.get("tool_call_id") for message in compacted if message.get("role") == "tool"]
    assert tool_ids == ["failed", "safe"]
    assert compacted[2]["content"].startswith("Status: error")
    assert stats.cleared_results == 0  # safe is the recent compactable result


def test_microcompact_ignores_transient_system_user_overlay():
    messages = [{"role": "user", "content": "current"}]
    messages += _chain("current", "read_file", "Z" * 4_000)
    messages.append({"role": "user", "content": "[SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]\nverify"})

    compacted, stats = micro_compact_tool_results(
        messages,
        char_budget=500,
        keep_recent=1,
    )
    assert compacted[2]["content"] == "Z" * 4_000
    assert stats.cleared_results == 0


def _compact_process(payload, tool="process_read"):
    import json
    raw = json.dumps(payload, ensure_ascii=False)
    messages = [{"role": "user", "content": "old"}]
    messages += _chain("process", tool, raw)
    messages += _chain("recent", "read_file", "Z" * 2000)
    messages += [{"role": "user", "content": "continue"}]
    compacted, stats = micro_compact_tool_results(messages, char_budget=1000, keep_recent=1)
    assert messages[2]["content"] == raw
    return compacted[2]["content"], stats


def test_process_stamp_keeps_status_exact_byte_range_and_reread_arguments():
    import json
    text = "开始\n" + "中" * 4000 + "\n12 passed"
    payload = {"process_id": "p1", "status": "completed", "exit_code": 0,
                   "artifact_path": "/tmp/log with spaces", "stream": "stderr",
                   "byte_offset": 123, "next_byte_offset": 123 + len(text.encode()),
                   "content": text, "eof": True}
    result, stats = _compact_process(payload)
    stamp = json.loads(result.split("\n", 1)[1])
    assert stamp["status"] == "completed" and stamp["exit_code"] == 0
    assert stamp["artifact_path"] == "/tmp/log with spaces"
    assert stamp["next_byte_offset"] == payload["next_byte_offset"]
    assert stamp["reread"]["arguments"] == {"process_id": "p1", "stream": "stderr",
                                               "byte_offset": 123, "max_chars": len(text)}
    assert "12 passed" in stamp["output_excerpt"]
    assert stats.saved_chars > 0


def test_process_errors_are_not_cleared_even_when_json_quoted():
    for payload in [
        {"process_id": "p", "status": "failed", "exit_code": 1, "content": "X" * 5000},
        {"process_id": "p", "status": "completed", "exit_code": 2, "content": "X" * 5000},
    ]:
        result, stats = _compact_process(payload)
        assert stats.cleared_results == 0
        assert "X" * 5000 in result


def test_poll_stamp_keeps_observed_running_state_and_does_not_invent_output():
    import json
    result, stats = _compact_process({"process_id": "p", "status": "running",
        "exit_code": None, "artifact_path": "/tmp/out", "metadata": {"large": "x" * 5000}},
        "process_poll")
    stamp = json.loads(result.split("\n", 1)[1])
    assert stamp["status"] == "running" and stamp["exit_code"] is None
    assert "output_excerpt" not in stamp and "reread" not in stamp
    assert stats.saved_chars > 0
