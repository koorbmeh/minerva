"""Host loop v0 core logic — cursor delivery + approval resolution semantics.
Run: python tests/test_host_loop.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import host_loop  # noqa: E402

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
    with tempfile.TemporaryDirectory() as td:
        outbox = os.path.join(td, "outbox.jsonl")
        approvals = os.path.join(td, "approvals.jsonl")
        cursor = os.path.join(td, "cursor.json")

        # missing files -> empty, no crash
        check("missing outbox tolerated", host_loop.new_outbox(outbox, {}) == ([], 0))
        check("missing approvals tolerated", host_loop.pending_approvals(approvals) == [])

        # garbled line tolerated
        with open(outbox, "w", encoding="utf-8") as f:
            f.write('{"ts": 1, "message": "m1"}\n{TORN\n{"ts": 2, "message": "m2"}\n')
        msgs = host_loop.read_jsonl(outbox)
        check("garbled line skipped", [m["message"] for m in msgs] == ["m1", "m2"])

        # cursor: fresh -> delivered -> nothing new
        fresh, total = host_loop.new_outbox(outbox, host_loop.load_cursor(cursor))
        check("all new before cursor", len(fresh) == 2 and total == 2)
        host_loop.save_cursor({"outbox_delivered": total}, cursor)
        fresh, total = host_loop.new_outbox(outbox, host_loop.load_cursor(cursor))
        check("cursor advances delivery", fresh == [] and total == 2)
        jl(outbox, {"ts": 3, "message": "m3"})
        fresh, _ = host_loop.new_outbox(outbox, host_loop.load_cursor(cursor))
        check("only post-cursor messages new", [m["message"] for m in fresh] == ["m3"])

        # approvals: pending -> decision supersedes (latest line per ref wins)
        jl(approvals,
           {"ref": "r1", "ts": 1.0, "tool": "notify_operator", "args": {"m": 1},
            "reason": "outbound_comms", "status": "pending"},
           {"ref": "r2", "ts": 2.0, "tool": "spendy", "args": {},
            "reason": "cost $9.00 > $5.00", "status": "pending"})
        pend = host_loop.pending_approvals(approvals)
        check("both asks pending, oldest first",
              [p["ref"] for p in pend] == ["r1", "r2"])

        rec = host_loop.record_decision("r1", True, "fine", approvals)
        check("decision record shape",
              rec["status"] == "approved" and rec["by"] == "operator" and rec["note"] == "fine")
        pend = host_loop.pending_approvals(approvals)
        check("approved ref no longer pending", [p["ref"] for p in pend] == ["r2"])

        host_loop.record_decision("r2", False, "", approvals)
        check("denied ref no longer pending", host_loop.pending_approvals(approvals) == [])

        # decision lines carry no tool field and must not resurrect as asks
        jl(approvals, {"ref": "r1", "ts": 9.0, "status": "pending"})  # malformed echo
        pend = host_loop.pending_approvals(approvals)
        check("re-pended ref resolves to original ask fields",
              len(pend) == 1 and pend[0]["tool"] == "notify_operator")

        # pitch lane: ledger PITCH entries, cursor-tracked, read-only ----------
        ledger = os.path.join(td, "ledger.jsonl")
        jl(ledger,
           {"id": "a1", "type": "action", "payload": {"tool": "pitch_idea"}},
           {"id": "p1", "type": "pitch", "ts": 1.0,
            "payload": {"domain": "income", "title": "Sell the spare GPU",
                        "detail": "It idles; list it this week."}},
           {"id": "t1", "type": "tool_call", "payload": {"tool": "pitch_idea"}},
           {"id": "p2", "type": "pitch", "ts": 2.0,
            "payload": {"domain": "health", "title": "Morning sun walk",
                        "detail": "10 minutes after waking."}})
        fresh, total = host_loop.new_pitches(ledger, {})
        check("only pitch entries surface",
              [e["id"] for e in fresh] == ["p1", "p2"] and total == 2)
        fresh, total = host_loop.new_pitches(ledger, {"pitches_announced": 1})
        check("pitch cursor advances", [e["id"] for e in fresh] == ["p2"])
        fresh, _ = host_loop.new_pitches(ledger, {"pitches_dmed": 0},
                                         cursor_key="pitches_dmed")
        check("discord pitch cursor independent", len(fresh) == 2)
        txt = host_loop.fmt_pitch(fresh[0])
        check("pitch formatted for delivery",
              "[PITCH] Sell the spare GPU (income)" in txt
              and "list it this week" in txt and "p1" in txt
              and "verdict" in txt)
        bytes_before = open(ledger, "rb").read()
        host_loop.new_pitches(ledger, {})
        check("pitch lane is read-only on the ledger",
              open(ledger, "rb").read() == bytes_before)

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
