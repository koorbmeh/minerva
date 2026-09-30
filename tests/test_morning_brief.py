"""Tests: morning brief v0 (round 20) - the adopted pitch, delivered.

Covers: kill-switch short-circuit (no engine), no-connections free skip (no
engine, safe to schedule pre-grant), happy path (notify_operator called, spend
metered, log+report written, lock released), forgot-notify fallback (outbox
append, warden record shape), lock-busy honesty (no engine), no-BYOK-key
refusal (privacy split: never the free lane), prompt/schedule sanity, and
the round-20 drive_tick cosmetics (request_connection counts as the pitch
attempt; schedule_command says 06:30 + 14:30).

Machine-independent: MINERVA_GMAIL_ENV / MINERVA_CALENDAR_ENV /
MINERVA_ANTHROPIC_ENV redirect all config reads into the tmp dir, so a real
filled config/ never changes results. Stdlib only; prints every check and
the final line.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from minerva_core.engine import SessionConfig            # noqa: E402
from minerva_core.events import EngineEvent, EventType   # noqa: E402

import drive_tick                                        # noqa: E402
import morning_brief                                     # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def ev(t: EventType, payload: dict) -> EngineEvent:
    return EngineEvent(t, "fake-1", payload)


class FakeSession:
    def __init__(self, events):
        self.id = "fake-1"
        self._events = events
        self.sent = ""
        self.closed = False

    def send(self, text: str):
        self.sent = text
        yield from self._events

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeAdapter:
    name = "fake"

    def __init__(self, events):
        self._events = events
        self.session = None
        self.cfg = None

    def capabilities(self):
        return {"frontier"}

    def create_session(self, cfg: SessionConfig) -> FakeSession:
        self.cfg = cfg
        self.session = FakeSession(self._events)
        return self.session


def factory_for(events):
    holder: dict = {}

    def factory(state_dir: str):
        a = FakeAdapter(events)
        holder["adapter"] = a
        return a, SessionConfig(persona=morning_brief.PERSONA,
                                tools=morning_brief.BRIEF_TOOLS), "fake"

    return factory, holder


def exploding_factory(state_dir: str):
    raise AssertionError("engine must not be created on this path")


def log_lines(td: str):
    p = os.path.join(td, morning_brief.LOG_NAME)
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p, encoding="utf-8")
            if x.strip()]


def gmail_on(td: str) -> str:
    p = os.path.join(td, "gmail.env")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("GMAIL_ADDRESS=t@example.com\nGMAIL_APP_PASSWORD=abcd\n")
    return p


ENV_KEYS = ("MINERVA_GMAIL_ENV", "MINERVA_CALENDAR_ENV",
            "MINERVA_ANTHROPIC_ENV", "ANTHROPIC_API_KEY",
            "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "CALENDAR_ICS_URL",
            "MINERVA_BRIEF_MODEL")


def main() -> None:
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    morning_brief.LOCK_TRIES = 1
    morning_brief.LOCK_WAIT_S = 0.01
    try:
        # ---------- kill switch: no engine, honest line ----------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = os.path.join(td, "nope.env")
            os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(td, "nope2.env")
            with open(os.path.join(td, "STOP"), "w") as f:
                f.write("test stop")
            r = morning_brief.run_brief(td, engine_factory=exploding_factory,
                                        quiet=True)
            check("killswitch: not ok", r["ok"] is False)
            check("killswitch: stop_reason", r["stop_reason"] == "killswitch")
            check("killswitch: logged", log_lines(td)[-1]["stop_reason"]
                  == "killswitch")
            check("killswitch: no outbox",
                  not os.path.exists(os.path.join(td, "outbox.jsonl")))

        # ---------- nothing connected: free skip, ok ------------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = os.path.join(td, "nope.env")
            os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(td, "nope2.env")
            r = morning_brief.run_brief(td, engine_factory=exploding_factory,
                                        quiet=True)
            check("no-conn: ok (expected state)", r["ok"] is True)
            check("no-conn: skipped", r.get("skipped") == "no-connections")
            check("no-conn: no spend",
                  not os.path.exists(os.path.join(td, "spend.jsonl")))
            check("no-conn: report written",
                  os.path.exists(os.path.join(td, morning_brief.REPORT_NAME)))

        # ---------- happy path ----------------------------------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_on(td)
            os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(td, "no.env")
            events = [
                ev(EventType.TOOL_CALL, {"tool": "email_unread", "args": {}}),
                ev(EventType.TOOL_RESULT, {"result": "2 unread of 2: ..."}),
                ev(EventType.TOOL_CALL, {"tool": "notify_operator",
                                         "args": {"message": "Morning brief"}}),
                ev(EventType.COST, {"usd": 0.0021}),
                ev(EventType.MESSAGE, {"text": "BRIEF-DONE 2 unread, 1 flagged"}),
                ev(EventType.DONE, {"stop_reason": "end_turn"}),
            ]
            factory, holder = factory_for(events)
            r = morning_brief.run_brief(td, engine_factory=factory, quiet=True)
            check("happy: ok", r["ok"] is True)
            check("happy: notify_called", r["notify_called"] is True)
            check("happy: no fallback", "fallback_delivered" not in r)
            check("happy: session closed", holder["adapter"].session.closed)
            check("happy: prompt carries the job",
                  "email_unread" in holder["adapter"].session.sent
                  and "BRIEF-DONE" in holder["adapter"].session.sent)
            check("happy: tools allowlist on session cfg",
                  holder["adapter"].cfg.tools == morning_brief.BRIEF_TOOLS)
            spend = [json.loads(x) for x in
                     open(os.path.join(td, "spend.jsonl"), encoding="utf-8")]
            check("happy: spend metered",
                  len(spend) == 1 and abs(spend[0]["usd"] - 0.0021) < 1e-9
                  and "morning brief" in spend[0]["what"])
            check("happy: log line ok", log_lines(td)[-1]["ok"] is True)
            check("happy: lock released",
                  drive_tick.acquire_lock(os.path.join(td, "drive-tick.lock")))
            drive_tick.release_lock(os.path.join(td, "drive-tick.lock"))

        # ---------- forgot notify -> fallback outbox -------------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_on(td)
            os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(td, "no.env")
            events = [
                ev(EventType.TOOL_CALL, {"tool": "email_unread", "args": {}}),
                ev(EventType.MESSAGE,
                   {"text": "Morning brief - Friday. All clear, zero unread."}),
                ev(EventType.DONE, {"stop_reason": "end_turn"}),
            ]
            factory, _h = factory_for(events)
            r = morning_brief.run_brief(td, engine_factory=factory, quiet=True)
            check("fallback: delivered", r.get("fallback_delivered") is True)
            check("fallback: still ok", r["ok"] is True)
            ob = [json.loads(x) for x in
                  open(os.path.join(td, "outbox.jsonl"), encoding="utf-8")]
            check("fallback: outbox record shape",
                  ob[0]["priority"] == "normal" and "ts" in ob[0]
                  and ob[0]["message"].startswith(
                      "[morning brief - fallback delivery]"))
            check("fallback: brief text rode along",
                  "All clear" in ob[0]["message"])

        # ---------- lock busy: honest, no engine -----------------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_on(td)
            os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(td, "no.env")
            lock = os.path.join(td, "drive-tick.lock")
            check("lockbusy: pre-acquired", drive_tick.acquire_lock(lock))
            r = morning_brief.run_brief(td, engine_factory=exploding_factory,
                                        quiet=True)
            drive_tick.release_lock(lock)
            check("lockbusy: not ok", r["ok"] is False)
            check("lockbusy: reason", r["stop_reason"] == "lock-busy")

        # ---------- no BYOK key: refusal, never the free lane ----------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_on(td)
            os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(td, "no.env")
            os.environ["MINERVA_ANTHROPIC_ENV"] = os.path.join(td, "no-key.env")
            r = morning_brief.run_brief(td, quiet=True)   # default factory
            check("nokey: not ok", r["ok"] is False)
            check("nokey: reason", r["stop_reason"] == "no-byok-key")
            check("nokey: honest error",
                  any("free model" in e for e in r["errors"]))
            check("nokey: lock released",
                  drive_tick.acquire_lock(os.path.join(td, "drive-tick.lock")))
            drive_tick.release_lock(os.path.join(td, "drive-tick.lock"))

        # ---------- prompt + schedule sanity ---------------------------------
        p = morning_brief.build_prompt()
        check("prompt: ascii", p == p.encode("ascii", "ignore").decode())
        check("prompt: weekday substituted", "{weekday}" not in p)
        check("prompt: privacy rule present", "DATA" in p)
        sc = morning_brief.schedule_command()
        check("schedule: 06:50 daily + /F", "06:50" in sc and "/F" in sc
              and "Minerva Morning Brief" in sc)

        # ---------- round-20 drive_tick cosmetics ----------------------------
        sc2 = drive_tick.schedule_command()
        check("tick schedule: 06:30 + 14:30 (no stale 09:30)",
              "06:30" in sc2 and "14:30" in sc2 and "09:30" not in sc2)
        check("tick schedule: PM task named",
              "Minerva Drive Tick PM" in sc2)
        src = open(os.path.join(ROOT, "scripts", "drive_tick.py"),
                   encoding="utf-8").read()
        check("tick counter: request_connection counts as pitch attempt",
              '"request_connection" in t' in src)
        check("tick counter: connection request logged counts as logged",
              '"connection request logged" in r' in src)

        # ---------- round-24: agenda_list wired into morning brief -----------
        check("agenda-brief: agenda_list in BRIEF_TOOLS",
              "agenda_list" in morning_brief.BRIEF_TOOLS)
        check("agenda-brief: prompt step 3b calls agenda_list",
              "agenda_list" in morning_brief.BRIEF_PROMPT)
        check("agenda-brief: prompt brief shape has Agenda: line",
              "Agenda:" in morning_brief.BRIEF_PROMPT)
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_on(td)
            os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(td, "no.env")
            events = [
                ev(EventType.TOOL_CALL, {"tool": "email_unread", "args": {}}),
                ev(EventType.TOOL_RESULT, {"result": "0 unread"}),
                ev(EventType.TOOL_CALL, {"tool": "agenda_list", "args": {}}),
                ev(EventType.TOOL_RESULT, {"result": "1 open: write weekly review"}),
                ev(EventType.TOOL_CALL, {"tool": "notify_operator",
                                         "args": {"message": "Morning brief - Friday. "
                                                  "All clear. Agenda: 1 open, top: "
                                                  "write weekly review"}}),
                ev(EventType.COST, {"usd": 0.0015}),
                ev(EventType.MESSAGE, {"text": "BRIEF-DONE all clear, 1 agenda item"}),
                ev(EventType.DONE, {"stop_reason": "end_turn"}),
            ]
            factory, holder = factory_for(events)
            r = morning_brief.run_brief(td, engine_factory=factory, quiet=True)
            check("agenda-brief: ok", r["ok"] is True)
            check("agenda-brief: agenda_list in session tools allowlist",
                  "agenda_list" in holder["adapter"].cfg.tools)
            check("agenda-brief: notify called with agenda text",
                  r["notify_called"] is True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
