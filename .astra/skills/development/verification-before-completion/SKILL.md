---
name: verification-before-completion
description: Use before claiming work is complete, fixed, passing, committed, merged, pushed, deployed, or otherwise ready for handoff.
---

# Verification Before Completion

## Evidence Gate

Every completion claim needs fresh evidence from the command that directly
proves it.

1. Identify each claim and the command that proves that exact claim.
2. Run the complete command now; do not rely on an earlier run or another
   agent's summary.
3. Read the exit status and the full relevant output, including failure,
   warning, skip, and test counts.
4. Compare the evidence to the requirements. If it does not prove the claim,
   report the actual state and the missing verification.
5. Only then state the result, together with the evidence boundary.

## Claim Map

| Claim | Required evidence |
| --- | --- |
| File exists | exact path and repository status |
| Focused tests pass | fresh focused command with zero failures |
| Full suite passes | fresh full-suite command with zero failures |
| Bug fixed | original reproduction plus regression test |
| Committed | `git status` and `git log` showing the intended commit |
| Merged | branch and commit ancestry on the target branch |
| Pushed | fetch plus zero local/remote divergence for the target branch |

Passing lint does not prove tests or builds. Saved output does not prove fresh
execution. A file's presence does not prove correctness. A subagent report does
not replace inspecting its diff and rerunning appropriate checks.

Before handoff, also inspect unintended files, dirty state, ignored local
artifacts, and platform-specific verification gaps. Never hide a known
baseline failure; distinguish it from regressions introduced by the change.
