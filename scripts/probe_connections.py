"""Guided probe for the CONNECTIONS layer (round 14).

Run via "Minerva - Connections.bat". Shows what is connected and what is
dormant; for every ACTIVE connector it runs one real governed call through
the gateway in its own sandbox state dir (state/connprobe/): unread email
headers and/or upcoming calendar events print right here (your console -
nothing leaves the machine), with a ledger chain check. For DORMANT ones it
prints the exact enable steps. Report: state/connprobe-report.json.
Secrets never print. Plain ASCII. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from minerva_core.adapters.anthropic_byok import GatewayClient   # noqa: E402
from minerva_core.gateway import tools_connections as tc         # noqa: E402
from minerva_core.ledger import Ledger                           # noqa: E402

STATE = os.path.join(ROOT, "state")
PROBE_STATE = os.path.join(STATE, "connprobe")
REPORT = os.path.join(STATE, "connprobe-report.json")


def say(s: str) -> None:
    print(str(s).encode("ascii", "backslashreplace").decode("ascii"))


def main() -> int:
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    say("Connections:")
    active: list[str] = []
    for name, c in tc.CONNECTORS.items():
        if c["configured"]():
            say(f"  {name}: CONNECTED")
            active.append(name)
        else:
            say(f"  {name}: not connected - to enable:")
            say(f"    {c['enable']}")
    report["active"] = active
    if not active:
        say("")
        say("Nothing connected yet - that's fine. Minerva can see this too")
        say("(connections_status) and may ask you for one when it would help.")
        with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, indent=2)
        return 0

    say("")
    say("Testing the connected ones through the governed gateway...")
    os.makedirs(PROBE_STATE, exist_ok=True)
    gw = GatewayClient(
        [sys.executable, os.path.join(ROOT, "scripts", "run_gateway.py"),
         "--state-dir", PROBE_STATE],
        extra_env={"MINERVA_TOOLS":
                   "connections_status,email_unread,calendar_upcoming"})
    try:
        gw.initialize()
        if "gmail" in active:
            text, err = gw.call("email_unread", {"max_results": 5})
            report["email_ok"] = not err and "unread" in text
            say("")
            say("EMAIL (read-only - nothing gets marked read):")
            for line in text.splitlines()[:8]:
                say("  " + line)
        if "calendar" in active:
            text, err = gw.call("calendar_upcoming", {"days": 7})
            report["calendar_ok"] = not err and not text.startswith("refused")
            say("")
            say("CALENDAR (next 7 days):")
            for line in text.splitlines()[:12]:
                say("  " + line)
    finally:
        gw.close()
    ok_chain, n, _ = Ledger(os.path.join(PROBE_STATE,
                                         "ledger.jsonl")).verify_chain()
    report["probe_ledger_entries"] = n
    report["probe_chain_intact"] = ok_chain
    report["ok"] = bool(ok_chain
                        and all(report.get(k, True)
                                for k in ("email_ok", "calendar_ok")))
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2)
    say("")
    if report["ok"]:
        say(f"CONNECTIONS WORK (probe ledger: {n} entries, chain INTACT).")
        say("Restart Start Minerva and she can use them in chat.")
        return 0
    say(f"Something failed - detail: state\\connprobe-report.json")
    return 1


if __name__ == "__main__":
    sys.exit(main())
