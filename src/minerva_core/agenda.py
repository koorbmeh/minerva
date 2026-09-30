"""Standing agenda (round 23) - Minerva's persistent between-ticks task queue.

The vision seed ("barely finite tasks", motivated-lifeform loop): work that
SURVIVES a single session. Until now every tick woke up amnesiac about its own
intentions - it could pitch, but an ADOPTED pitch had nowhere to live except
Claude's build queue. Now she owns a queue: adopted pitches and standing work
land here, ticks see it in their seed and move items forward, chat answers
"what are you working on?" from it.

Mechanics: event-sourced JSONL at state/agenda.jsonl. Every line is one event
{ts, op: add|update, id, ...}; the current view is a fold over all events.
Append-only like every Minerva bookkeeping file - no rewrites, no deletes,
torn-line tolerant reader (a torn tail costs at most the torn event). This is
OPERATIONAL state, same trust tier as outbox.jsonl: the hash-chained ledger
remains the audit trail (every agenda_* TOOL CALL lands there like any call).

Honesty rails: MAX_OPEN cap (a refusal says finish or drop something first -
an agenda that only grows is a nag, not a queue); near-duplicate titles
refused against OPEN items (slug Jaccard - the anti-nag lesson); 'done' and
'dropped' REQUIRE a note (evidence, not vibes); title/detail/note clamps.

Stdlib only. ASCII out (cp1252 rule): events are written ensure_ascii.
"""
from __future__ import annotations

import json
import os
import time

from .ledger import new_id, similar, slugify

AGENDA_NAME = "agenda.jsonl"
STATUSES = {"open", "done", "dropped"}
TITLE_CAP = 200
DETAIL_CAP = 2000
NOTE_CAP = 500
ORIGIN_CAP = 80
DEFAULT_MAX_OPEN = 25


def max_open() -> int:
    try:
        return int(os.environ.get("MINERVA_AGENDA_MAX_OPEN", "").strip()
                   or DEFAULT_MAX_OPEN)
    except ValueError:
        return DEFAULT_MAX_OPEN


def agenda_path(state_dir: str) -> str:
    return os.path.join(state_dir, AGENDA_NAME)


def short_id(iid: str) -> str:
    """The display handle: the RANDOM tail of a ledger-style id. The time
    prefix is shared by anything created within ~4s, so the first 8 chars
    collide constantly; the tail is unique by construction."""
    return str(iid).rsplit("-", 1)[-1]


def _read_events(path: str) -> list[dict]:
    """READ-ONLY, torn/garbled lines skipped (operational state, not audit)."""
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except OSError:
        return out
    return out


def _append(path: str, ev: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=True, sort_keys=True) + "\n")


def fold(path: str) -> dict[str, dict]:
    """id -> current item view. Unknown ops/ids are skipped, duplicate adds
    keep the first (ids are unique by construction; tolerance is for damage)."""
    items: dict[str, dict] = {}
    for ev in _read_events(path):
        op = ev.get("op")
        iid = str(ev.get("id", "") or "")
        if op == "add":
            if not iid or iid in items:
                continue
            ts = float(ev.get("ts", 0.0) or 0.0)
            items[iid] = {"id": iid,
                          "title": str(ev.get("title", ""))[:TITLE_CAP],
                          "detail": str(ev.get("detail", ""))[:DETAIL_CAP],
                          "origin": str(ev.get("origin", "engine"))[:ORIGIN_CAP],
                          "status": "open", "ts": ts, "updated_ts": ts,
                          "notes": []}
        elif op == "update":
            it = items.get(iid)
            if it is None:
                continue
            ts = float(ev.get("ts", 0.0) or 0.0)
            status = ev.get("status")
            if status in STATUSES:
                it["status"] = status
                if status == "done":
                    it["done_ts"] = ts
            note = str(ev.get("note", "") or "").strip()
            if note:
                it["notes"].append({"ts": ts, "note": note[:NOTE_CAP]})
            it["updated_ts"] = ts
    return items


def open_items(items: dict[str, dict]) -> list[dict]:
    """Open items, OLDEST first (finish what you started)."""
    return sorted((i for i in items.values() if i["status"] == "open"),
                  key=lambda i: i["ts"])


def resolve_id(items: dict[str, dict], piece: str) -> tuple[dict | None, str]:
    """Exact id, else unique prefix, else unique CONTAINS (verdict id-piece
    convention) - so the short tail handle that add/list print resolves."""
    piece = str(piece or "").strip().lower()
    if not piece:
        return None, "no id given"
    if piece in items:
        return items[piece], ""
    hits = [i for k, i in items.items() if k.startswith(piece)]
    if not hits:
        hits = [i for k, i in items.items() if piece in k]
    if not hits:
        return None, f"no agenda item matches id '{piece}' (agenda_list shows ids)"
    if len(hits) > 1:
        ids = ", ".join(sorted(short_id(i["id"]) for i in hits))
        return None, f"id '{piece}' is ambiguous ({ids}) - use more characters"
    return hits[0], ""


def add(path: str, title: str, detail: str = "", origin: str = "engine",
        now: float | None = None) -> tuple[dict | None, str]:
    """Returns (event-or-None, plain-language result text)."""
    title = " ".join(str(title or "").split())[:TITLE_CAP]
    if not title:
        return None, "refused: empty title"
    items = fold(path)
    opens = open_items(items)
    slug = slugify(title)
    for i in opens:
        if similar(slug, slugify(i["title"])):
            return None, (f"refused: near-duplicate of open item "
                          f"{short_id(i['id'])} \"{i['title']}\" - "
                          "agenda_update that item instead")
    cap = max_open()
    if len(opens) >= cap:
        return None, (f"refused: agenda full ({len(opens)} open, cap {cap}) - "
                      "finish or drop something first")
    ev = {"ts": now if now is not None else time.time(), "op": "add",
          "id": new_id(), "title": title,
          "detail": str(detail or "")[:DETAIL_CAP],
          "origin": str(origin or "engine")[:ORIGIN_CAP]}
    _append(path, ev)
    return ev, f"added to agenda ({short_id(ev['id'])}): {title}"


def update(path: str, id_piece: str, status: str | None = None,
           note: str = "", now: float | None = None) -> str:
    """Progress note, finish (done - note required), drop (note required),
    or reopen an item. Plain-language result text either way."""
    items = fold(path)
    it, err = resolve_id(items, id_piece)
    if it is None:
        return "refused: " + err
    status = (str(status).strip().lower() or None) if status is not None else None
    if status is not None and status not in STATUSES:
        return ("refused: status must be one of open|done|dropped "
                "(omit it to just record progress)")
    note = " ".join(str(note or "").split())
    if status in ("done", "dropped") and not note:
        return (f"refused: marking an item {status} needs a note saying what "
                "actually happened (evidence, not vibes)")
    if status is None and not note:
        return "refused: nothing to update - pass a status and/or a note"
    ev: dict = {"ts": now if now is not None else time.time(), "op": "update",
                "id": it["id"]}
    if status is not None:
        ev["status"] = status
    if note:
        ev["note"] = note[:NOTE_CAP]
    _append(path, ev)
    verb = f"status -> {status}" if status is not None else "progress noted"
    tail = f" ({note[:120]})" if note else ""
    return f"agenda {short_id(it['id'])} \"{it['title'][:80]}\": {verb}{tail}"


def summarize(path: str, max_items: int = 5, now: float | None = None) -> str:
    """Plain one-screen summary of OPEN items, oldest first - the same text
    feeds the tick seed, the status line, and agenda_list's default view."""
    now = now if now is not None else time.time()
    opens = open_items(fold(path))
    if not opens:
        return "agenda: empty"
    lines = [f"agenda: {len(opens)} open (cap {max_open()}), oldest first:"]
    for i in opens[:max_items]:
        age_d = max(0.0, (now - i["ts"]) / 86400.0)
        line = f"- [{short_id(i['id'])}] ({age_d:.1f}d) {i['title']}"
        if i["origin"] and i["origin"] != "engine":
            line += f" (origin: {i['origin']})"
        if i["notes"]:
            line += f" | last: {i['notes'][-1]['note'][:120]}"
        lines.append(line)
    if len(opens) > max_items:
        lines.append(f"  (+{len(opens) - max_items} more open)")
    return "\n".join(lines)


def adopted_pitches_off_agenda(ledger_entries: list[dict],
                               items: dict[str, dict],
                               max_results: int = 3) -> list[tuple[str, str]]:
    """(pitch_id, title) for pitches the operator ADOPTED that no agenda item claims
    (origin == 'pitch:<id>', any status). The tick seed surfaces these so SHE
    puts adopted work on her own queue - the motivated-lifeform loop."""
    claimed = {i["origin"] for i in items.values()}
    pitches = {e.get("id"): (e.get("payload") or {})
               for e in ledger_entries if e.get("type") == "pitch"}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    # newest entry first: a pitch's LATEST verdict wins (mind-changes fold
    # as fresh entries)
    for e in reversed(ledger_entries):
        if e.get("type") != "pitch_verdict" or e.get("actor") != "operator":
            continue
        p = e.get("payload") or {}
        pid = str(e.get("parent_id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        if str(p.get("verdict", "")) != "adopt":
            continue
        if f"pitch:{pid}" in claimed:
            continue
        title = str(pitches.get(pid, {}).get("title", "")) or "(untitled pitch)"
        out.append((pid, title[:TITLE_CAP]))
        if len(out) >= max_results:
            break
    return out
