#!/usr/bin/env bash
set -euo pipefail

if [[ "${ASTRA_MACOS_COMPUTER_E2E:-}" != "1" ]]; then
  printf '%s\n' 'ASTRA_MACOS_COMPUTER_E2E: NOT_RUN (set to 1 only in an explicitly authorized interactive desktop)'
  exit 0
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
release_log="$(mktemp -t astra-ax-release.XXXXXX)"
trap 'rm -f -- "$release_log"' EXIT

ASTRA_MACOS_COMPUTER_E2E=1 \
  swift run --package-path "$project_root/native/macos-computer-helper" AstraMacComputerE2EHarness \
  | tee "$release_log"
grep -Fqx 'AstraMacComputerE2EHarness: PASS AX-first/fail-closed' "$release_log"
