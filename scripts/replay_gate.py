"""Governance REPLAY suite v0 (round 21) - the rails row's last gap.

Re-evaluates the gate's HISTORICAL decisions (the hash-chained ledger,
READ-ONLY) against the CURRENT gate configuration, and reports what would
change. This is the promote-time regression net the architecture doc asked
for: before trusting a new gate config / policy / code change, replay the
decision log and see every flip with your own eyes.

Honest scope (deliberate):
  * It replays the POLICY decision (tool + classes + est cost vs deny list,
    ask-classes, spend threshold) - the gate's actual inputs, all of which
    every ledger entry carries in its ctx.
  * STATEFUL outcomes are skipped, not faked: kill-switch denials, the
    approve/deny/pending interplay, and daily-budget breaches depended on
    state at that moment; replaying them against today's state would be
    noise. They are counted and listed as "skipped (stateful)".
  * The inline warden is never CALLED in replay (no API, no spend) - but
    --inline-classes shows which historical ALLOWs would NOW go through
    pre-action review ("would-review"), so the operator sees the blast radius of
    turning a class on before he does it.

Usage:
  python scripts\\replay_gate.py                       # current defaults
  python scripts\\replay_gate.py --deny-tools web_fetch # contemplated change
  python scripts\\replay_gate.py --threshold 1.0
  python scripts\\replay_gate.py --inline-classes media_gen,send_file

Artifacts: state/replay-report.json. Exit 0 unless the ledger is unreadable.
Stdlib only. ASCII out (cp1252 rule). The replay gate writes to a THROWAWAY
in-memory-ish tmp ledger - the real ledger keeps its single appender.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from minerva_core.governance import GovernanceGate            # noqa: E402
from minerva_core.ledger import Ledger                        # noqa: E402

STATE = os.path.join(ROOT, "state")

STATEFUL_PREFIXES = ("KILL SWITCH", "operator approved", "operator denied",
                     "daily budget:", "inline warden")
RANK = {"allow": 0, "ask": 1, "deny": 2}


def historical_decisions(ledger_path: str) -> list[dict]:
    """Tool-call decisions from the ledger, oldest first. Torn-tolerant."""
    out = []
    if not os.path.exists(ledger_path):
        return out
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = e.get("payload") or {}
            if e.get("type") not in ("action", "ask"):
                continue
            if not p.get("tool") or "decision" not in p:
                continue
            out.append({"id": e.get("id", ""), "ts": e.get("ts", ""),
                        "tool": p["tool"],
                        "classes": set(p.get("classes") or []),
                        "est_cost_usd": float(p.get("est_cost_usd") or 0.0),
                        "old_decision": p["decision"],
                        "old_reason": str(p.get("reason", ""))})
    return out


def replay(ledger_path: str, deny_tools: set[str] | None = None,
           threshold: float = 5.0,
           inline_cls: set[str] | None = None) -> dict:
    inline_cls = inline_cls or set()
    rows = historical_decisions(ledger_path)
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
              "ledger": ledger_path, "entries_replayed": 0,
              "config": {"deny_tools": sorted(deny_tools or []),
                         "spend_ask_threshold_usd": threshold,
                         "inline_classes": sorted(inline_cls)},
              "same": 0, "stricter": [], "looser": [],
              "skipped_stateful": 0, "would_review": [], "rows": []}
    with tempfile.TemporaryDirectory() as td:
        gate = GovernanceGate(Ledger(os.path.join(td, "replay-ledger.jsonl")),
                              spend_ask_threshold_usd=threshold,
                              deny_tools=deny_tools or set())
        for r in rows:
            if any(r["old_reason"].startswith(p) for p in STATEFUL_PREFIXES):
                report["skipped_stateful"] += 1
                continue
            d = gate.check(r["tool"], {}, classes=r["classes"],
                           est_cost_usd=r["est_cost_usd"])
            report["entries_replayed"] += 1
            row = {"id": r["id"], "tool": r["tool"],
                   "old": r["old_decision"], "new": d.decision}
            report["rows"].append(row)
            if d.decision == r["old_decision"]:
                report["same"] += 1
            elif RANK.get(d.decision, 1) > RANK.get(r["old_decision"], 1):
                report["stricter"].append(row)
            else:
                report["looser"].append(row)
            if (d.decision == "allow" and r["classes"] & inline_cls):
                report["would_review"].append({"id": r["id"], "tool": r["tool"],
                                               "classes": sorted(r["classes"]
                                                                 & inline_cls)})
    return report


def main() -> int:
    ap = argparse.ArgumentParser(prog="minerva-replay-gate")
    ap.add_argument("--state-dir",
                    default=os.environ.get("MINERVA_STATE_DIR") or STATE)
    ap.add_argument("--deny-tools", default="",
                    help="comma list: contemplated deny-list change")
    ap.add_argument("--threshold", type=float,
                    default=float(os.environ.get("MINERVA_SPEND_THRESHOLD", "5.0")))
    ap.add_argument("--inline-classes", default="",
                    help="comma list: show which past ALLOWs would now review")
    args = ap.parse_args()

    ledger_path = os.path.join(args.state_dir, "ledger.jsonl")
    deny = {t.strip() for t in args.deny_tools.split(",") if t.strip()}
    icls = {c.strip() for c in args.inline_classes.split(",") if c.strip()}
    rep = replay(ledger_path, deny_tools=deny, threshold=args.threshold,
                 inline_cls=icls)
    out = os.path.join(args.state_dir, "replay-report.json")
    os.makedirs(args.state_dir, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, indent=2)

    print("=== GATE REPLAY ===")
    print(f"  replayed: {rep['entries_replayed']} decision(s); "
          f"skipped (stateful): {rep['skipped_stateful']}")
    print(f"  unchanged: {rep['same']}")
    print(f"  now STRICTER: {len(rep['stricter'])}"
          + (" - " + ", ".join(f"{x['tool']}({x['old']}->{x['new']})"
                               for x in rep["stricter"][:8])
             if rep["stricter"] else ""))
    print(f"  now LOOSER: {len(rep['looser'])}"
          + (" - " + ", ".join(f"{x['tool']}({x['old']}->{x['new']})"
                               for x in rep["looser"][:8])
             if rep["looser"] else ""))
    if icls:
        print(f"  would now go to inline review: {len(rep['would_review'])}")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
