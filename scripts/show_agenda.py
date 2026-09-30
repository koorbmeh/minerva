"""Her standing agenda, on one screen (round 23) - "what is she working on?"

Read-only over state/agenda.jsonl: open items oldest first with ids, ages and
last progress notes, then the last few finished/dropped ones. Run via
"Minerva - Agenda.bat". Never writes anything. Stdlib only. ASCII.
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from minerva_core import agenda  # noqa: E402

STATE = os.environ.get("MINERVA_STATE_DIR", "").strip() or \
    os.path.join(ROOT, "state")
RECENT_CLOSED = 5


def say(s: str) -> None:
    print(str(s).encode("ascii", "backslashreplace").decode("ascii"))


def main() -> int:
    say("MINERVA'S STANDING AGENDA - " + time.strftime("%Y-%m-%d %H:%M"))
    say("(her own between-sessions task queue: ticks move it, chat answers "
        "from it)")
    say("")
    path = agenda.agenda_path(STATE)
    items = agenda.fold(path)
    opens = agenda.open_items(items)
    if not opens:
        say("  nothing open - she queues standing work as it lands (adopted")
        say("  pitches, things you hand her in chat, multi-tick jobs).")
    else:
        say(f"  OPEN ({len(opens)} of cap {agenda.max_open()}), oldest first:")
        for i in opens:
            age_d = max(0.0, (time.time() - i["ts"]) / 86400.0)
            say(f"   [{agenda.short_id(i['id'])}] {age_d:5.1f}d  {i['title']}")
            if i["origin"] and i["origin"] != "engine":
                say(f"            from: {i['origin']}")
            if i["notes"]:
                say(f"            last: {i['notes'][-1]['note'][:150]}")
    closed = sorted((i for i in items.values() if i["status"] != "open"),
                    key=lambda i: i["updated_ts"], reverse=True)
    if closed:
        say("")
        say(f"  recently finished/dropped (last {min(len(closed), RECENT_CLOSED)}):")
        for i in closed[:RECENT_CLOSED]:
            note = i["notes"][-1]["note"][:100] if i["notes"] else ""
            say(f"   [{agenda.short_id(i['id'])}] ({i['status']}) {i['title']}"
                + (f" - {note}" if note else ""))
    say("")
    say("  to change it: just tell her in Discord (chat can add/update/finish")
    say("  items), or let her ticks work the queue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
