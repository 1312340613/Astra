"""Dependency-free source entrypoint; keeps cwd as the user's workspace."""

from pathlib import Path
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Astra requires Python 3.11 or newer.")

# -I/-S management probes and bootstrap interpreters must still import the
# package beside this file, never an unrelated checkout in the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.launcher.cli import main

if __name__ == "__main__":
    raise SystemExit(main(root=Path(__file__).resolve().parent))
