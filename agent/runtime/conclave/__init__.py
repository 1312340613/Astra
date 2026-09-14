"""Conclave — Astra's multi-expert research panel.

A hybrid inspired by virtual-model MoA and a 16-expert research seminar.
Each expert uses Astra's configured search waterfall, the chairperson
dynamically selects relevant experts based on question intent, and a
cross-discussion phase surfaces disagreements before the final synthesis.
"""

from .core import Conclave, ConclaveResult
from .config import ConclaveConfig

__all__ = ["Conclave", "ConclaveResult", "ConclaveConfig"]
