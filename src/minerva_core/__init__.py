"""minerva_core — the permanent layer: interface, events, ledger, governance.

Stdlib only. Engines (Hermes, frontier APIs, slim loop) plug in behind
engine.EngineAdapter; everything here outlives any engine choice.
Spec: ../../Minerva-Engine-Interface.md
"""

__version__ = "0.1.0"

from .events import EngineEvent, EventType
from .engine import (BrokerPolicy, EngineAdapter, EngineSession, EngineRegistry,
                     RoutingHints, SessionConfig)
from .ledger import Ledger, ProposalIndex, slugify
from .governance import GovernanceGate, GovernanceDecision

__all__ = [
    "EngineEvent", "EventType",
    "BrokerPolicy", "EngineAdapter", "EngineSession", "EngineRegistry",
    "RoutingHints", "SessionConfig",
    "Ledger", "ProposalIndex", "slugify",
    "GovernanceGate", "GovernanceDecision",
]
