"""The seam (spec §1): engines are replaceable, this interface is not.

Adapters live in minerva_core.adapters.* and may carry dependencies;
this module stays stdlib-only and engine-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol, runtime_checkable

from .events import EngineEvent


@dataclass
class RoutingHints:
    """Architecture §2d routing dimensions. The broker grows around these."""
    sensitivity: str = "normal"        # "private": see BrokerPolicy.private_off_frontier
    capability: str = "standard"       # light | standard | heavy | max (model tier)
    guardrail_tier: str = "frontier"   # "frontier" | "hosted-open" | "local"
    cost_cap_usd: float = 1.0          # per-task ceiling; breach -> ask
    modality: str = "text"             # "image"/"video": needs a *_gen-capable lane


@dataclass
class BrokerPolicy:
    """operator-level routing switches (compute_strategy, softened 2026-06-09:
    the privacy constraint follows the DATA/provider, not the building).

    private_off_frontier=False is operator-direct (2026-06-11): private work MAY
    ride the frontier lane unless he flips this switch — then it must land on
    a local-capable engine or the broker refuses (no silent leak)."""
    private_off_frontier: bool = False


@dataclass
class SessionConfig:
    persona: str = ""                  # system/persona text injected at session start
    model: str = ""                    # engine-native model hint ("" = engine default)
    routing: RoutingHints = field(default_factory=RoutingHints)
    tools: tuple[str, ...] = ()        # gateway tool allowlist for this session


@runtime_checkable
class EngineSession(Protocol):
    id: str

    def send(self, text: str) -> Iterator[EngineEvent]:
        """Submit a turn; yield events until DONE. Must always terminate."""
        ...

    def cancel(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class EngineAdapter(Protocol):
    name: str

    def capabilities(self) -> set[str]:
        """e.g. {"stream","tools_mcp","fork","model_per_session","local"}"""
        ...

    def create_session(self, cfg: SessionConfig) -> EngineSession: ...


class EngineRegistry:
    """v0.1 router: static rules over RoutingHints + BrokerPolicy.
    Replace guts later; keep pick()/route()."""

    def __init__(self, policy: BrokerPolicy | None = None) -> None:
        self._adapters: dict[str, EngineAdapter] = {}
        self._default: str | None = None
        self.policy = policy or BrokerPolicy()

    def register(self, adapter: EngineAdapter, default: bool = False) -> None:
        self._adapters[adapter.name] = adapter
        if default or self._default is None:
            self._default = adapter.name

    def get(self, name: str) -> EngineAdapter:
        return self._adapters[name]

    def pick(self, hints: RoutingHints) -> EngineAdapter:
        """Which engine gets the job. v0.2 policy (2026-06-12):
        * guardrail_tier "local" = an explicit route to the full-control
          tier — honored strictly (no local engine -> error, never a silent
          fall-through to frontier);
        * guardrail_tier "hosted-open" (aliases "open"/"uncensored") = the
          low-restriction hosted lane (Venice = adapter #3) — honored just
          as strictly: no such engine -> loud error, never a silent ride on
          a frontier lane whose guardrails the operator asked to avoid;
        * sensitivity "private" forces local ONLY when the operator flipped
          BrokerPolicy.private_off_frontier (his 2026-06-11 call: frontier
          is fine for private data until he says otherwise);
        * everything else -> the default engine. Deliberately dumb — the
          ledger will tell us what smarter routing should look like."""
        wants_local = (hints.guardrail_tier == "local"
                       or (hints.sensitivity == "private"
                           and self.policy.private_off_frontier))
        if wants_local:
            for a in self._adapters.values():
                if "local" in a.capabilities():
                    return a
            raise RuntimeError(
                "routing: this job requires a local-capable engine "
                f"(guardrail_tier={hints.guardrail_tier!r}, sensitivity="
                f"{hints.sensitivity!r}, private_off_frontier="
                f"{self.policy.private_off_frontier}) and none is registered")
        if hints.guardrail_tier in ("hosted-open", "open", "uncensored"):
            for a in self._adapters.values():
                caps = a.capabilities()
                if "hosted-open" in caps or "uncensored" in caps:
                    return a
            raise RuntimeError(
                "routing: this job asked for the hosted-open (low-restriction) "
                f"lane (guardrail_tier={hints.guardrail_tier!r}) and no such "
                "engine is registered - fill config/venice.env and register "
                "the Venice adapter")
        if hints.modality in ("image", "video"):       # round 13: generation
            need = hints.modality + "_gen"
            for a in self._adapters.values():
                if need in a.capabilities():
                    return a
            raise RuntimeError(
                f"routing: this job needs {need} and no registered engine "
                "provides it - the Venice lane does (fill config/venice.env "
                "and register VeniceAdapter)")
        if self._default is None:
            raise RuntimeError("no engines registered")
        return self._adapters[self._default]

    def route(self, hints: RoutingHints, persona: str = "",
              tools: tuple[str, ...] = ()) -> tuple[EngineAdapter, SessionConfig]:
        """pick() + a ready SessionConfig: the one call site for 'which brain
        gets which job'. Adapters that expose model_for_hints() (the BYOK
        lane's shortlist) get their model filled; engines without per-task
        models (Hermes lane) keep '' = engine default."""
        adapter = self.pick(hints)
        chooser = getattr(adapter, "model_for_hints", None)
        model = chooser(hints) if callable(chooser) else ""
        return adapter, SessionConfig(persona=persona, model=model,
                                      routing=hints, tools=tools)
