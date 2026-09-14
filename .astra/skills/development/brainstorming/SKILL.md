---
name: brainstorming
description: Use when a requested feature, component, or behavior change needs its intent, constraints, or design resolved before implementation.
---

# Brainstorming

## Purpose

Turn an initial idea into an approved written design. Requirements discovery is
read-only; approval of the design is a separate gate from permission to edit,
merge, push, or perform other risky actions.

## Workflow

1. Inspect the repository, relevant documentation, and current behavior before
   proposing changes.
2. Identify the goal, users, constraints, success criteria, compatibility
   requirements, and explicit non-goals.
3. Ask one material question at a time. Use `ask_user_question` when structured
   choices help; it must be the only tool call in that assistant step. If it is
   unavailable, ask in ordinary prose.
4. Present two or three viable approaches when a real tradeoff exists. Lead
   with a recommendation and explain cost, risk, and compatibility briefly.
5. Present the proposed design in reviewable sections: scope, architecture,
   data or control flow, error handling, testing, rollout, and non-goals.
6. Obtain explicit approval of the complete design. A clarification answer
   updates requirements; it does not approve later writes.
7. Save the approved specification under the repository's design-doc
   convention and review it against the conversation.
8. Before producing an implementation plan, call `skill_view` for
   `writing-plans` and follow it.

## Boundaries

- Do not implement while the design is unresolved.
- Do not invent requirements that materially expand scope.
- Do not ask questions whose answers are cheaply discoverable from local
  evidence.
- Keep a simple change simple; not every task needs multiple alternatives or a
  large document.
