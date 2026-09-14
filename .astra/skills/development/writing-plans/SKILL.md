---
name: writing-plans
description: Use when an approved design or fixed requirements need to be converted into a multi-step implementation plan before code changes begin.
---

# Writing Plans

## Purpose

Create an executable implementation plan for an engineer who knows little
about the repository. Planning begins only after the complete design is
explicitly approved.

## Plan Contract

1. Read the approved specification and inspect the current files, tests,
   commands, and repository conventions it depends on.
2. Map every created, modified, and tested file to one clear responsibility.
3. Record global constraints exactly: compatibility, dependencies, naming,
   permissions, non-goals, and integration boundaries.
4. Split work into the smallest independently testable tasks. Each behavior
   change follows `test-driven-development`: failing test, observed RED,
   minimal implementation, observed GREEN, regressions, then commit.
5. For every task provide exact paths, consumed and produced interfaces,
   concrete edits, commands, expected results, and a focused commit message.
6. Include final lint, type, test, diff, Git, merge, and push verification that
   matches the authorized delivery scope.
7. Save the plan at `docs/superpowers/plans/YYYY-MM-DD-<feature>.md` unless the
   repository defines another convention.

## Self-review

Compare the finished plan line by line with the specification. Remove
placeholders such as “later,” “appropriate handling,” or unspecified tests.
Check that names and interfaces remain consistent across tasks and that every
requirement maps to an implementation or verification step.

Before inline implementation, call `skill_view` for `executing-plans`. Do not
treat writing the plan as authorization for risky actions not already approved.
