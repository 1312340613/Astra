#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd -- "$ROOT"

if (( $# > 1 )); then
    printf 'Usage: %s [image-tag]\n' "${0##*/}" >&2
    exit 2
fi
TAG="${1:-astra-sandbox:agent-system-dev}"

exec docker build --file "$ROOT/docker/sandbox/Dockerfile" --tag "$TAG" "$ROOT"
