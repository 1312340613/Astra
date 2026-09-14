---
name: test-driven-development
description: Use when implementing a feature, bug fix, refactor, or other executable behavior change before writing production code.
---

# Test-Driven Development

## Rule

No production behavior change without first observing a test fail for the
expected reason. A test written after the implementation cannot prove that it
would have detected the missing behavior.

## RED-GREEN-REFACTOR

1. Define one externally meaningful behavior and the smallest test that proves
   it.
2. Add the test before production code.
3. Run the narrow test and confirm it fails because the behavior is missing,
   not because of a typo, bad fixture, or unavailable environment.
4. Implement the minimum change needed to pass. Avoid unrelated cleanup and
   speculative options.
5. Run the narrow test again and inspect its exit status and output.
6. Run relevant regression tests. Refactor only while the suite stays green.
7. Repeat for the next behavior.

If production code was written first, remove that change and restart with the
test. Do not keep it as a template while claiming a test-first cycle.

## Appropriate Exceptions

Pure documentation or declarative configuration may lack executable behavior.
For those changes, define structural, parsing, lint, or repository-state checks
first and observe them fail when practical. Generated or exploratory code is
not production-ready until its intended behavior has gone through this cycle.

## Evidence

Record the RED failure, the GREEN result, and the regression command. A focused
pass proves only the focused scope. Before any completion claim, call
`skill_view` for `verification-before-completion` and follow it.
