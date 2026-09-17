# Stable runtime context prefixes

The approved scope is to adapt DeepSeek Harness's append-only runtime context
snapshots to Astra. Today each request prepends a transient block to the latest
user message. On the next turn that block disappears from the earlier message;
loading a Skill also rewrites that message. Both break reusable request prefixes.

## Decision

Keep canonical user/assistant/tool messages unchanged. Store a separate runtime
context projection in session metadata, with snapshots anchored to fingerprints
of canonical history. A changed memory/runtime/path-rule/Skill snapshot appends a
complete replacement after completed history. An unchanged snapshot adds nothing.
Clearing context appends an explicit supersession notice. Restoring a session
restores these anchors; compaction or history repair rebases them to current
history. Snapshots cannot split an assistant tool-call/result chain.

Alternatives were persisting injected user bodies (pollutes canonical history),
or moving all dynamic context into the system message (invalidates the earliest
prefix). Separate append-only projection preserves current session contracts and
works without requiring provider support for in-history system messages.

## Boundaries

- Existing provider-specific system prompt projection remains unchanged.
- Interaction modes retain their adjacent runtime-state layout and do not replay
  work-mode snapshots. Request-local observations, images and transient steering
  retain their current lifetime; they are not persisted in this projection.
- Include retained snapshots in token estimates and compaction admission. Bound
  snapshot count and drop superseded snapshots when budget pressure warrants it.
- Appshot admission/rollback and staged session loading use detached projections.
- Extend opt-in query diagnostics with content-free first-difference location
  and kind. Do not turn on profiling or change user settings automatically.
- No global change to tool-result retention, compression thresholds or cache TTL.

## Acceptance

Capture actual fake-provider requests across user turns, Skill activation/update,
and session save/restore. Unchanged request history must remain an exact prefix;
canonical user bodies and tool protocol must remain intact. Test explicit clear,
malformed saved state, history rewrites, budget pressure, preview isolation, and
ephemeral data exclusion. Run the affected runtime/session/profiler/Appshot and
interaction tests, lint and type checks. Offline tests establish request prefix
stability; actual cloud cache hit rates require subsequent real usage.

References: deepseek-ai/deepseek-harness runtime-context.ts and agent-loop README;
DeepSeek API context caching guide. Local comparison used upstream fb2c4b9e and
the official master implementation checked during the investigation.

Self-review: scope, authority, persistence, reset, budgeting and preview boundaries
are specified; no new provider dependencies or architecture approval are needed.
