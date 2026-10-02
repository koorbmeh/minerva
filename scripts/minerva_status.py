"""One-glance status (round 16.1) - "is she okay?" in plain language.

Read-only over the live state dir: kill switch, budget, last tick, ledger
health, connections, and what is waiting on the operator. Run via
"Minerva - Status.bat". Never writes anything. Stdlib only. ASCII.
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from minerva_core import agenda as agenda_mod              # noqa: E402
from minerva_core.gateway import tools_connections as tc   # noqa: E402
from minerva_core.killswitch import is_stopped             # noqa: E402
from minerva_core.ledger import Ledger, ProposalIndex      # noqa: E402
from minerva_core.spend import SpendMeter, spend_path      # noqa: E402

STATE = os.environ.get("MINERVA_STATE_DIR", "").strip() or \
    os.path.join(ROOT, "state")


def say(s: str) -> None:
    print(str(s).encode("ascii", "backslashreplace").decode("ascii"))


def _tail_jsonl(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    last = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return last


def main() -> int:
    say("MINERVA STATUS - " + time.strftime("%Y-%m-%d %H:%M"))
    say("")

    stopped = is_stopped(STATE)
    if stopped:
        say(f"  KILL SWITCH: ON ({stopped})")
        say("    nothing runs until you clear it: Minerva - Resume.bat")
    else:
        say("  kill switch: off (she may run)")

    meter = SpendMeter(spend_path(STATE))
    say("  " + meter.status_text())

    tick = _tail_jsonl(os.path.join(STATE, "drive-tick-log.jsonl"))
    if tick is None:
        say("  last brainstorm tick: none recorded yet")
    else:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(tick.get("ts", 0)))
        ok = "OK" if tick.get("ok") else "FAILED/SKIPPED"
        extra = ""
        if tick.get("stop_reason") == "killswitch":
            extra = " (kill switch was on)"
        elif tick.get("pitch_logged"):
            extra = " (she pitched something - see Inbox)"
        say(f"  last brainstorm tick: {when} - {ok}{extra}")

    brief = _tail_jsonl(os.path.join(STATE, "morning-brief-log.jsonl"))
    if brief is None:
        say("  last morning brief: none yet (Minerva - Enable Morning Brief "
            "schedules it daily 06:50)")
    else:
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(brief.get("ts", 0)))
        ok = "OK" if brief.get("ok") else \
            f"DID NOT SEND ({brief.get('stop_reason', '?')})"
        usd = brief.get("usd")
        say(f"  last morning brief: {when} - {ok}"
            + (f" (${usd:.4f})" if usd else ""))

    me = _tail_jsonl(os.path.join(STATE, "mail-events.json")) or {}
    if os.environ.get("MINERVA_MAIL_EVENTS", "1").strip().lower() in \
            ("0", "off", "no", "false"):
        say("  mail alerts: OFF (MINERVA_MAIL_EVENTS=0)")
    elif not me:
        say("  mail alerts: armed; no polls recorded yet (runs inside "
            "Start Minerva)")
    else:
        n = int(me.get("dm_count_day", 0) or 0)
        last = float(me.get("last_dm_ts", 0.0) or 0.0)
        if last:
            say(f"  mail alerts: on - {n} ping(s) on {me.get('dm_day', '?')}, "
                "last at "
                + time.strftime("%H:%M", time.localtime(last)))
        else:
            say("  mail alerts: on - watching, no pings yet")

    ag = agenda_mod.fold(agenda_mod.agenda_path(STATE))
    ag_open = [i for i in ag.values() if i["status"] == "open"]
    if ag_open:
        oldest_d = (time.time() - min(i["ts"] for i in ag_open)) / 86400.0
        say(f"  her agenda: {len(ag_open)} open item(s), oldest "
            f"{oldest_d:.1f}d - Minerva - Agenda.bat shows them")
    else:
        say("  her agenda: empty (she queues standing work as it lands)")

    lp = os.path.join(STATE, "ledger.jsonl")
    if os.path.exists(lp):
        ok, n, bad = Ledger(lp).verify_chain()
        say(f"  ledger: {n} entries, chain "
            + ("INTACT" if ok else f"BROKEN at {bad} - find out why before trusting it"))
        try:
            proposals = ProposalIndex(Ledger(lp))
            open_p = [r for r in proposals._scan().values()
                      if r["state"] == "open"]
            if open_p:
                say(f"  waiting on YOU: {len(open_p)} pitch(es) without a "
                    "verdict - Minerva - Inbox.bat")
        except Exception:                                  # noqa: BLE001
            pass
    else:
        say("  ledger: none yet")

    ap = os.path.join(STATE, "approvals.jsonl")
    pending = 0
    if os.path.exists(ap):
        latest: dict = {}
        with open(ap, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ref"):
                    latest[rec["ref"]] = rec.get("status", "pending")
        pending = sum(1 for s in latest.values() if s == "pending")
    if pending:
        say(f"  waiting on YOU: {pending} approval(s) - Minerva - Inbox.bat")

    names = []
    for name, c in tc.CONNECTORS.items():
        names.append(name + (" (connected)" if c["configured"]() else
                             " (not connected)"))
    say("  connections: " + ", ".join(names))

    jobs = os.path.join(STATE, "workspace", "generated", "video-jobs.jsonl")
    j = _tail_jsonl(jobs)
    if j:
        say(f"  last video job: queue_id {j.get('queue_id', '?')} "
            f"({j.get('model', '?')}) - resumable ~24h if one ever strands")

    say("")
    say("  engine lock: " + ("HELD (an engine session is running right now)"
                             if os.path.exists(os.path.join(STATE, "drive-tick.lock"))
                             else "free"))
    hb = _tail_jsonl(os.path.join(STATE, "heartbeat.json"))
    if hb and hb.get("ts"):
        age = max(0.0, time.time() - float(hb["ts"]))
        try:
            stale = float(os.environ.get("MINERVA_WATCHDOG_STALE_S", "").strip()
                          or 180.0)
        except ValueError:
            stale = 180.0
        if age <= stale:
            say(f"  daemon: ALIVE (pulse {age:.0f}s ago)")
        else:
            say(f"  daemon: pulse STALE ({age:.0f}s) - is the Start Minerva "
                "window open? (the scheduled watchdog DMs you about this)")
    else:
        say("  daemon: no pulse recorded yet - open Start Minerva")
    return 0


if __name__ == "__main__":
    sys.exit(main())
