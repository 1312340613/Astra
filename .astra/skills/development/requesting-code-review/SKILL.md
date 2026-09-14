---
name: requesting-code-review
description: Use when implementation or a substantial change needs an evidence-backed review before integration, handoff, or release.
---

# Requesting Code Review

## Review Package

Establish the approved specification, target branch, merge base, commit range,
and complete diff. Include focused test evidence and known verification limits.
Do not review only the last commit when the feature spans several commits.

## Review Method

1. Compare every requirement and non-goal with the actual diff.
2. Inspect correctness, regressions, error handling, security and permission
   boundaries, data loss risk, portability, test quality, and unnecessary scope.
3. Trace each suspected issue through current code and tests before reporting
   it. A plausible concern without a reachable failure path is a question, not
   a confirmed defect.
4. Classify findings by impact: blocking correctness or security, important
   behavior or maintainability, then minor polish. Give an exact path and the
   smallest useful evidence for every actionable finding.
5. Run targeted read-only checks when they distinguish a real issue from a
   false positive. Do not mutate code during a review-only request.

When isolated context materially improves a broad review, `delegate_task` may
run a bounded `mode=explorer` review. Delegation is optional: do the review
locally for small diffs or when a subagent would duplicate the critical path.
Always verify returned findings against the repository yourself.

If fixes are also requested, call `skill_view` for `receiving-code-review`
before changing code. After fixes, rerun the review of the full feature range;
do not treat a clean task-level review as whole-branch approval.
