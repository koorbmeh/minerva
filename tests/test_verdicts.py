"""Pitch-verdict capture — decision file semantics + gateway fold.
Run: python tests/test_verdicts.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import host_loop  # noqa: E402
from minerva_core import verdicts as V  # noqa: E402
from minerva_core.gateway.mcp_stdio import MCPGateway  # noqa: E402
from minerva_core.gateway.tools_builtin import build_default_registry  # noqa: E402
from minerva_core.governance import GovernanceGate  # noqa: E402
from minerva_core.ledger import Ledger, ProposalIndex  # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def jl(path: str, *recs: dict) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def main() -> None:
    # ---- alias table ----------------------------------------------------
    check("aliases: adopt/yes", V.canonical_verdict("yes") == "adopt"
          and V.canonical_verdict("Adopt") == "adopt")
    check("aliases: never family", V.canonical_verdict("never") == "decline_forever"
          and V.canonical_verdict("decline forever") == "decline_forever")
    check("aliases: later family", V.canonical_verdict("later") == "decline_for_now"
          and V.canonical_verdict("not now") == "decline_for_now")
    check("aliases: park / junk", V.canonical_verdict("park") == "park"
          and V.canonical_verdict("maybe") is None)
    check("host_loop mirrors alias table",
          host_loop.VERDICT_ALIASES == V.VERDICT_ALIASES)

    with tempfile.TemporaryDirectory() as td:
        vpath = os.path.join(td, "verdicts.jsonl")

        # ---- decision lines ----------------------------------------------
        rec = V.record_verdict(vpath, "p1", "later", note="busy", cooldown_days=7)
        check("later line carries cooldown", rec["verdict"] == "decline_for_now"
              and rec["cooldown_days"] == 7.0 and rec["status"] == "decided")
        rec = V.record_verdict(vpath, "p2", "park", reopen_condition="after the move")
        check("park line carries reopen", rec["reopen_condition"] == "after the move")
        try:
            V.record_verdict(vpath, "p3", "dunno")
            check("junk verdict rejected", False)
        except ValueError:
            check("junk verdict rejected", True)
        V.record_verdict(vpath, "p4", "adopt", note="go ahead — carefully")
        raw = open(vpath, encoding="utf-8").read()
        check("decision lines are ASCII-safe", "\\u2014" in raw
              and "—" not in raw)

        # ---- pending semantics -------------------------------------------
        pend = V.pending_decisions(vpath)
        check("all decided lines pending", {d["pitch"] for d in pend}
              == {"p1", "p2", "p4"})
        jl(vpath, {"pitch": "p1", "ts": 1.0, "status": "folded",
                   "ledger_id": "x", "by": "gateway"})
        pend = V.pending_decisions(vpath)
        check("folded marker closes the cycle",
              {d["pitch"] for d in pend} == {"p2", "p4"})
        V.record_verdict(vpath, "p1", "adopt", note="changed my mind")
        pend = V.pending_decisions(vpath)
        check("new decision after fold reopens", any(
            d["pitch"] == "p1" and d["verdict"] == "adopt" for d in pend))
        V.record_verdict(vpath, "p2", "never")
        last_p2 = [d for d in V.pending_decisions(vpath) if d["pitch"] == "p2"]
        check("latest decision per pitch wins",
              len(last_p2) == 1 and last_p2[0]["verdict"] == "decline_forever")

    # ---- fold into a real ledger -----------------------------------------
    with tempfile.TemporaryDirectory() as td:
        led = Ledger(os.path.join(td, "ledger.jsonl"))
        idx = ProposalIndex(led, min_interval_s=0.0)
        vpath = os.path.join(td, "verdicts.jsonl")
        p1 = idx.pitch("income", "Sell the spare GPU")["id"]
        p2 = idx.pitch("health", "Try a standing desk")["id"]

        V.record_verdict(vpath, p1, "never", note="not selling it")
        s = V.fold(led, idx, vpath)
        check("fold appends one pitch_verdict",
              s == {"pending": 1, "folded": 1, "errors": 0})
        pv = [e for e in led.entries() if e["type"] == "pitch_verdict"]
        check("entry parented to pitch, actor operator",
              len(pv) == 1 and pv[0]["parent_id"] == p1
              and pv[0]["actor"] == "operator"
              and pv[0]["payload"]["verdict"] == "decline_forever"
              and pv[0]["payload"]["reason"] == "not selling it")
        marks = [r for r in V.read_lines(vpath) if r.get("status") == "folded"]
        check("folded marker cites ledger id",
              len(marks) == 1 and marks[0]["ledger_id"] == pv[0]["id"])
        ok, _, _ = led.verify_chain()
        check("chain intact after fold", ok)
        s = V.fold(led, idx, vpath)
        check("fold is idempotent", s == {"pending": 0, "folded": 0, "errors": 0})
        check("anti-nag: similar idea now dead",
              idx.can_pitch("income", "Sell the spare GPU now")[0] is False
              and "declined forever" in
              idx.can_pitch("income", "Sell the spare GPU now")[1])

        # crash recovery: decision present, equivalent verdict already
        # ledgered later, marker missing -> marker-only, no duplicate
        V.record_verdict(vpath, p2, "later", note="winter maybe", cooldown_days=14)
        d2 = V.pending_decisions(vpath)[0]
        idx.verdict(p2, "decline_for_now", reason="winter maybe", cooldown_days=14)
        n_before = len(led.entries())
        s = V.fold(led, idx, vpath)
        check("crash refold dedupes (marker-only)",
              s["folded"] == 1 and len(led.entries()) == n_before
              and d2["ts"] <= [e for e in led.entries()
                               if e["type"] == "pitch_verdict"][-1]["ts"])
        check("cooling state live", idx.state(p2) == "cooling")

        # unknown pitch + junk verdict in the file -> error markers, no growth
        jl(vpath, {"pitch": "nope-123", "ts": 1.0, "verdict": "adopt",
                   "note": "", "by": "operator", "status": "decided"})
        jl(vpath, {"pitch": p1, "ts": 1.0, "verdict": "sideways",
                   "note": "", "by": "operator", "status": "decided"})
        n_before = len(led.entries())
        s = V.fold(led, idx, vpath)
        check("bad lines -> error markers, ledger untouched",
              s["errors"] == 2 and len(led.entries()) == n_before)
        errs = [r for r in V.read_lines(vpath) if r.get("status") == "error"]
        check("error markers recorded", len(errs) == 2)
        s = V.fold(led, idx, vpath)
        check("error markers close their cycles", s["pending"] == 0)

        # change of mind AFTER fold -> second entry, newest wins in the index
        V.record_verdict(vpath, p1, "adopt", note="ok do it")
        V.fold(led, idx, vpath)
        check("post-fold change of mind lands", idx.state(p1) == "adopted")
        ok, _, _ = led.verify_chain()
        check("chain still intact at end", ok)

    # ---- pre-fold change of mind: only newest folds ------------------------
    with tempfile.TemporaryDirectory() as td:
        led = Ledger(os.path.join(td, "ledger.jsonl"))
        idx = ProposalIndex(led, min_interval_s=0.0)
        vpath = os.path.join(td, "verdicts.jsonl")
        p1 = idx.pitch("general", "Refile the garage shelves")["id"]
        V.record_verdict(vpath, p1, "later")
        V.record_verdict(vpath, p1, "never", note="final answer")
        V.fold(led, idx, vpath)
        pv = [e for e in led.entries() if e["type"] == "pitch_verdict"]
        check("pre-fold rethink folds once, newest wins",
              len(pv) == 1 and pv[0]["payload"]["verdict"] == "decline_forever")

    # ---- gateway integration ----------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        lpath = os.path.join(td, "ledger.jsonl")
        vpath = os.path.join(td, "verdicts.jsonl")
        led = Ledger(lpath)
        idx = ProposalIndex(led, min_interval_s=0.0)
        p1 = idx.pitch("income", "Sell the spare GPU")["id"]
        V.record_verdict(vpath, p1, "never", note="keep it")
        reg = build_default_registry(led, idx,
                                     outbox_path=os.path.join(td, "outbox.jsonl"))
        gate = GovernanceGate(led)
        gw = MCPGateway(reg, gate, led, approvals_path=os.path.join(td, "a.jsonl"),
                        proposals=idx, verdicts_path=vpath)
        check("gateway folds at startup",
              any(e["type"] == "pitch_verdict" for e in led.entries())
              and V.pending_decisions(vpath) == [])
        out = gw.call_tool("pitch_idea", {"domain": "income",
                                          "title": "Sell the spare GPU now"})
        check("folded verdict blocks similar pitch through gateway",
              "REJECTED" in out["content"][0]["text"]
              and "declined forever" in out["content"][0]["text"])
        p2 = idx.pitch("health", "Try a standing desk")["id"]
        V.record_verdict(vpath, p2, "never", note="mid-session decision")
        out = gw.call_tool("pitch_idea", {"domain": "health",
                                          "title": "Try the standing desk"})
        check("mid-session decision folds before pitch_idea",
              "declined forever" in out["content"][0]["text"])
        legacy = MCPGateway(reg, gate, led, approvals_path=os.path.join(td, "a.jsonl"))
        out = legacy.call_tool("ping", {"echo": "x"})
        check("legacy construction (no fold wiring) still serves",
              out["content"][0]["text"] == "pong x")
        ok, _, _ = led.verify_chain()
        check("chain intact after gateway folds", ok)

    # ---- host_loop surface --------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        lpath = os.path.join(td, "ledger.jsonl")
        vpath = os.path.join(td, "verdicts.jsonl")
        jl(lpath,
           {"id": "19aa11-aaaa", "type": "pitch", "ts": 1.0,
            "payload": {"title": "Pitch A", "domain": "x", "detail": "d"}},
           {"id": "19bb22-bbbb", "type": "pitch", "ts": 2.0,
            "payload": {"title": "Pitch B", "domain": "y", "detail": "d"}},
           {"id": "19cc33-cccc", "type": "pitch", "ts": 3.0,
            "payload": {"title": "Pitch C", "domain": "z", "detail": "d"}},
           {"id": "v1", "type": "pitch_verdict", "ts": 4.0,
            "parent_id": "19cc33-cccc", "payload": {"verdict": "adopt"}})
        waiting, fold_n = host_loop.verdict_state(lpath, vpath)
        check("verdict_state: folded pitch excluded",
              {e["id"] for e in waiting} == {"19aa11-aaaa", "19bb22-bbbb"}
              and fold_n == 0)
        rec = host_loop.record_verdict_line("19aa11-aaaa", "not now",
                                            note="swamped", cooldown_days=10,
                                            verdicts_path=vpath)
        check("record_verdict_line maps alias + cooldown",
              rec["verdict"] == "decline_for_now" and rec["cooldown_days"] == 10.0)
        waiting, fold_n = host_loop.verdict_state(lpath, vpath)
        check("decided-not-folded leaves the waiting list",
              {e["id"] for e in waiting} == {"19bb22-bbbb"} and fold_n == 1)
        try:
            host_loop.record_verdict_line("19bb22-bbbb", "shrug",
                                          verdicts_path=vpath)
            check("host_loop rejects junk verdict", False)
        except ValueError:
            check("host_loop rejects junk verdict", True)
        pid, why = host_loop.resolve_pitch_ref("bb22", lpath)
        check("pitch ref: unique substring resolves",
              pid == "19bb22-bbbb" and why == "")
        pid, why = host_loop.resolve_pitch_ref("19", lpath)
        check("pitch ref: ambiguous refused", pid is None and "matches 3" in why)
        pid, why = host_loop.resolve_pitch_ref("zz", lpath)
        check("pitch ref: miss explained", pid is None and "no pitch" in why)
        bytes_before = open(lpath, "rb").read()
        host_loop.verdict_state(lpath, vpath)
        host_loop.resolve_pitch_ref("bb22", lpath)
        check("verdict lane is read-only on the ledger",
              open(lpath, "rb").read() == bytes_before)

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
