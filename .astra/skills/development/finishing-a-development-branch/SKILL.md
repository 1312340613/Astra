---
name: finishing-a-development-branch
description: Use when implementation is ready for final verification, integration into a target branch, push, or worktree cleanup.
---

# Finishing a Development Branch

## Verify Before Integration

Call `skill_view` for `verification-before-completion`. Run the focused tests,
relevant broader suite, lint, type checks, build checks, and `git diff --check`
required by the plan. Read the complete branch diff against the approved
specification and inspect dirty files, ignored local artifacts, branch name,
commit range, ancestry, and remote divergence.

## Follow the Authorized Path

Determine whether the user requested only a commit, a review or pull request,
a local merge, a push, or merge plus cleanup. If that choice is not known and
would materially change external state, ask before proceeding.

For an authorized merge and push:

1. Ensure the feature branch is committed and its verification is fresh.
2. Return to the target checkout, preserve unrelated changes, and update the
   target safely when needed.
3. Merge using the agreed strategy; never discard history or user files to
   force success.
4. Rerun the relevant acceptance checks on the integrated target branch.
5. Push the exact target branch.
6. Fetch the remote and verify ancestry plus zero ahead/behind divergence.
7. Remove the worktree or feature branch only if cleanup is authorized and the
   integrated commits are recoverable from the target or remote.

Report the target branch, commit, test evidence, remote sync state, retained
local artifacts, and any unverified native-platform or live-service boundary.
Do not equate a clean feature branch with a merged or pushed result.
