# Worker runtime and Team lifecycle

Astra delegates bounded work through `delegate_task` and coordinates named Team
members through `team_spawn`, messaging and the task board. The current contracts
live in `agent/runtime/worker.py`, `tools/delegate.py`, `agent_team.py` and
`team_budget.py`; this guide replaces the earlier phase-by-phase implementation
plan.

## Capabilities and isolation

`mode="explorer"` exposes the permitted read-only tool surface.
`mode="worker"` may edit workspace files and run checks through the existing
sandbox and approval policy. Explicit tool subsets cannot enlarge that surface.
Eligible CodeGraph tools can be included when configured; they are not a required
service. Policy, approval scopes and YOLO state remain shared with the parent.

Shared workers operate in the selected workspace. `isolation="worktree"` creates
an isolated Git checkout; changed work must be reviewed and integrated. Parallel
workers should own disjoint files. A worktree is file isolation, not a separate
OS security boundary.

## Run contract

`WorkerSpec` fixes the goal, supplied context, allowed tools, model overrides,
turn/time limits and optional Team/workspace identity. Its public metadata omits
the full supplied context.

`WorkerRun` accompanies the existing process result with a run ID, outcome,
timing, turn usage, remaining allowance and error code. Completion, partial work,
failure, cancellation and timeout are distinct outcomes. Poll/read/cancel refer
to that existing run; they do not launch its goal again. Saved output is evidence
of what the worker reported, not independent verification of its conclusions.

Normal delegates default to 12 model turns and 120 seconds. The maximum is 50
turns and 1,800 seconds. Team members default to 50 turns, including a reserved
final-report turn. Counts refer to model responses, not individual tool calls.

## Team continuity and budgets

A Team member requested with `keep_alive=true` can remain idle between
assignments. The initial result may report pending readiness; use the later Team
status to establish that it is actually available. A new assignment does not
silently replenish the member's cumulative turn allowance.

An idle episode publishes its report once to the parent's durable delegate
inbox. The parent can finish its turn while that member remains alive; queued
messages and active episodes still count as work to join. `delegate_poll` waits
for current work to report and returns `episode_result` with worker status
`idle`, while the process status remains `running`. Normal idle expiry does not
repeat a delivered report. Cancellation still reaps idle members, and a later
failure remains observable. A new parent turn can resume the Team and assign
work to the same retained member with its existing conversation history.

Runtime notices expose total usage, remaining turns and episode usage. An episode
estimate is a planning target, not extra allowance. The task board and episode
records retain actual consumption and outcomes for later estimates. Report
remaining work and evidence before exhaustion instead of beginning another broad
investigation with insufficient budget.

Worktree startup needs a verified tool root and usable project dependencies.
Missing dependencies are environment failures, not proof of a code regression.
Use the repository's Team and worktree Skills for the operational workflow.

## Cancellation and recovery

Cancellation stops further work and requests cleanup of the owned process.
Results from already dispatched external actions may still need inspection;
process termination does not prove that those actions were undone. Persisted
process/Team records support inspection and recovery, but do not serialize a
live model coroutine for automatic continuation after a host restart.

`team_restart` re-seeds any terminal member, including a completed one, as a
new worker from its durable spawn spec and transcript tail, and re-applies a
`keep_alive` request recorded at spawn so a revived retained member returns
to idle after its recovery episode.

Relevant regressions cover delegation, Team state and budgets, process lifecycle,
approval propagation, workspace isolation, mailbox delivery and interrupted
results. Use fresh evidence when validating another platform or provider.
