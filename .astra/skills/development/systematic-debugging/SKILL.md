---
name: systematic-debugging
description: Use when diagnosing a bug, failing test, crash, timeout, regression, or other unexpected behavior whose root cause is not yet established.
---

# Systematic Debugging

## Core Principle

Find the root cause before changing behavior. Evidence from the failing system
is more reliable than a plausible guess.

## Investigation

1. Preserve the exact symptom: command, input, environment, exit status, and
   complete relevant error output.
2. Reproduce consistently or state why reproduction is unavailable. Separate
   product failure from sandbox, network, credential, or test-harness limits.
3. Establish the nearest working and failing boundaries across versions,
   platforms, components, and data flow.
4. Read the involved code and trace the bad value or state backward to where it
   first becomes incorrect. Instrument boundaries when the existing evidence
   is insufficient.
5. State one falsifiable root-cause hypothesis and the observation that would
   distinguish it from alternatives.
6. Run the smallest discriminating check. If it disproves the hypothesis,
   discard it rather than stacking another speculative change.

## Fix and Verification

After the cause is supported by evidence, call `skill_view` for
`test-driven-development` before implementing a behavior change. Add a focused
regression test, observe the expected failure, make the smallest causal fix,
and rerun both the reproduction and relevant regressions.

Use `read_file` and read-only `execute_shell` commands during diagnosis. Do not
edit merely because the user asked for an explanation or diagnosis. If a fix
is requested, preserve unrelated dirty changes and platform compatibility.

## Stop Conditions

Stop and report the evidence when required access, inputs, or reproduction are
unavailable. Three failed speculative fixes indicate the investigation model
is wrong: return to the boundaries and collect new evidence.
