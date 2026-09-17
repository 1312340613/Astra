# macOS Computer Use compatibility evidence

## Historical summary

Earlier cooperative input checks stopped during AX tree preparation/selection,
before input. The real pointer did not move. Those fail-closed attempts did not
establish a passing compatibility cell.

| Application | Bundle ID | Historical outcome |
| --- | --- | --- |
| Fixture | `dev.astra.computer-fixture` | FAIL_CLOSED before input |
| WPS | `com.kingsoft.wpsoffice.mac` | FAIL_CLOSED before input |
| VS Code | `com.microsoft.VSCode` | FAIL_CLOSED before input |
| Edge | `com.microsoft.edgemac` | FAIL_CLOSED before input |

This is a redacted historical summary, not a fresh acceptance run or a claim
about every current application version. Raw run metadata and machine-specific
observations remain in private local evidence, outside the public repository.

## Current support and evidence

Read the current [compatibility registry](../config/macos_computer_compatibility.json)
and [evidence rules](computer-use-evidence.md) for configured capabilities and
the distinction between fixture results and fresh application acceptance.
The source registry and the signed helper's bundled resource must match.
Registry presence alone is not a fresh real-machine PASS.

Deterministic tests establish protocol, approval, build and cleanup contracts.
A skipped test, prior output or fail-closed stop cannot establish live desktop
compatibility. An accepted result applies only to its tested bundle, version and
action; it cannot authorize an untested action or bypass an approval boundary.

## Recording a new run

Follow the [real-machine runbook](macos-computer-use-real-machine-test-runbook.md)
and copy the [transactional matrix template](macos-app-state-acceptance.md) into
an ignored local evidence directory. Keep raw process/window identifiers, paths,
screenshots and user document details in that local copy. Publish only a
redacted summary that preserves the measured result and its limitations.
