"""Kill switch (round 16) - the EXTERNAL stop the rails row was missing.

Before this, stopping Minerva meant closing windows and hoping. Now there is
ONE file: <state>/STOP. While it exists:
  * the gateway DENIES every tool call (any lane, any engine) - each denial
    is ledgered, so the audit trail shows exactly what she tried while
    stopped;
  * drive ticks refuse to start (no engine session, no spend);
  * chat answers "I'm stopped" WITHOUT opening an engine session (no tokens).

It is a file on purpose: it works when chat is wedged, when Discord is down,
when an engine is mid-loop (next tool call dies), and it survives reboots.
Double-click "Minerva - STOP.bat" to halt; "Minerva - Resume.bat" to clear.
Deleting the file by hand is equally valid - no magic.

Stdlib only. ASCII.
"""
from __future__ import annotations

import json
import os
import time

STOP_NAME = "STOP"


def stop_path(state_dir: str) -> str:
    return os.path.join(state_dir, STOP_NAME)


def is_stopped(state_dir: str) -> str | None:
    """The stop reason when halted, else None. An unreadable/empty STOP file
    still stops (fail SAFE - the file's existence is the switch)."""
    p = stop_path(state_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
        reason = str(rec.get("reason", "")).strip()
        ts = rec.get("ts")
        when = (time.strftime(" (since %Y-%m-%d %H:%M)", time.localtime(ts))
                if isinstance(ts, (int, float)) else "")
        return (reason or "no reason recorded") + when
    except (OSError, json.JSONDecodeError, ValueError):
        return "stop file present (unreadable)"


def stop(state_dir: str, reason: str = "", by: str = "operator") -> str:
    os.makedirs(state_dir, exist_ok=True)
    p = stop_path(state_dir)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"ts": time.time(), "reason": str(reason)[:300], "by": by}, f)
    return p


def resume(state_dir: str) -> bool:
    """True if a STOP file was removed."""
    try:
        os.remove(stop_path(state_dir))
        return True
    except OSError:
        return False
