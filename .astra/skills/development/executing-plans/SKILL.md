---
name: executing-plans
description: Use when a written implementation plan is ready to be carried out task by task in the current Astra session.
---

# Executing Plans

## Preflight

Read the complete plan and its approved specification. Check for contradictions,
missing interfaces, stale paths, unexpected dirty changes, and actions outside
the user's authority. Raise one consolidated question only when ambiguity
materially changes the implementation.

For substantial work, call `skill_view` for `using-git-worktrees` and establish
an isolated workspace before edits. Run relevant baseline tests so new failures
can be distinguished from existing ones.

## Choose the Execution Model

For every approved multi-step development plan, read
`team-tdd-development` with `skill_view` and follow it as the default execution
model. A multi-step plan has at least two dependency-ordered repository changes
or independently accepted plan tasks.

Keep one bounded change inline with `test-driven-development`. Use
`subagent-driven-development` only when the user explicitly asks for
disposable one-shot delegates without context continuation.

## Execution

1. Use `plan_update` for 2-12 concrete steps. Keep exactly one step
   `in_progress` while work remains and resend the complete list after each
   state change.
2. Execute tasks in dependency order. Before each behavior change, read
   `test-driven-development` with `skill_view` and observe the planned RED and
   GREEN evidence.
3. Keep changes within the current task, preserve unrelated work, and commit at
   the task boundary only after its checks pass.
4. Continue without routine “should I continue?” pauses. Stop for a genuine
   blocker, unavailable authority, destructive ambiguity, or a plan conflict
   that cannot be resolved from repository evidence.
5. After all tasks, inspect the complete branch diff against the specification,
   not only individual commits.

Before declaring completion, call `skill_view` for
`verification-before-completion`. Then read `finishing-a-development-branch`
before merge, push, branch deletion, or worktree cleanup.
