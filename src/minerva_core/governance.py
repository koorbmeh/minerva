"""GovernanceGate v0.1 — the host-side trust boundary (spec §3).

Engines are untrusted; every tool call passes here BEFORE execution.
This is deliberately a conservative placeholder: the warden (second-model
policy audit) and operator-tuned policy tables replace the guts later (Track N).
The check->ledger->verdict loop it establishes is permanent — and every
verdict the operator gives is self-model training data (the flywheel).

v0.1 adds APPROVAL-HONORING RETRY (2026-06-11). The gate owns the whole
approvals interplay so the hash-chained ledger keeps a single appender (this
gateway process):
  * escalated calls are FINGERPRINTED (sha256 of canonical {tool,args});
  * an identical call while the ask is pending does NOT re-queue (anti-spam) —
    the engine is told it's still awaiting the operator;
  * once the operator records "approved" (host_loop -> approvals.jsonl), the next
    identical call is ALLOWED once: the gate appends a "consumed" line and
    ledgers a VERDICT parented to the original ask entry;
  * "denied" STANDS for that fingerprint (denial is an answer, not an
    obstacle — anti-nag philosophy); changed arguments = new fingerprint =
    fresh escalation.
approvals_path=None disables all of this (legacy v0 behavior, used by older
tests); the gateway entry point wires it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass

from .killswitch import is_stopped
from .ledger import ACTION, ASK, VERDICT, Ledger

ALLOW, ASK_OPERATOR, DENY = "allow", "ask", "deny"

# action classes that always escalate in v0 (architecture §3, the ask-triggers row)
ASK_CLASSES = {"irreversible", "spend", "outbound_comms", "cross_person",
               "physical", "medical_consequential", "legal_consequential"}


def _is_operator_directed(args: dict | None) -> bool:
    """An action the operator explicitly directed - origin 'operator' or an adopted pitch
    ('pitch:<id>'). The inline warden never second-guesses these (round 27)."""
    o = str((args or {}).get("origin", "")).strip().lower()
    return o == "operator" or o.startswith("pitch:")


@dataclass
class GovernanceDecision:
    decision: str          # allow | ask | deny
    reason: str
    entry_id: str          # ledger entry for audit / parent of the operator's verdict
    already_queued: bool = False   # ask: pending line exists; gateway must not re-queue
    awaiting_ref: str = ""         # ask: the original pending ref


class GovernanceGate:
    def __init__(self, ledger: Ledger, spend_ask_threshold_usd: float = 5.0,
                 deny_tools: set[str] | None = None,
                 approvals_path: str | None = None,
                 spend_meter=None, state_dir: str | None = None,
                 inline_reviewer=None, inline_classes: set[str] | None = None,
                 inline_fail: str = "escalate",
                 inline_mode: str = "advisory") -> None:
        self.ledger = ledger
        self.spend_ask_threshold_usd = spend_ask_threshold_usd
        self.deny_tools = deny_tools or set()
        self.approvals_path = approvals_path
        self.spend_meter = spend_meter        # round 15: budget governor (G4)
        self.state_dir = state_dir            # round 16: kill-switch home
        # round 21: inline warden (pre-action veto). Reviewer is a callable
        # (tool, args, classes, est_cost_usd) -> ("pass"|"veto"|"unavailable",
        # reason). Runs ONLY on the would-be-ALLOW path for opted-in classes;
        # a veto ESCALATES through the ask rail (the operator stays the authority).
        self.inline_reviewer = inline_reviewer
        self.inline_classes = inline_classes or set()
        self.inline_fail = inline_fail if inline_fail in ("escalate", "allow") \
            else "escalate"
        # round 27: advisory (default) = the warden informs, never overrules;
        # block = the r21 veto-escalates behavior (opt-in).
        self.inline_mode = inline_mode if inline_mode in ("advisory", "block") \
            else "advisory"

    # ---- approvals interplay (v0.1) -------------------------------------
    @staticmethod
    def fingerprint(tool: str, args: dict) -> str:
        canon = json.dumps({"args": args, "tool": tool}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canon.encode("ascii")).hexdigest()

    def _approvals_state(self, fp: str) -> tuple[str | None, str, str]:
        """(state, ref, note) for this fingerprint from the append-only log.
        Latest line per ref wins; consumed refs are closed cycles (ignored).
        Across refs: approved > pending > denied > none."""
        if not self.approvals_path or not os.path.exists(self.approvals_path):
            return None, "", ""
        asks: dict[str, str] = {}        # ref -> fingerprint (from the pending line)
        latest: dict[str, tuple[str, str]] = {}
        order: list[str] = []
        with open(self.approvals_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ref = rec.get("ref")
                if not ref:
                    continue
                if rec.get("tool") is not None and ref not in asks:
                    asks[ref] = self.fingerprint(rec["tool"], rec.get("args") or {})
                    order.append(ref)
                latest[ref] = (rec.get("status", "pending"), rec.get("note", ""))
        approved, pending, denied = [], [], []
        for ref in order:
            if asks.get(ref) != fp:
                continue
            st, note = latest.get(ref, ("pending", ""))
            if st == "approved":
                approved.append((ref, note))
            elif st == "pending":
                pending.append((ref, note))
            elif st == "denied":
                denied.append((ref, note))
            # "consumed": closed cycle, ignore
        for bucket, name in ((approved, "approved"), (pending, "pending"),
                             (denied, "denied")):
            if bucket:
                ref, note = bucket[-1]               # newest matching ref
                return name, ref, note
        return None, "", ""

    def _consume(self, ref: str) -> None:
        with open(self.approvals_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ref": ref, "ts": time.time(),
                                "status": "consumed", "by": "gateway"},
                               ensure_ascii=True) + "\n")

    # ---- the gate ---------------------------------------------------------
    def check(self, tool: str, args: dict, classes: set[str] | None = None,
              est_cost_usd: float = 0.0, session_id: str = "") -> GovernanceDecision:
        """classes: action classes claimed by the tool's registration metadata.
        Tools self-describe at registration; the gateway, not the engine,
        owns that metadata."""
        classes = classes or set()
        ctx = {"tool": tool, "args_keys": sorted(args.keys()), "classes": sorted(classes),
               "est_cost_usd": est_cost_usd, "session_id": session_id}

        if self.state_dir:                       # round 16: STOP beats all
            stopped = is_stopped(self.state_dir)
            if stopped:
                reason = ("KILL SWITCH is on - " + stopped + ". Nothing runs "
                          "until the operator clears it (Minerva - Resume.bat or "
                          "delete state/STOP).")
                e = self.ledger.append(ACTION, {**ctx, "decision": DENY,
                                                "reason": reason})
                return GovernanceDecision(DENY, reason, e["id"])

        if tool in self.deny_tools:
            e = self.ledger.append(ACTION, {**ctx, "decision": DENY, "reason": "deny-listed"})
            return GovernanceDecision(DENY, "deny-listed", e["id"])

        over_budget, b_spent, b_cap = False, 0.0, 0.0
        if self.spend_meter is not None and est_cost_usd > 0:
            ok_b, b_spent, b_cap = self.spend_meter.check(est_cost_usd)
            over_budget = not ok_b               # round 15: cumulative cap

        if (classes & ASK_CLASSES
                or est_cost_usd > self.spend_ask_threshold_usd or over_budget):
            if classes & ASK_CLASSES:
                why = "action class: " + ",".join(sorted(classes & ASK_CLASSES))
            elif est_cost_usd > self.spend_ask_threshold_usd:
                why = (f"cost ${est_cost_usd:.2f} > "
                       f"${self.spend_ask_threshold_usd:.2f}")
            else:
                why = (f"daily budget: ${b_spent:.2f} already spent in 24h; "
                       f"this ${est_cost_usd:.2f} call would exceed the "
                       f"${b_cap:.2f} cap (MINERVA_DAILY_BUDGET_USD)")
            return self._escalate(tool, args, ctx, why)

        # ---- inline warden on the would-be-ALLOW path (round 21; round 27:
        #      advisory-by-default + operator-directed exemption, so it can NEVER
        #      overrule the operator) -----------------------------------------------
        if (self.inline_reviewer is not None
                and classes & self.inline_classes):
            if _is_operator_directed(args):
                # His own directive (origin operator / an adopted pitch) is HIS
                # call, never the warden's - skip review entirely.
                e = self.ledger.append(ACTION, {**ctx, "decision": ALLOW,
                    "reason": "inline-warden skipped (operator-directed)"})
                return GovernanceDecision(
                    ALLOW, "inline-warden skipped (operator-directed)", e["id"])
            try:
                verdict, w_reason = self.inline_reviewer(tool, args, classes,
                                                         est_cost_usd)
            except Exception as exc:                          # noqa: BLE001
                verdict, w_reason = "unavailable", f"reviewer crashed: {exc!r}"[:200]
            if verdict == "pass":
                e = self.ledger.append(ACTION, {**ctx, "decision": ALLOW,
                                                "reason": "inline-warden pass"})
                return GovernanceDecision(ALLOW, "inline-warden pass", e["id"])
            if self.inline_mode == "advisory":
                # ADVISORY: the warden INFORMS, never overrules. Allow the call
                # and record the concern (the post-hoc warden + the audit
                # surface it to the operator); nothing is blocked or escalated.
                tag = "veto" if verdict == "veto" else "unavailable"
                e = self.ledger.append(ACTION, {**ctx, "decision": ALLOW,
                    "reason": f"inline-warden ADVISORY {tag} (allowed, not "
                              f"blocking): {w_reason}", "advisory": True})
                return GovernanceDecision(
                    ALLOW, f"allowed (warden advisory {tag}: {w_reason})", e["id"])
            # block mode (opt-in): the round-21 behavior.
            if verdict == "veto":
                return self._escalate(tool, args, ctx,
                                      "inline warden veto: " + w_reason)
            if self.inline_fail == "escalate":     # unavailable, fail-safe
                return self._escalate(tool, args, ctx,
                                      "inline warden unavailable (fail-safe): "
                                      + w_reason)
            e = self.ledger.append(ACTION, {**ctx, "decision": ALLOW,
                                            "reason": "inline warden unavailable "
                                                      "(fail=allow): " + w_reason})
            return GovernanceDecision(ALLOW, "allowed; inline warden was "
                                             "unavailable (fail=allow)", e["id"])

        e = self.ledger.append(ACTION, {**ctx, "decision": ALLOW, "reason": "default-allow (v0)"})
        return GovernanceDecision(ALLOW, "default-allow (v0)", e["id"])

    def _escalate(self, tool: str, args: dict, ctx: dict,
                  why: str) -> GovernanceDecision:
        """The ask rail (v0.1 interplay, verbatim) - shared by ask-classes,
        budget breaches, and round-21 inline-warden vetoes, so approve-once /
        standing-denial / anti-spam semantics are identical everywhere."""
        if self.approvals_path:
            fp = self.fingerprint(tool, args)
            state, ref, note = self._approvals_state(fp)
            if state == "approved":              # single-use: consume, VERDICT, allow
                self._consume(ref)
                reason = f"operator approved (ref {ref})"
                e = self.ledger.append(VERDICT, {**ctx, "decision": ALLOW,
                                                 "reason": reason, "note": note,
                                                 "fingerprint": fp}, parent_id=ref,
                                       actor="operator")
                return GovernanceDecision(ALLOW, reason, e["id"])
            if state == "denied":                # standing denial per fingerprint
                reason = f"operator denied (ref {ref})" + (f": {note}" if note else "")
                e = self.ledger.append(ACTION, {**ctx, "decision": DENY,
                                                "reason": reason, "fingerprint": fp},
                                       parent_id=ref)
                return GovernanceDecision(DENY, reason, e["id"])
            if state == "pending":               # don't re-queue; engine stands by
                e = self.ledger.append(ASK, {**ctx, "decision": ASK_OPERATOR,
                                             "reason": why, "fingerprint": fp,
                                             "awaiting": ref}, parent_id=ref)
                return GovernanceDecision(ASK_OPERATOR, why, e["id"],
                                          already_queued=True, awaiting_ref=ref)
            e = self.ledger.append(ASK, {**ctx, "decision": ASK_OPERATOR,
                                         "reason": why, "fingerprint": fp})
            return GovernanceDecision(ASK_OPERATOR, why, e["id"])
        e = self.ledger.append(ASK, {**ctx, "decision": ASK_OPERATOR, "reason": why})
        return GovernanceDecision(ASK_OPERATOR, why, e["id"])
