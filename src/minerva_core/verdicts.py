"""Pitch-verdict capture — the appender-safe bridge from the operator to the ledger.

The operator records verdicts on pitches (adopt / decline_forever / decline_for_now /
park) via host_loop, which appends DECISION lines to state/verdicts.jsonl —
an append-only event log, never the hash-chained ledger (single-appender rule:
the gateway process owns all ledger writes; a second writer forks the chain).

The GATEWAY folds pending decisions into the ledger as PITCH_VERDICT entries
(actor=operator, parent=pitch) at startup and again before any pitch_idea call —
so ProposalIndex/can_pitch never evaluates a brainstorm against stale taste
state. Same pattern as approvals.jsonl: decision lines from the operator, marker
lines from the gateway, latest line per pitch wins, nothing rewritten.

Line shapes (one JSON object per line, ASCII-safe):
  {"pitch": id, "ts": t, "verdict": v, "note": s, "by": "operator",
   "status": "decided"[, "cooldown_days": f][, "reopen_condition": s]}
  {"pitch": id, "ts": t, "status": "folded", "ledger_id": id, "by": "gateway"}
  {"pitch": id, "ts": t, "status": "error",  "error": s,      "by": "gateway"}

Crash ordering: ledger append FIRST, marker second. A crash between the two
re-folds next pass, where the content dedupe (same pitch + verdict + reason,
ledger ts >= decision ts) turns it into marker-only — duplicate work beats a
lost verdict, and the dedupe prevents a duplicate ledger entry.

A new decision line after a folded marker re-opens the cycle: The operator changed
his mind, the fold appends a fresh PITCH_VERDICT, and ProposalIndex (file-
order scan, last verdict wins) follows. Every verdict is taste-learning data.
"""
from __future__ import annotations

import json
import os
import time

from .ledger import PITCH, PITCH_VERDICT, PITCH_VERDICTS, Ledger, ProposalIndex

# Plain-language aliases -> canonical verdicts. host_loop mirrors this table
# (it stays stdlib-standalone); change BOTH or the CLI and fold disagree.
VERDICT_ALIASES = {
    "adopt": "adopt", "yes": "adopt",
    "never": "decline_forever", "decline-forever": "decline_forever",
    "decline_forever": "decline_forever",
    "later": "decline_for_now", "not-now": "decline_for_now",
    "notnow": "decline_for_now", "decline-for-now": "decline_for_now",
    "decline_for_now": "decline_for_now",
    "park": "park",
}


def canonical_verdict(word: str) -> str | None:
    return VERDICT_ALIASES.get(str(word).strip().lower().replace(" ", "-"))


def read_lines(path: str) -> list[dict]:
    """Tolerant JSONL reader (torn/garbled lines skipped)."""
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _append(path: str, rec: dict) -> dict:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=True, sort_keys=True) + "\n")
    return rec


def record_verdict(path: str, pitch_id: str, verdict: str, note: str = "",
                   cooldown_days: float = 30.0, reopen_condition: str = "",
                   by: str = "operator") -> dict:
    """Append the operator's decision line. Accepts aliases; raises on junk."""
    v = canonical_verdict(verdict)
    if v is None:
        raise ValueError(f"unknown verdict '{verdict}' "
                         f"(use one of: {sorted(set(VERDICT_ALIASES))})")
    rec = {"pitch": pitch_id, "ts": time.time(), "verdict": v,
           "note": str(note), "by": by, "status": "decided"}
    if v == "decline_for_now":
        rec["cooldown_days"] = float(cooldown_days)
    if v == "park":
        rec["reopen_condition"] = str(reopen_condition)
    return _append(path, rec)


def pending_decisions(path: str) -> list[dict]:
    """Decisions awaiting fold: per pitch, the LAST line wins; pending iff
    that line is a 'decided' line (a later marker closes the cycle)."""
    last: dict[str, dict] = {}
    order: list[str] = []
    for rec in read_lines(path):
        pid = rec.get("pitch")
        if not pid:
            continue
        if pid not in last:
            order.append(pid)
        last[pid] = rec
    return [last[p] for p in order if last[p].get("status") == "decided"]


def fold(ledger: Ledger, proposals: ProposalIndex, path: str) -> dict:
    """Fold pending decisions into the ledger as PITCH_VERDICT entries.

    MUST run inside the single-appender process (the gateway). Returns
    {"pending": n, "folded": n, "errors": n}. Idempotent: no pending
    decisions -> reads only, writes nothing.
    """
    summary = {"pending": 0, "folded": 0, "errors": 0}
    pend = pending_decisions(path)
    summary["pending"] = len(pend)
    if not pend:
        return summary
    entries = ledger.entries()
    pitch_ids = {e["id"] for e in entries if e["type"] == PITCH}
    for d in pend:
        pid = str(d.get("pitch", ""))
        v = str(d.get("verdict", ""))
        note = str(d.get("note", ""))
        if pid not in pitch_ids:
            _append(path, {"pitch": pid, "ts": time.time(), "status": "error",
                           "error": "unknown pitch id", "by": "gateway"})
            summary["errors"] += 1
            continue
        if v not in PITCH_VERDICTS:
            _append(path, {"pitch": pid, "ts": time.time(), "status": "error",
                           "error": f"bad verdict '{v}'", "by": "gateway"})
            summary["errors"] += 1
            continue
        # Crash recovery: equivalent verdict already ledgered after this
        # decision was made -> marker-only, no duplicate entry.
        dup = next((e for e in entries
                    if e["type"] == PITCH_VERDICT and e.get("parent_id") == pid
                    and e["payload"].get("verdict") == v
                    and e["payload"].get("reason") == note
                    and e["ts"] >= float(d.get("ts", 0.0))), None)
        if dup is not None:
            _append(path, {"pitch": pid, "ts": time.time(), "status": "folded",
                           "ledger_id": dup["id"], "by": "gateway"})
            summary["folded"] += 1
            continue
        e = proposals.verdict(pid, v, reason=note,
                              cooldown_days=float(d.get("cooldown_days", 30.0)),
                              reopen_condition=str(d.get("reopen_condition", "")))
        _append(path, {"pitch": pid, "ts": time.time(), "status": "folded",
                       "ledger_id": e["id"], "by": "gateway"})
        summary["folded"] += 1
    return summary
