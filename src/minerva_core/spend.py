"""SpendMeter (round 15) - the budget governor's accounting (rails G4).

Round 13/14 gave Minerva real spending power (generation lanes, BYOK chat).
The gate's per-call controls (est-cost threshold, video quote cap) cannot see
ACCUMULATION - fifty small calls sail under every one of them. This meter
closes that: every lane that spends appends a line to state/spend.jsonl, and
GovernanceGate consults the rolling 24h total BEFORE allowing any call with a
nonzero estimated cost. Over budget -> the call ESCALATES to the operator (the
existing ask/approve rail), never silently runs.

File format: one JSON line per spend - {"ts", "usd", "what", "kind"}.
kind: "actual" (a real number, e.g. the video quote or chat token cost) or
"est" (our registration estimate, e.g. $0.04/image). Honest accounting beats
precise accounting: estimates are counted at face value.

Concurrency honesty: unlike the hash-chained ledger (single appender), this
is a plain append log written by the gateway process AND the chat host
process. Appends are single short lines opened O_APPEND-style; the reader is
torn-line TOLERANT (a lost line slightly undercounts - acceptable for a
budget meter, never for the audit ledger). The audit trail of decisions
remains the ledger; this file is metering only.

Stdlib only. ASCII.
"""
from __future__ import annotations

import json
import os
import time

DEFAULT_DAILY_CAP_USD = 5.0
WINDOW_S = 24 * 3600.0
SPEND_FILE_NAME = "spend.jsonl"


def daily_cap_usd() -> float:
    try:
        return float(os.environ.get("MINERVA_DAILY_BUDGET_USD", "").strip()
                     or DEFAULT_DAILY_CAP_USD)
    except ValueError:
        return DEFAULT_DAILY_CAP_USD


def spend_path(state_dir: str) -> str:
    return os.path.join(state_dir, SPEND_FILE_NAME)


def record_spend(path: str, usd: float, what: str,
                 kind: str = "actual") -> None:
    """Append one spend line. Never raises (metering must not break a lane)."""
    try:
        usd = float(usd)
        if usd <= 0:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": time.time(), "usd": round(usd, 6),
                                "what": str(what)[:120], "kind": kind},
                               ensure_ascii=True) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def spent_since(path: str, since_ts: float) -> float:
    """Tolerant rolling sum (torn/garbled lines skipped)."""
    if not os.path.exists(path):
        return 0.0
    total = 0.0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if float(rec.get("ts", 0)) >= since_ts:
                        total += max(0.0, float(rec.get("usd", 0)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError:
        return total
    return total


class SpendMeter:
    """The gate's view of the wallet. cap_usd=None re-reads the env knob at
    every check, so the operator can change the budget without restarting."""

    def __init__(self, path: str, cap_usd: float | None = None) -> None:
        self.path = path
        self._cap = cap_usd

    @property
    def cap_usd(self) -> float:
        return self._cap if self._cap is not None else daily_cap_usd()

    def spent_24h(self, now: float | None = None) -> float:
        return spent_since(self.path, (now or time.time()) - WINDOW_S)

    def add(self, usd: float, what: str, kind: str = "actual") -> None:
        record_spend(self.path, usd, what, kind)

    def check(self, est_usd: float,
              now: float | None = None) -> tuple[bool, float, float]:
        """(within_budget, spent_24h, cap). est<=0 is always within budget -
        free calls never get budget-blocked."""
        spent = self.spent_24h(now)
        if est_usd <= 0:
            return True, spent, self.cap_usd
        return (spent + est_usd) <= self.cap_usd, spent, self.cap_usd

    def status_text(self, now: float | None = None) -> str:
        spent = self.spent_24h(now)
        return (f"spend last 24h: ${spent:.2f} of ${self.cap_usd:.2f} daily "
                f"budget (${max(0.0, self.cap_usd - spent):.2f} left). Over-"
                "budget calls escalate to the operator instead of running. Budget "
                "knob: MINERVA_DAILY_BUDGET_USD.")
