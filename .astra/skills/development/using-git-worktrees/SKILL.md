---
name: using-git-worktrees
description: Use before substantial feature work or plan execution when repository changes should be isolated from the user's current checkout.
---

# Using Git Worktrees

## Safe Setup

1. Inspect `git rev-parse --git-dir`, `git rev-parse --git-common-dir`, the
   current branch, superproject state, worktree list, and dirty files. Do not
   create a nested worktree when already isolated.
2. Prefer an existing repository-local `.worktrees` directory. Before creation,
   verify it is ignored with `git check-ignore`; add and commit an ignore rule
   first if necessary.
3. Confirm the intended branch and path do not already exist. Derive both from
   the repository root and quote every path.
4. Create the isolated branch with `git worktree add <path> -b <branch>` using
   `execute_shell`. Preserve all user changes in the original checkout.
5. Detect the project's existing environment and dependency workflow rather
   than installing into a new global environment. Before editing, run the
   repository's `scripts/worktree_preflight.py` with the intended interpreter,
   `--workspace` set to the worktree, `--module agent.runtime.tools.delegate`,
   and `--require-package pytest`. Add `--check-tui` for TUI work. When applying
   this skill to another repository, use its equivalent checks instead of
   assuming Astra's script or module exists.
6. Resolve missing or incompatible dependencies through the existing project
   workflow. Do not blindly symlink `.venv` or `ui-tui/node_modules`. Reuse is
   acceptable only when manifests/locks match and the actual project imports
   resolve into the worktree. Do not install into a shared environment without
   considering other checkouts. Use the verified interpreter via `-m` from the
   worktree; console entry points may still load an editable main-checkout install.
7. Run relevant baseline tests inside the worktree before implementation. Run a
   full baseline when the scope calls for it. A background baseline must use an
   independent fixed snapshot; concurrent edits in its checkout invalidate the
   comparison. The lead runs the broader integrated regression after changes.
8. Before dispatching Team work, compare Team status `workspace_root` with the
   observed Git root and the absolute path returned by a member's `stat_file`
   call on a known file. The preflight script checks execution/import paths;
   it cannot prove a different agent's file-tool binding. Resolve any mismatch
   before assignment, with one concise readiness report.

Git commands and repository-relative paths are shared across macOS, native
Windows, and WSL. Do not hard-code home directories, drive letters, `/tmp`, or
shell-specific path expansion. When a command's syntax truly differs, provide
an explicit PowerShell or POSIX form without weakening either platform.

## Boundaries

If baseline tests fail, report the exact failure before implementing unless the
user already authorized investigating it. Worktree removal and branch deletion
are separate destructive actions: perform them only through the authorized
branch-finishing workflow and only after preserving needed commits.
