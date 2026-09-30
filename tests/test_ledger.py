"""Tests for the ledger spine + anti-nag state machine + governance stub.

Stdlib only; run directly:  python3 tests/test_ledger.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.governance import ALLOW, ASK_OPERATOR, GovernanceGate
from minerva_core.ledger import Ledger, ProposalIndex, similar, slugify

DAY = 86400.0
PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def test_chain_and_tamper(tmp: str) -> None:
    path = os.path.join(tmp, "state", "ledger.jsonl")
    led = Ledger(path)
    a = led.append("note", {"msg": "genesis note"})
    led.append("action", {"tool": "x", "decision": "allow"}, parent_id=a["id"])
    led.append("cost", {"usd": 0.01})
    ok, n, bad = led.verify_chain()
    check("chain verifies clean", ok and n == 3 and bad is None)

    led2 = Ledger(path)  # reopen continues the chain
    led2.append("note", {"msg": "after reopen"})
    ok, n, _ = led2.verify_chain()
    check("chain survives reopen", ok and n == 4)

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    e = json.loads(lines[1])
    e["payload"]["decision"] = "deny"  # tamper with history
    lines[1] = json.dumps(e, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)
    ok, _, bad = Ledger(path).verify_chain()
    check("tamper detected at right line", not ok and bad == 3)


def test_proposal_lifecycle(tmp: str) -> None:
    led = Ledger(os.path.join(tmp, "p", "ledger.jsonl"))
    idx = ProposalIndex(led, max_open_per_domain=2, min_interval_s=0.0)

    p1 = idx.pitch("income", "Sell refurbished film cameras on eBay")
    check("pitch lands open", idx.state(p1["id"]) == "open")

    ok, why = idx.can_pitch("income", "sell refurbished film cameras on ebay!")
    check("exact-ish duplicate blocked while open", not ok and "open" in why)
    ok, why = idx.can_pitch("income", "Sell film cameras refurbished")
    check("similar wording blocked (jaccard)", not ok)

    idx.verdict(p1["id"], "decline_for_now", reason="not this month", cooldown_days=7)
    ok, why = idx.can_pitch("income", "Sell refurbished film cameras on eBay")
    check("cooling blocks re-pitch", not ok and "cooling" in why)
    future = time.time() + 8 * DAY
    ok, _ = idx.can_pitch("income", "Sell refurbished film cameras on eBay", now=future)
    check("cooldown expiry reopens", ok)

    p2 = idx.pitch("income", "Flip thrift-store electronics")
    idx.verdict(p2["id"], "decline_forever", reason="hates thrifting")
    ok, why = idx.can_pitch("income", "Flip thrift store electronics", now=future)
    check("decline_forever is forever", not ok and "forever" in why)

    p3 = idx.pitch("health", "Add zone-2 cardio block to protocol")
    idx.verdict(p3["id"], "adopt")
    check("adopt state derived", idx.state(p3["id"]) == "adopted")
    ok, why = idx.can_pitch("health", "Add zone 2 cardio block", now=future)
    check("adopted idea not re-pitched", not ok and "adopted" in why)

    pa = idx.pitch("biz", "Start a niche newsletter")
    pb = idx.pitch("biz", "Automate Etsy keyword research")
    ok, why = idx.can_pitch("biz", "Print-on-demand mug store", now=time.time())
    check("domain budget of 2 enforced", not ok and "budget" in why)
    idx.verdict(pa["id"], "park", reopen_condition="if the newsletter plateaus")
    ok, why = idx.can_pitch("biz", "Start a niche newsletter", now=future)
    check("parked blocks until reopen note", not ok and "parked" in why)
    led.append("note", {"reopen": True, "msg": "the newsletter plateaued"}, parent_id=pa["id"])
    check("explicit reopen flips state", idx.state(pa["id"]) == "reopened")

    try:
        idx.pitch("biz", "Automate Etsy keyword research")
        check("pitch() refuses duplicates", False)
    except PermissionError:
        check("pitch() refuses duplicates", True)

    idx2 = ProposalIndex(led, min_interval_s=DAY)
    ok, why = idx2.can_pitch("health", "Cold plunge tracking", now=time.time())
    check("min-interval throttle works", not ok and "interval" in why)


def test_governance(tmp: str) -> None:
    led = Ledger(os.path.join(tmp, "g", "ledger.jsonl"))
    gate = GovernanceGate(led, spend_ask_threshold_usd=5.0, deny_tools={"rm_rf"})

    d = gate.check("web_search", {"q": "weather"})
    check("benign tool allowed", d.decision == ALLOW)
    d = gate.check("send_email", {"to": "x"}, classes={"outbound_comms"})
    check("outbound comms escalates", d.decision == ASK_OPERATOR)
    d = gate.check("rent_gpu", {"gpu": "H100"}, est_cost_usd=12.0)
    check("big spend escalates", d.decision == ASK_OPERATOR)
    d = gate.check("rm_rf", {})
    check("deny list denies", d.decision == "deny")
    ok, n, _ = led.verify_chain()
    check("every decision ledgered", ok and n == 4)


def test_similarity() -> None:
    check("slugify", slugify("Sell, Refurbished Film Cameras!") == "sell-refurbished-film-cameras")
    check("similar true", similar("sell-refurbished-film-cameras-on-ebay", "sell-film-cameras-refurbished"))
    check("similar false", not similar("start-niche-newsletter", "automate-etsy-keyword-research"))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_chain_and_tamper(tmp)
        test_proposal_lifecycle(tmp)
        test_governance(tmp)
        test_similarity()
    print(f"\nall {PASSED} checks passed")
