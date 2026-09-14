"""Astra-native computer-activity recorder (stage 3). Design:
docs/activity-history.md#activity-summary-catch-up-and-health

M1 = the contract: bucket/event JSON shapes byte-compatible with CUAService so
sync/store/FTS/vector stay untouched. The Swift recorder (M2/M3) must emit
exactly what `validate_event` accepts.
"""
from . import contract  # noqa: F401
