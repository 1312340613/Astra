---
name: verification-before-completion
description: Use before claiming work is complete, fixed, passing, committed, merged, pushed, deployed, or otherwise ready for handoff.
---

# Verification Before Completion

## Evidence

Support completion claims with evidence that still applies to the work being
delivered. Evidence validity depends on the relevant code, dependencies,
configuration, and environment, not on whether it came from this reply.

1. Identify the claims that matter and the evidence needed to support them.
2. Reuse completed checks when their scope still covers the current state.
   Run the affected checks when relevant inputs changed, a failure remains
   unresolved, evidence is missing, or the user explicitly requests a rerun.
3. Read the exit status and the relevant output, including failure,
   warning, skip, and test counts.
4. Compare the evidence to the requirements. If it does not prove the claim,
   report the actual state and the missing verification.
5. State the result with the important evidence boundaries.

## Proportionate Checks

- Check a coherent batch of changes; do not run a suite after every edit.
- During implementation and debugging, prefer focused regressions. Run broader
  gates at integration or release when required by the project or change impact.
- For comments, prose, and other low-impact changes, a diff or content review
  may be sufficient; do not invent unrelated test work.
- A new reply, commit, subagent handoff, or unchanged review does not itself
  invalidate results. Inspect a subagent's diff and concrete evidence, and
  rerun only when the coverage or evidence is insufficient.
- Once the needed checks pass, deliver. Expand verification only for a new
  relevant change, failure, or unresolved concern.

## Claim Map

| Claim | Required evidence |
| --- | --- |
| File exists | observation of the exact path |
| Focused tests pass | applicable focused run with zero failures |
| Full suite passes | applicable full-suite run with zero failures |
| Bug fixed | original reproduction plus regression test |
| Committed | `git status` and `git log` showing the intended commit |
| Merged | branch and commit ancestry on the target branch |
| Pushed | fetch plus zero local/remote divergence for the target branch |

Passing lint does not prove tests or builds. Saved output can document a prior
run; do not describe it as a new execution. A file's presence does not prove
correctness. A subagent's unsupported completion claim is not test evidence.

For repository handoff, account for the relevant diff, local changes, and
platform-specific verification gaps. Never hide a known baseline failure;
distinguish it from regressions introduced by the change. Report material gaps
without turning inapplicable checks into a checklist of excuses.
