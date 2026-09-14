#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd -- "$ROOT"

PYTHON_BIN="${PYTHON:-$ROOT/.venv/bin/python}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf '  [ERROR] Project Python not found at %s. Run ./astra.sh --setup-only first or set PYTHON.\n' "$PYTHON_BIN" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$ROOT/scripts/phase_t_gate.py" "$@"
