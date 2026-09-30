"""Gateway tools for the STANDING AGENDA (round 23) - her own task queue.

Three tools, governed like everything else (every call rides GovernanceGate
and lands in the hash-chained ledger): agenda_list (read), agenda_add and
agenda_update (class agenda_write - flippable into MINERVA_ASK_CLASSES /
MINERVA_INLINE_CLASSES the day the operator wants agenda changes approval-gated or
warden-reviewed; today they are allow-class: the file is HER OWN queue in
state/, no external effect).

Anti-flood rail: agenda_add is capped per GATEWAY PROCESS (one process = one
engine session), so a looping engine cannot stuff the queue in a single
session; the store's own MAX_OPEN cap bounds the total either way.

Stdlib only. Plain-language results (the model relays them to the operator).
"""
from __future__ import annotations

import os

from .. import agenda
from .registry import ToolRegistry, ToolSpec

DEFAULT_ADDS_PER_SESSION = 3
LIST_CAP = 25


def adds_per_session() -> int:
    try:
        return int(os.environ.get("MINERVA_AGENDA_ADDS_PER_SESSION", "").strip()
                   or DEFAULT_ADDS_PER_SESSION)
    except ValueError:
        return DEFAULT_ADDS_PER_SESSION


def register_agenda_tools(reg: ToolRegistry, agenda_path: str) -> None:
    session = {"adds": 0}        # per gateway process == per engine session

    def _list(a: dict) -> str:
        which = (str(a.get("status", "") or "open").strip().lower() or "open")
        if which == "open":
            try:
                n = int(a.get("max_items", 10) or 10)
            except (TypeError, ValueError):
                n = 10
            return agenda.summarize(agenda_path, max_items=max(1, min(n, LIST_CAP)))
        if which not in agenda.STATUSES and which != "all":
            return "status must be open, done, dropped, or all"
        items = sorted(agenda.fold(agenda_path).values(),
                       key=lambda i: i["updated_ts"], reverse=True)
        chosen = [i for i in items if which == "all" or i["status"] == which]
        if not chosen:
            return f"agenda: no {which} items"
        lines = [f"agenda ({which}): {len(chosen)} item(s), latest touched first:"]
        for i in chosen[:LIST_CAP]:
            line = f"- [{agenda.short_id(i['id'])}] ({i['status']}) {i['title']}"
            if i["notes"]:
                line += f" | last: {i['notes'][-1]['note'][:120]}"
            lines.append(line)
        if len(chosen) > LIST_CAP:
            lines.append(f"  (+{len(chosen) - LIST_CAP} more)")
        return "\n".join(lines)

    def _add(a: dict) -> str:
        cap = adds_per_session()
        if session["adds"] >= cap:
            return (f"refused: you already added {cap} item(s) this session - "
                    "work the queue you have")
        ev, msg = agenda.add(agenda_path, str(a.get("title", "")),
                             detail=str(a.get("detail", "") or ""),
                             origin=str(a.get("origin", "") or "engine"))
        if ev is not None:
            session["adds"] += 1
        return msg

    def _update(a: dict) -> str:
        return agenda.update(agenda_path, str(a.get("id", "")),
                             status=a.get("status"),
                             note=str(a.get("note", "") or ""))

    reg.register(ToolSpec(
        name="agenda_list",
        description=("YOUR standing agenda - the task list that persists "
                     "between your sessions. Default shows OPEN items oldest "
                     "first with ids and last progress notes. status: "
                     "open|done|dropped|all."),
        input_schema={"type": "object",
                      "properties": {"status": {"type": "string",
                                                "enum": ["open", "done",
                                                         "dropped", "all"]},
                                     "max_items": {"type": "integer"}}},
        handler=_list))

    reg.register(ToolSpec(
        name="agenda_add",
        description=("Add a NEW standing task to your agenda - work that "
                     "should survive this session (an idea the operator ADOPTED, a "
                     "multi-step job, something he asked you to keep doing). "
                     "NOT for one-shot observations (episodic_note) or new "
                     "ideas for the operator (pitch_idea). origin: who it came from "
                     "- 'operator', 'engine', or 'pitch:<pitch id>' for an "
                     "adopted pitch. Near-duplicates of open items are "
                     "refused; open items and per-session adds are capped."),
        input_schema={"type": "object",
                      "properties": {"title": {"type": "string"},
                                     "detail": {"type": "string"},
                                     "origin": {"type": "string"}},
                      "required": ["title"]},
        classes={"agenda_write"},
        handler=_add))

    reg.register(ToolSpec(
        name="agenda_update",
        description=("Move an agenda item by id (a unique id prefix works): "
                     "record progress (note only), finish it (status=done + "
                     "a note saying what ACTUALLY happened - evidence "
                     "required), drop it (status=dropped + why), or reopen "
                     "(status=open)."),
        input_schema={"type": "object",
                      "properties": {"id": {"type": "string"},
                                     "status": {"type": "string",
                                                "enum": ["open", "done",
                                                         "dropped"]},
                                     "note": {"type": "string"}},
                      "required": ["id"]},
        classes={"agenda_write"},
        handler=_update))
