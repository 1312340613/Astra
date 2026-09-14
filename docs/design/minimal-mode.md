# Minimal mode

`/minimal` opens an isolated coding session with a small, fixed tool surface.
The ordinary work session is parked and restored on leaving. The controller is
`agent/runtime/minimal_mode.py`; command routing and session storage retain the
same separation used by the other isolated modes.

## User commands

```text
/minimal
/minimal new
/minimal status
/minimal sessions
/minimal leave
```

The session uses the fixed system prompt `You are a helpful software engineer
assistant.` and disables ordinary work-mode memory, skill catalogs, project
instruction refresh and compaction. It does not promise that a smaller prompt
improves the selected model's answer quality.

## Tool and sandbox boundaries

| Tool | Execution path |
| --- | --- |
| `bash` | Persistent POSIX PTY on macOS/Linux; WSL PTY on Windows |
| `str_replace_editor` | The restricted editor operations |
| `run_code` | Docker-isolated Python programmatic tool calls |
| `search_web`, `web_extract` | The explicitly allowed read-only network tools |

The allowlist stays fixed; group activation cannot expand it. Host shell commands
still use Astra's command policy and approval checks. `run_code` continues using
Docker even though Bash uses a host/WSL terminal. Entering Minimal mode does not
change the user's saved sandbox default.

The Bash environment and current directory persist across calls. Exit, timeout,
reset and mode cleanup can destroy that shell. `ASTRA_PERSISTENT_BASH_TIMEOUT`
configures its command limit, with `ASTRA_WSL_PERSISTENT_TIMEOUT` retained as a
legacy fallback. Tool output limits and errors still apply.

## Maintenance

Entering parks the normal context and runtime fields; switching Minimal sessions
reapplies its restrictions; leaving saves the isolated session and restores the
parked work state and sandbox selection. New per-turn injection paths must
respect the mode's guards. Sessions stay under `.sessions/minimal/` rather than
appearing as ordinary work conversations.

The existing tests in `tests/test_minimal_mode.py`, `tests/test_minimal_tools.py`
and `tests/test_wsl_pty.py` cover isolation, restoration and platform shell
behavior. For approvals inside `run_code`, see the
[PTC approval bridge](ptc-approval-bridge.md).
