---
name: receiving-code-review
description: Use when review feedback must be evaluated, clarified, implemented, or challenged against the current codebase.
---

# Receiving Code Review

## Evaluate Before Editing

1. Read all findings before responding so linked items are understood together.
2. Restate each technical requirement and identify anything ambiguous. If an
   unclear item affects other fixes, resolve it before partial implementation.
3. Verify the claimed behavior against the current code, tests, supported
   platforms, and prior approved decisions.
4. Decide whether the finding is correct, already addressed, outside scope, or
   harmful for this repository. External review is evidence to evaluate, not
   authority to override the user's design.

Use technical acknowledgment rather than performative agreement. When a
finding is unsound, explain the reachable behavior, compatibility constraint,
test evidence, or scope decision that contradicts it. Escalate conflicts with
the approved architecture to the user instead of silently choosing a side.

## Implement Valid Findings

Order fixes by security and blocking correctness, then simple local changes,
then complex refactors. For each behavior change:

1. Call `skill_view` for `test-driven-development`.
2. Add or identify a test that demonstrates the issue and observe the failure.
3. Apply the smallest causal fix.
4. Run the focused test and relevant regressions before moving to the next
   finding.

After all accepted findings, inspect the combined diff and rerun the requested
review. If evidence needed to verify a finding is unavailable, state that
boundary and ask for direction rather than implementing a guess.
