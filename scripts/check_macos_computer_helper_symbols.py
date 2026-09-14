"""Validate the exact undefined-symbol boundary of the signed macOS helper."""

from __future__ import annotations

import sys

_REQUIRED_SYMBOLS = (
    "_CGEventPostToPid",
    "_CGEventTapCreate",
)
_FORBIDDEN_SYMBOLS = (
    "_CGWarpMouseCursorPosition",
)


def _captured_symbols(nm_text: str) -> set[str]:
    return {
        fields[-1]
        for line in nm_text.splitlines()
        if (fields := line.split())
    }


def main() -> int:
    symbols = _captured_symbols(sys.stdin.read())
    for symbol in _REQUIRED_SYMBOLS:
        if symbol not in symbols:
            print(f"Production helper is missing required symbol: {symbol}", file=sys.stderr)
            return 1
    for symbol in _FORBIDDEN_SYMBOLS:
        if symbol in symbols:
            print(f"Production helper retains forbidden symbol: {symbol}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
