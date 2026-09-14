# macOS Cooperative Computer Use compatibility evidence

Date: 2026-08-29 (Asia/Singapore)

## Verdict

**SUPPORTED DETERMINISTIC RELEASE GATES PASS; REAL-DESKTOP COMPATIBILITY
FAIL-CLOSED.**

No real application or fixture PID compatibility cell is accepted. This report
does not treat a deterministic test, skipped run, old artifact, or fail-closed
stop as real-desktop compatibility acceptance.

## Real-desktop compatibility evidence

These results come from the previously authorized real-desktop compatibility
attempts. Task 4 did not rerun desktop PID acceptance and did not enable any
compatibility cell.

| Application | Bundle ID | Exact version | Result | Failure stage |
| --- | --- | --- | --- | --- |
| Fixture | `dev.astra.computer-fixture` | `1.0.0` | FAIL_CLOSED | AX tree preparation/selection before input |
| WPS | `com.kingsoft.wpsoffice.mac` | `12.1.26026` | FAIL_CLOSED | AX tree preparation/selection before input |
| VS Code | `com.microsoft.VSCode` | `1.134.0` | FAIL_CLOSED | AX tree preparation/selection before input |
| Edge | `com.microsoft.edgemac` | `151.0.4129.107` | FAIL_CLOSED | AX tree preparation/selection before input |

All four cells failed during AX tree preparation/selection before input. No
synthetic input was produced, the real pointer did not move, and no target
effect was used as acceptance evidence. Therefore none of these attempts is a
compatibility PASS.

## Accepted registry

The accepted registry remains exactly empty:

```json
{"schema_version": 1, "applications": []}
```

The source registry and the signed bundle resource must remain byte-identical.
Their current byte comparison is recorded with the deterministic release gates
below; it cannot turn a failed real-desktop cell into an accepted one.

## Deterministic release gates

Deterministic gates establish protocol, test, build, signing, bundled-resource,
and symbol properties. They do not establish real-application PID compatibility.

| Gate | Result |
| --- | --- |
| Focused Python protocol/skill tests | PASS: 103 passed |
| Full Python | PASS: 2801 passed, 45 skipped, 2 xfailed |
| Raw `swift test --package-path native/macos-computer-helper` | NOT A TEST PASS: build completed, then exit 1 with `error: no tests found` because the package uses executable test products |
| Repository native script | PASS: 186 Swift tests and 58 production harness tests |
| Production helper build | PASS |
| Deep/strict codesign verification | PASS |
| Source/bundled registry byte comparison | PASS: both 48 bytes with SHA-256 `118c557e1a700477e15586466d45ed655551f63946eeee412878ac6ecb2b737f` |
| Required signed symbols | PASS: `_CGEventPostToPid` and `_CGEventTapCreate` present |
| Forbidden signed symbols | PASS: `_CGWarpMouseCursorPosition` and exact `_CGEventPost` absent |
| Raw `npm test --prefix ui-tui` | NOT A TEST PASS: exit 1 because the package has no `test` script |
| Repository-supported TUI test command | PASS: 118 passed |
| TUI production build | PASS: `tsc --noEmit` and `esbuild` |
| Ruff on every changed Python file from `5104e3a` plus Task 4 | PASS: 10 files |
| `git diff --check` | PASS |

The first full Python run exposed an inherited Task 2 cross-language fixture
gap: `AstraComputerProtocolFixture` omitted the required
`pid_action_classes` field from a successful plan response. Eight integration
cases failed before `act` parsing. A direct regression then failed with
`KeyError: 'pid_action_classes'`; adding only the exact empty PID subset made
all 15 integration tests pass, and the full Python result above is from the
post-fix rerun.

The two raw command-selector failures are recorded rather than converted into
passes. Behavioral coverage comes from the repository-supported native and TUI
commands with explicit nonzero test counts. No real-desktop PID acceptance was
rerun during these deterministic gates.

## Independent review remediation

The first whole-branch review found that Python created a new foreground plan
after the once-only approval. Although native execution consumed that new plan
exactly once, its sealed backend alignment was not the plan present when the
user approved the fragment. The permission check now creates the foreground
plan before approval, verifies the earlier background digest through a
background-mode projection of that same immutable plan, and passes its unchanged
`plan_ref` to both `takeover_begin` and `act`. A regression proves there are
exactly two plans across the background request and foreground retry, that no
planning occurs after approval, and that both native calls receive the plan
recorded at approval.

The same review found and closed two evidence-contract issues: the design
document's trailing spaces made a range-aware diff check fail, and the build
script's allowed-symbol alternation required only one of the two cooperative
symbols. The range-aware diff is now clean, while the build and tests require
`_CGEventPostToPid` and `_CGEventTapCreate` independently. Operator documentation
also no longer promises the removed synthetic Unicode keyboard fallback.
