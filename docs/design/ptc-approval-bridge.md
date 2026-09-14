# Approvals inside programmatic tool calls

A program running through `run_code` can call Astra tools and wait for their
normal approval decision. Native and programmatic calls share one policy and
approval pipeline; the program does not receive a bypass because it runs inside
a subprocess.

## Request flow

1. The program's tool wrapper sends a request over the existing subprocess RPC.
2. The host routes it through the normal tool execution, permission and approval
   checks. The request can suspend while waiting for a decision.
3. A granted request runs and returns its result. Denial, missing approval,
   cancellation and execution failure remain errors returned to the program.

The subprocess wrapper waits on a per-request future while its reader continues
processing replies. The host handles requests asynchronously, so one pending
approval does not require a second approval channel or a separate UI.

## Boundaries to preserve

- Docker/subprocess isolation remains in place; approval handling does not move
  model-authored code into the host process.
- Approval waits consume the enclosing execution deadline. An unanswered request
  does not acquire an unlimited lifetime.
- Session grants use the existing permission scope. Repeat guards and current
  policy still apply to later calls.
- Denied or unknown actions are not permission to retry through another tool.
  Existing native fallback exposure must still pass the normal authorization
  and execution checks.
- Cancellation and timeout clean up pending approval state through the same
  inbox lifecycle used by ordinary calls.

The `tests/test_code_mode.py` and related code-mode/approval tests cover granted,
denied, missing and session-scoped approvals, dynamic path checks and cancellation.
Single-run pass counts belong in test reports rather than this contract.
