"""One spine, two views (spec §4).

Append-only JSONL, hash-chained (prev_hash = sha256 of previous raw line).
Decision-log view: action/ask/verdict/cost. Proposal-ledger view:
pitch/pitch_verdict/outcome + derived anti-nag state machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time

GENESIS = "0" * 64

# entry types
SESSION, ACTION, TOOL_CALL, ASK, VERDICT = "session", "action", "tool_call", "ask", "verdict"
PITCH, PITCH_VERDICT, OUTCOME, COST, NOTE = "pitch", "pitch_verdict", "outcome", "cost", "note"

VALID_TYPES = {SESSION, ACTION, TOOL_CALL, ASK, VERDICT, PITCH, PITCH_VERDICT, OUTCOME, COST, NOTE}
PITCH_VERDICTS = {"adopt", "decline_forever", "decline_for_now", "park"}


def new_id() -> str:
    return f"{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _tokens(slug: str) -> set[str]:
    return {t for t in slug.split("-") if len(t) > 2}


def similar(slug_a: str, slug_b: str, threshold: float = 0.6) -> bool:
    if slug_a == slug_b:
        return True
    ta, tb = _tokens(slug_a), _tokens(slug_b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._last_hash = GENESIS
        if os.path.exists(path):
            with open(path, "rb") as f:
                for raw in f:
                    line = raw.rstrip(b"\n")
                    if line:
                        self._last_hash = hashlib.sha256(line).hexdigest()

    def append(self, etype: str, payload: dict, parent_id: str | None = None,
               actor: str = "minerva") -> dict:
        if etype not in VALID_TYPES:
            raise ValueError(f"unknown entry type: {etype}")
        entry = {"id": new_id(), "ts": time.time(), "type": etype, "actor": actor,
                 "parent_id": parent_id, "payload": payload, "prev_hash": self._last_hash}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
        self._last_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return entry

    def entries(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
        return out

    def verify_chain(self) -> tuple[bool, int, int | None]:
        """Returns (ok, n_entries, first_bad_line_number_or_None)."""
        if not os.path.exists(self.path):
            return True, 0, None
        prev = GENESIS
        n = 0
        with open(self.path, "rb") as f:
            for i, raw in enumerate(f, start=1):
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    return False, n, i
                if entry.get("prev_hash") != prev:
                    return False, n, i
                prev = hashlib.sha256(line).hexdigest()
                n += 1
        return True, n, None


class ProposalIndex:
    """Derived state over pitch entries — the anti-nag machine (spec §4).

    States: open | adopted | dead | cooling(until) | parked | tried.
    can_pitch() is the single gate the brainstorm drive must pass before
    the operator ever sees an idea. Repetition is a bug.
    """

    def __init__(self, ledger: Ledger,
                 max_open_per_domain: int = 2,
                 min_interval_s: float = 24 * 3600.0) -> None:
        self.ledger = ledger
        self.max_open_per_domain = max_open_per_domain
        self.min_interval_s = min_interval_s

    def _scan(self) -> dict[str, dict]:
        pitches: dict[str, dict] = {}
        for e in self.ledger.entries():
            if e["type"] == PITCH:
                p = e["payload"]
                pitches[e["id"]] = {"id": e["id"], "ts": e["ts"],
                                    "slug": p.get("slug") or slugify(p.get("title", "")),
                                    "domain": p.get("domain", "general"),
                                    "state": "open", "until": None}
            elif e["type"] == PITCH_VERDICT and e.get("parent_id") in pitches:
                rec, p = pitches[e["parent_id"]], e["payload"]
                v = p.get("verdict")
                if v == "adopt":
                    rec["state"] = "adopted"
                elif v == "decline_forever":
                    rec["state"] = "dead"
                elif v == "decline_for_now":
                    rec["state"] = "cooling"
                    rec["until"] = e["ts"] + float(p.get("cooldown_days", 30)) * 86400.0
                elif v == "park":
                    rec["state"] = "parked"
            elif e["type"] == OUTCOME and e.get("parent_id") in pitches:
                pitches[e["parent_id"]]["state"] = "tried"
            elif e["type"] == NOTE and e.get("parent_id") in pitches:
                if e["payload"].get("reopen"):  # changed circumstances, explicit
                    pitches[e["parent_id"]]["state"] = "reopened"
        return pitches

    def state(self, pitch_id: str, now: float | None = None) -> str | None:
        rec = self._scan().get(pitch_id)
        if rec is None:
            return None
        return self._effective(rec, now or time.time())

    @staticmethod
    def _effective(rec: dict, now: float) -> str:
        if rec["state"] == "cooling" and rec["until"] is not None and now >= rec["until"]:
            return "expired"  # cooldown over -> pitchable again
        return rec["state"]

    def can_pitch(self, domain: str, title_or_slug: str,
                  now: float | None = None) -> tuple[bool, str]:
        now = now or time.time()
        slug = slugify(title_or_slug)
        open_in_domain = 0
        latest_in_domain = 0.0
        for rec in self._scan().values():
            eff = self._effective(rec, now)
            if rec["domain"] == domain:
                if eff == "open":
                    open_in_domain += 1
                latest_in_domain = max(latest_in_domain, rec["ts"])
            if similar(slug, rec["slug"]):
                if eff == "dead":
                    return False, f"declined forever ({rec['id']})"
                if eff == "cooling":
                    return False, f"cooling until {time.strftime('%Y-%m-%d', time.localtime(rec['until']))} ({rec['id']})"
                if eff == "open":
                    return False, f"already open ({rec['id']})"
                if eff == "parked":
                    return False, f"parked — needs explicit reopen note ({rec['id']})"
                if eff == "adopted":
                    return False, f"already adopted ({rec['id']})"
        if open_in_domain >= self.max_open_per_domain:
            return False, f"pitch budget: {open_in_domain} open in '{domain}'"
        if latest_in_domain and now - latest_in_domain < self.min_interval_s:
            return False, f"pitch interval: last '{domain}' pitch < {int(self.min_interval_s/3600)}h ago"
        return True, "ok"

    # convenience writers ------------------------------------------------
    def pitch(self, domain: str, title: str, detail: str = "") -> dict:
        ok, reason = self.can_pitch(domain, title)
        if not ok:
            raise PermissionError(f"anti-nag: {reason}")
        return self.ledger.append(PITCH, {"domain": domain, "title": title,
                                          "slug": slugify(title), "detail": detail})

    def verdict(self, pitch_id: str, verdict: str, reason: str = "",
                cooldown_days: float = 30, reopen_condition: str = "") -> dict:
        if verdict not in PITCH_VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(PITCH_VERDICTS)}")
        payload = {"verdict": verdict, "reason": reason}
        if verdict == "decline_for_now":
            payload["cooldown_days"] = cooldown_days
        if verdict == "park":
            payload["reopen_condition"] = reopen_condition
        return self.ledger.append(PITCH_VERDICT, payload, parent_id=pitch_id, actor="operator")
