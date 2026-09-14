#!/usr/bin/env bash
set -euo pipefail

if [[ "${ASTRA_MACOS_COMPUTER_E2E:-}" != "1" || "${ASTRA_MACOS_COMPUTER_PID_POINTER_EXPERIMENT:-}" != "1" ]]; then
  printf '%s\n' 'PID pointer experiment: NOT_RUN (set both explicit switches in an authorized desktop)'
  exit 0
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
ASTRA_MACOS_COMPUTER_E2E=1 ASTRA_MACOS_COMPUTER_PID_POINTER_EXPERIMENT=1 \
  swift run --package-path "$project_root/native/macos-computer-helper" AstraMacComputerE2EHarness
