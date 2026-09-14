#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$SCRIPT_DIR"
# Select a bootstrap interpreter, without changing the caller's workspace.
if [[ -n "${PYTHON:-}" ]]; then
    ASTRA_PYTHON="$PYTHON"
elif command -v python3.11 >/dev/null 2>&1; then
    ASTRA_PYTHON="$(command -v python3.11)"
elif command -v python3 >/dev/null 2>&1; then
    ASTRA_PYTHON="$(command -v python3)"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    ASTRA_PYTHON="$ROOT/.venv/bin/python"
else
    printf 'Astra: Python 3.11 or newer is required.\n' >&2
    exit 1
fi
if ! "$ASTRA_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' >/dev/null 2>&1; then
    printf 'Astra: Python 3.11 or newer is required (selected: %s).\n' "$ASTRA_PYTHON" >&2
    exit 1
fi
exec "$ASTRA_PYTHON" "$ROOT/astra.py" "$@"
