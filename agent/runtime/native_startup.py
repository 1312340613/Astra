"""Load Windows native dependencies before background retrieval starts."""

import importlib
import logging
import sys


def prepare_native_dependencies() -> None:
    """Avoid concurrent Windows DLL initialization in NumPy and SDK imports.

    These imports must run on the startup thread, not via asyncio.to_thread:
    an import stuck in a native loader also prevents asyncio deadlines firing.
    macOS retains its existing lazy loading behavior.
    """
    if sys.platform != "win32":
        return
    for name in ("numpy", "jiter"):
        try:
            importlib.import_module(name)
        except ImportError:
            # NumPy is optional; normal consumers own dependency diagnostics.
            logging.getLogger(__name__).warning("Native startup import unavailable: %s", name)
