"""Broker v0.1 — model shortlist, prices, BrokerPolicy routing.
Run: python tests/test_routing.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.adapters.anthropic_byok import (  # noqa: E402
    MODEL_PRICES, MODEL_SHORTLIST, model_for, price_for)
from minerva_core.engine import (  # noqa: E402
    BrokerPolicy, EngineRegistry, RoutingHints, SessionConfig)

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class FakeSession:
    id = "fake-1"

    def send(self, text):
        return iter(())

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeAdapter:
    def __init__(self, name: str, caps: set[str], model_map=None) -> None:
        self.name = name
        self._caps = caps
        self._model_map = model_map

    def capabilities(self) -> set[str]:
        return self._caps

    def create_session(self, cfg: SessionConfig) -> FakeSession:
        return FakeSession()


class FakeByok(FakeAdapter):
    def model_for_hints(self, hints) -> str:
        return model_for(hints)


def main() -> None:
    # ---- shortlist + prices ------------------------------------------------
    check("shortlist covers all four tiers",
          set(MODEL_SHORTLIST) == {"light", "standard", "heavy", "max"})
    check("every shortlist model is priced",
          all(m in MODEL_PRICES for m in MODEL_SHORTLIST.values()))
    check("tier ladder is price-ordered",
          [MODEL_PRICES[MODEL_SHORTLIST[t]][1] for t in
           ("light", "standard", "heavy", "max")]
          == sorted(MODEL_PRICES[MODEL_SHORTLIST[t]][1] for t in
                    ("light", "standard", "heavy", "max")))
    check("model_for maps tiers",
          model_for("light") == "claude-haiku-4-5-20251001"
          and model_for("heavy") == "claude-opus-4-8"
          and model_for("max") == "claude-fable-5")
    check("model_for defaults to standard (never priciest)",
          model_for("") == "claude-sonnet-4-6"
          and model_for("frontier-ultra") == "claude-sonnet-4-6"
          and model_for(None) == "claude-sonnet-4-6")
    check("model_for accepts RoutingHints",
          model_for(RoutingHints(capability="light"))
          == "claude-haiku-4-5-20251001")
    check("RoutingHints default tier is standard",
          RoutingHints().capability == "standard")

    check("price_for exact ids", price_for("claude-sonnet-4-6") == (3.0, 15.0)
          and price_for("claude-fable-5") == (10.0, 50.0)
          and price_for("claude-opus-4-8") == (5.0, 25.0))
    check("price_for dated response id -> alias price",
          price_for("claude-sonnet-4-6-20250901") == (3.0, 15.0))
    check("price_for alias -> dated key price",
          price_for("claude-haiku-4-5") == (1.0, 5.0))
    check("price_for unknown degrades to token-only",
          price_for("gpt-99") == (0.0, 0.0) and price_for("") == (0.0, 0.0))
    # the live BYOK probe burned 579 in / 55 out on haiku — about a tenth of
    # a cent; the COST event math is (in*pin + out*pout) / 1e6
    pin, pout = price_for("claude-haiku-4-5-20251001")
    check("haiku probe math lands at ~$0.001",
          round((579 * pin + 55 * pout) / 1_000_000, 6) == 0.000854)

    # ---- BrokerPolicy + pick() ----------------------------------------------
    local = FakeAdapter("local-llm", {"local"})
    frontier = FakeByok("anthropic-byok", {"frontier", "tools_mcp"})

    reg = EngineRegistry()
    reg.register(frontier, default=True)
    reg.register(local)
    check("normal work -> default engine",
          reg.pick(RoutingHints()) is frontier)
    check("private rides frontier by default (the operator 2026-06-11)",
          reg.pick(RoutingHints(sensitivity="private")) is frontier)
    check("guardrail_tier local -> local engine",
          reg.pick(RoutingHints(guardrail_tier="local")) is local)

    strict = EngineRegistry(policy=BrokerPolicy(private_off_frontier=True))
    strict.register(frontier, default=True)
    strict.register(local)
    check("flipped switch: private -> local engine",
          strict.pick(RoutingHints(sensitivity="private")) is local)

    nolocal = EngineRegistry(policy=BrokerPolicy(private_off_frontier=True))
    nolocal.register(frontier, default=True)
    try:
        nolocal.pick(RoutingHints(sensitivity="private"))
        check("no silent leak to frontier", False)
    except RuntimeError as exc:
        check("no silent leak to frontier", "local-capable" in str(exc))
    try:
        nolocal.pick(RoutingHints(guardrail_tier="local"))
        check("local tier never falls through", False)
    except RuntimeError:
        check("local tier never falls through", True)
    check("nolocal registry still serves normal work",
          nolocal.pick(RoutingHints()) is frontier)

    # ---- route(): adapter + SessionConfig in one call ------------------------
    a, cfg = reg.route(RoutingHints(capability="light"), persona="be brief",
                       tools=("ping",))
    check("route picks engine + shortlist model",
          a is frontier and cfg.model == "claude-haiku-4-5-20251001"
          and cfg.persona == "be brief" and cfg.tools == ("ping",))
    a, cfg = reg.route(RoutingHints(capability="max"))
    check("route: max tier opts into fable", cfg.model == "claude-fable-5")
    a, cfg = reg.route(RoutingHints(guardrail_tier="local", capability="heavy"))
    check("route: engines without a shortlist keep engine-default model",
          a is local and cfg.model == "")
    check("route carries hints through", cfg.routing.capability == "heavy")

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
