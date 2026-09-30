"""Chat decision tools (round 27) - record the operator's decisions when he states
them in CONVERSATION.

Before this, a verdict/approval only registered through the Discord reply
GRAMMAR (approve/deny/adopt/park <id>) or the Inbox launcher. A natural-language
decision in chat ("I accept that", "park the calendar one", "approve the X")
went through the chat lane as conversation and recorded NOTHING - so the pitch
stayed open and the approval stayed pending (exactly what bit the operator 2026-06-15).
These three tools close that gap: Minerva resolves what the operator referenced,
records the decision with HIS attribution, and confirms it back to him.

GUARDRAIL: wire these on the CHAT lane ONLY (the operator is present and sees the
confirmation) - never the autonomous tick, so a brainstorm can't fabricate
the operator's verdicts. Provenance is explicit (note says "recorded from chat") and
everything is auditable (chat-log + the verdict/approval line + the hash-chained
ledger). record_verdict folds immediately (we ARE the single-appender gateway
process), so the decision takes effect at once.

  decisions_pending()  - what is awaiting the operator's call: open pitches + pending
                         approvals, with the ids/refs to reference. [read]
  record_verdict(pitch, verdict, note, reopen_condition)  - adopt | park |
                         decline_forever | decline_for_now on a pitch (by id or
                         a title fragment).
  resolve_approval(ref, decision, note)  - approve | deny a pending escalation
                         (by ref or the tool name).

Stdlib only. Plain-language results.
"""
from __future__ import annotations

import json
import os
import time

from .. import verdicts as verdicts_mod
from ..ledger import PITCH
from .registry import ToolRegistry, ToolSpec

OPEN_STATES = {"open", "reopened", "expired"}


def open_pitches(ledger, proposals, now: float | None = None) -> list[tuple[str, str, str]]:
    """[(pitch_id, domain, title)] for pitches awaiting a verdict."""
    now = now or time.time()
    entries = ledger.entries()
    out: list[tuple[str, str, str]] = []
    for e in entries:
        if e.get("type") != PITCH:
            continue
        p = e.get("payload") or {}
        st = proposals.state(e["id"], now)
        if st in OPEN_STATES:
            out.append((e["id"], str(p.get("domain", "")), str(p.get("title", ""))))
    return out


def resolve_pitch(open_list, ref: str) -> tuple[str, str, str]:
    """(pitch_id, title, err). Match by id-substring then title-substring."""
    ref = str(ref or "").strip().lower()
    if not ref:
        return "", "", "no pitch given"
    hits = [(pid, t) for (pid, _d, t) in open_list if ref in pid.lower()]
    if not hits:
        hits = [(pid, t) for (pid, _d, t) in open_list if ref in t.lower()]
    if not hits:
        return "", "", (f"no OPEN pitch matches '{ref}' - call decisions_pending "
                        "to see what is awaiting your verdict")
    if len(hits) > 1:
        names = "; ".join(f'"{t[:40]}"' for _, t in hits)
        return "", "", f"'{ref}' is ambiguous ({len(hits)}: {names}) - use more of the title or the id"
    return hits[0][0], hits[0][1], ""


def pending_approvals(approvals_path: str) -> list[tuple[str, str, str]]:
    """[(ref, tool, reason)] for asks whose latest status is 'pending'."""
    if not os.path.exists(approvals_path):
        return []
    asks: dict[str, tuple[str, str]] = {}
    latest: dict[str, str] = {}
    order: list[str] = []
    try:
        with open(approvals_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ref = r.get("ref")
                if not ref:
                    continue
                if r.get("tool") is not None and ref not in asks:
                    asks[ref] = (str(r.get("tool")), str(r.get("reason", "")))
                    order.append(ref)
                latest[ref] = r.get("status", "pending")
    except OSError:
        return []
    return [(ref, asks[ref][0], asks[ref][1]) for ref in order
            if latest.get(ref) == "pending" and ref in asks]


def resolve_ref(pend, piece: str) -> tuple[str, str]:
    piece = str(piece or "").strip().lower()
    if not piece:
        return "", "no approval given"
    hits = [ref for (ref, _t, _r) in pend if piece in ref.lower()]
    if not hits:
        hits = [ref for (ref, tool, _r) in pend if piece in str(tool).lower()]
    if not hits:
        return "", f"no PENDING approval matches '{piece}' - call decisions_pending"
    if len(hits) > 1:
        return "", f"'{piece}' is ambiguous ({len(hits)}) - use the exact ref"
    return hits[0], ""


def register_decision_tools(reg: ToolRegistry, ledger, proposals,
                            verdicts_path: str, approvals_path: str) -> None:

    def _pending(a: dict) -> str:
        op = open_pitches(ledger, proposals)
        pa = pending_approvals(approvals_path)
        lines: list[str] = []
        if op:
            lines.append(f"Pitches awaiting your verdict ({len(op)}):")
            for pid, domain, title in op:
                lines.append(f'  - [{pid}] ({domain}) "{title}"')
        if pa:
            lines.append(f"Escalations awaiting approve/deny ({len(pa)}):")
            for ref, tool, reason in pa:
                lines.append(f"  - [{ref}] {tool}: {str(reason)[:100]}")
        return "\n".join(lines) if lines else "Nothing is awaiting your decision right now."

    def _verdict(a: dict) -> str:
        pid, title, err = resolve_pitch(open_pitches(ledger, proposals),
                                        str(a.get("pitch", "")))
        if not pid:
            return "refused: " + err
        v = verdicts_mod.canonical_verdict(str(a.get("verdict", "")))
        if v is None:
            return ("refused: verdict must be adopt | park | decline_forever | "
                    "decline_for_now (aliases: yes/never/later/not-now)")
        note = (str(a.get("note", "") or "").strip()
                + " (recorded from chat on the operator's behalf)").strip()
        verdicts_mod.record_verdict(verdicts_path, pid, v, note=note,
                                    reopen_condition=str(a.get("reopen_condition", "") or ""),
                                    by="operator")
        try:                          # we ARE the single-appender gateway -> fold now
            verdicts_mod.fold(ledger, proposals, verdicts_path)
        except Exception:             # noqa: BLE001
            pass
        return f'recorded your verdict on "{title[:60]}": {v}'

    def _approval(a: dict) -> str:
        ref, err = resolve_ref(pending_approvals(approvals_path), str(a.get("ref", "")))
        if not ref:
            return "refused: " + err
        dec = str(a.get("decision", "")).strip().lower()
        if dec in ("approve", "approved", "yes", "ok", "allow"):
            status = "approved"
        elif dec in ("deny", "denied", "no", "reject", "decline"):
            status = "denied"
        else:
            return "refused: decision must be approve or deny"
        note = (str(a.get("note", "") or "").strip()
                + " (recorded from chat on the operator's behalf)").strip()
        try:
            with open(approvals_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"ref": ref, "ts": time.time(),
                                    "status": status, "by": "operator", "note": note},
                                   ensure_ascii=True) + "\n")
        except OSError as exc:
            return f"refused: could not record ({exc})"
        return f"recorded your decision on {ref}: {status}"

    reg.register(ToolSpec(
        name="decisions_pending",
        description=("What is awaiting the operator's decision right now: open pitches "
                     "(need a verdict) and escalations (need approve/deny), with "
                     "the ids/refs to reference. Call this when he asks what's "
                     "pending, or before recording a decision so you have the id."),
        input_schema={"type": "object", "properties": {}},
        handler=_pending))

    reg.register(ToolSpec(
        name="record_verdict",
        description=("Record the operator's VERDICT on a pitch when he tells you in "
                     "chat (e.g. 'I accept that' -> adopt; 'park the calendar "
                     "one' -> park; 'never' -> decline_forever; 'not now' -> "
                     "decline_for_now). pitch = the pitch id OR a few words of "
                     "its title. ONLY record a decision he EXPLICITLY stated, "
                     "then tell him what you recorded. reopen_condition is "
                     "optional (for park)."),
        input_schema={"type": "object",
                      "properties": {"pitch": {"type": "string"},
                                     "verdict": {"type": "string",
                                                 "enum": ["adopt", "park",
                                                          "decline_forever",
                                                          "decline_for_now"]},
                                     "note": {"type": "string"},
                                     "reopen_condition": {"type": "string"}},
                      "required": ["pitch", "verdict"]},
        classes={"decision_write"},
        handler=_verdict))

    reg.register(ToolSpec(
        name="resolve_approval",
        description=("Record the operator APPROVING or DENYING a pending escalation when "
                     "he says so in chat. ref = the approval ref OR the tool name "
                     "it is waiting on. decision = approve | deny. ONLY record "
                     "what he explicitly decided, then confirm it to him."),
        input_schema={"type": "object",
                      "properties": {"ref": {"type": "string"},
                                     "decision": {"type": "string",
                                                  "enum": ["approve", "deny"]},
                                     "note": {"type": "string"}},
                      "required": ["ref", "decision"]},
        classes={"decision_write"},
        handler=_approval))
