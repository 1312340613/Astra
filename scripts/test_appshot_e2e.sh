#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Appshot cross-process fixtures require macOS.' >&2
  exit 1
fi
exec swift test --package-path "$project_root/native/macos-computer-helper" \
  --disable-automatic-resolution --no-parallel --filter AppshotCrossProcessTests "$@"
