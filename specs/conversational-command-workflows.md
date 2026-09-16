# Conversational command workflows

Status: implemented.

## Outcome

Commands that require judgment start ordinary, persisted agent conversations.
Their evidence and recommendations remain available when the user replies with
questions, corrections, a narrower scope, or permission to proceed. The main
model and its configured reasoning effort perform the work.

Command expansion is a small shared adapter, not a separate agent, scheduler,
approval queue, or general workflow engine. Existing operation implementations
remain responsible for validation and storage.

## Command contract

| Entry | Behavior |
| --- | --- |
| `/learn review [skill-name]` | Read an owned automatic-skill batch, explain concrete proposed edits and any useful verification, then finish the read-only turn. A subsequent user reply can authorize selected edits and the presented verification. |
| `/doctor [section or symptom]` | Read current diagnostic evidence, explain findings and next checks, and propose repairs. The initial diagnostic turn is read-only. |
| `/diagnostics [section]` | Explain a fresh runtime snapshot in conversation. `/diagnostics json` remains a machine-readable query. |
| `/conclave <question>` | Invoke the existing research tool inside the normal conversation. Retain its report for follow-up discussion without automatically repeating the research. |
| `/skills create [name] [description]` | Inspect related skills and propose a complete user-owned skill, asking only for missing information. The initial turn drafts without saving; the next user reply can authorize saving and verification. |
| `/memory review [query]` | Read current memory records with stable identifiers and provenance, explain obsolete or conflicting claims, and propose exact corrections. Initial turn is read-only. Existing precise memory commands remain available. |
| `/handoff [notes]` | Read the deterministic handoff evidence, compose a useful handoff with completed work, decisions, pending work, and acceptance limits, then save through the redacting handoff writer. Support normal follow-up revisions. |

`/doctor --raw`, `/diagnostics --raw`, and `/handoff --raw` retain direct
operations. `/skills create --template <name> <description>` retains the blank
template operation. Configuration, status, history, undo, reset, cancellation,
session switching, and maintenance commands keep their existing semantics.

## Shared conversation entry

A shared parser builds an explicitly labeled, scoped workflow message containing
the original user command and concise built-in instructions. It is admitted using
the existing task/stream/cancellation path in both the Ink backend and plain CLI.
The UI shows the original command; persisted model context retains the expansion
and evidence. Restored history shows the short command rather than the entire
template. Inputs retain their original case and quoting.

Review, diagnostic, memory-review, and skill-drafting turns have a runtime tool
allowlist. They cannot execute shell/Python, modify files or memory, delegate to
an unrestricted agent, or approve and apply their own proposals in that turn.
This restriction is released in a finally block on completion, cancellation, or
failure. Existing mode restrictions intersect with it; workflows are unavailable
inside conversational modes that intentionally disable work tools.

The model ends a proposal turn so the next normal user message controls what
happens next. A reply of “continue” covers the concrete plan just presented;
prompts must not create repeated confirmations within that scope. Existing tool
permission checks continue to apply. A scheduled wakeup or goal continuation must
not be used to self-authorize proposal execution.

## Skill review operations

Expose read-only snapshots and a separate application operation. Reuse the
existing automatic ownership checks, bounded batches, exact patches, merge-file
conflict checks, transaction journal, history, and undo.

A snapshot has a random identifier, an owning session, the original files, and
source evidence. Keep a bounded number in memory. Applying requires a later user
turn in the same session and unchanged original files. There is no lock held
while waiting for the user. After restart or snapshot eviction, reread and review
the current files before applying. Apply consumes the snapshot once. Decisions
must cover the batch, using keep for declined changes. No independent reviewer
model call is made. The proposal distinguishes content review from live testing.

Verification uses normal tools on the subsequent user-authorized turn. Use a
temporary example where suitable, state inputs and expected observable results,
and report the actual checks and uncovered limits. Content approval alone must
never be reported as successful execution.

## Other operation adapters

- Diagnostic reads call existing doctor/runtime builders on the live runtime.
  Reports are evidence, not instructions, and failures remain visible.
- Memory inspection returns current stable IDs, sources, and content. Corrections
  compare the previously inspected content before superseding it, preventing a
  stale proposal from overwriting a changed record. Core and structured memories
  retain their respective existing semantics.
- Conclave uses its existing agent-facing tool and configured search provider.
  Configuration commands remain direct operations.
- Handoff reads preserve deterministic task evidence. Saves use the existing
  bounded destination and redaction, without exposing arbitrary file paths.
Automatic exit handoffs remain deterministic.
- Skill drafting reuses `skills_list`, `skill_view`, and `skill_manage` with
  `origin=user` for user-requested skills.

## Validation

Test command parsing and direct-operation escape hatches, both frontend routes,
normal streaming and follow-up context, read-only enforcement (including a model
attempting an unadvertised write), restriction cleanup on cancellation, snapshot
session/turn/version boundaries, partial user selection, history and undo, stale
memory correction rejection, and handoff redaction.

Run relevant Python tests, frontend interaction tests and build, static checks,
then the complete Python suite because the turn-scoped tool restriction touches
the agent loop. Automated provider fixtures establish routing and safety
contracts; interactive live-model quality remains a separate acceptance check.

## Delivery sequence

1. Shared parser, turn scope, and frontend admission.
2. Skill snapshots/application and the six workflow prompts/tool adapters.
3. Help, completion, documentation, focused tests and broad regression checks.
4. Review the final diff, integrate the isolated branch, preserve unrelated work,
   and report validation plus any unrun live-model acceptance.

## Self-review

The initial proposal/next-user-turn boundary is consistent across review and
drafting workflows. Handoff creation is directly authorized by its command and
therefore saves without an additional proposal gate. Raw diagnostics and exact
operations remain available. No new service or external framework is required.
