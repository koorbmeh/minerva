"""Tests: mail-event lane v0 (round 21) - event-driven new-mail awareness.

Covers: quiet-hour + personal/bulk heuristics (unit), disabled/not-configured
free skips (no IMAP), kill-switch suppression, first-run cursor bootstrap
(never alert on old unread), personal-mail ping w/ outbox record shape,
cooldown + daily cap + quiet-hour suppression (cursor still advances -
deliberate no-backlog rule), lock-busy = no cursor advance (re-detect, never
lose), combined single DM, poll throttle, imap-error tolerance, and the
read-only EXAMINE guarantee on every open.

Machine-independent (MINERVA_GMAIL_ENV redirect; injected _imap_factory).
Stdlib only; prints every check and the final line.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from minerva_core.gateway import tools_connections as tc   # noqa: E402

import drive_tick                                          # noqa: E402
import mail_events                                         # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def hdr(frm: str, to: str = "operator@example.com", subject: str = "hi",
        unsub: str = "", prec: str = "") -> bytes:
    h = f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\n"
    if unsub:
        h += f"List-Unsubscribe: {unsub}\r\n"
    if prec:
        h += f"Precedence: {prec}\r\n"
    return (h + "\r\n").encode()


class FakeImap:
    def __init__(self, unseen, headers):
        self.unseen = list(unseen)
        self.headers = dict(headers)
        self.readonly_seen = None
        self.fetches = 0

    def login(self, a, p):
        return ("OK", [b""])

    def select(self, folder, readonly=False):
        self.readonly_seen = readonly
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [" ".join(str(u) for u in self.unseen).encode()])
        if cmd == "FETCH":
            self.fetches += 1
            u = int(args[0].decode())
            blob = self.headers.get(u, b"")
            if not blob:
                return ("OK", [None])
            return ("OK", [(b"x (UID y)", blob), b")"])
        return ("NO", [b""])

    def logout(self):
        return ("BYE", [b""])


class Factory:
    """Injectable tc._imap_factory; counts calls; can explode."""
    def __init__(self, client=None, explode=False):
        self.client = client
        self.explode = explode
        self.calls = 0

    def __call__(self, host):
        self.calls += 1
        if self.explode:
            raise AssertionError("IMAP must not be opened on this path")
        return self.client


def gmail_env(td: str) -> str:
    p = os.path.join(td, "gmail.env")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("GMAIL_ADDRESS=operator@example.com\nGMAIL_APP_PASSWORD=abcd\n")
    return p


def outbox_lines(td: str):
    p = os.path.join(td, "outbox.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]


def st(td: str) -> dict:
    return mail_events._load(mail_events.state_path(td))


def _at(day_offset: int, hour: int, minute: int = 0, second: int = 0) -> float:
    """A local timestamp ANCHORED TO THE REAL CLOCK - day_offset days after
    tomorrow, at the given local time.  The injected timeline below must never
    run BACKWARDS past time.time(): MINERVA_MAIL_EVENT_POLL_S is "0" in this
    suite, so a `now` earlier than the last real-clock poll trips the poll
    throttle and check() returns 0 before reaching the logic under test.  Fixed
    calendar dates (this suite used June 2026) pass while they are current and
    then quietly rot - the checks keep passing for the wrong reason until one of
    the cursor assertions catches it.
    """
    t = time.localtime(time.time() + 86400 * (1 + day_offset))
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                        hour, minute, second, 0, 0, -1))


ENV = {"MINERVA_GMAIL_ENV": None, "MINERVA_CALENDAR_ENV": None,
       "GMAIL_ADDRESS": None, "GMAIL_APP_PASSWORD": None,
       "MINERVA_MAIL_EVENTS": None, "MINERVA_MAIL_EVENT_POLL_S": "0",
       "MINERVA_MAIL_EVENT_COOLDOWN_S": "0",
       "MINERVA_MAIL_EVENT_DAILY_CAP": None,
       "MINERVA_MAIL_EVENT_QUIET": "off",
       "MINERVA_MAIL_TRIAGE": "0"}   # round 22: keep this suite on the plain-ping path


def main() -> None:
    saved = {k: os.environ.get(k) for k in ENV}
    saved_factory = tc._imap_factory
    for k, v in ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        # ---------- unit: quiet hours --------------------------------------
        at23 = time.mktime((2026, 6, 12, 23, 0, 0, 0, 0, -1))
        at12 = time.mktime((2026, 6, 12, 12, 0, 0, 0, 0, -1))
        at06 = time.mktime((2026, 6, 12, 6, 30, 0, 0, 0, -1))
        check("quiet: 23:00 inside 22-07", mail_events.in_quiet_hours(at23, "22-07"))
        check("quiet: 06:30 inside 22-07 (wraps midnight)",
              mail_events.in_quiet_hours(at06, "22-07"))
        check("quiet: noon outside 22-07",
              not mail_events.in_quiet_hours(at12, "22-07"))
        check("quiet: 'off' disables", not mail_events.in_quiet_hours(at23, "off"))
        check("quiet: equal bounds = never",
              not mail_events.in_quiet_hours(at23, "9-9"))

        # ---------- unit: person-vs-bulk -----------------------------------
        J = "operator@example.com"
        check("personal: named human to him",
              mail_events.is_personal("Sam Smith <sam@x.com>", J, J, "", ""))
        check("bulk: List-Unsubscribe never pings",
              not mail_events.is_personal("Sam <sam@x.com>", J, J,
                                          "<mailto:u@x.com>", ""))
        check("bulk: precedence bulk never pings",
              not mail_events.is_personal("Sam <sam@x.com>", J, J, "", "bulk"))
        check("bulk: noreply local part never pings",
              not mail_events.is_personal("Shop <no-reply@shop.com>", J, J, "", ""))
        check("bulk: newsletter local part never pings",
              not mail_events.is_personal("N <newsletter@x.com>", J, J, "", ""))
        check("bulk: not addressed to him never pings",
              not mail_events.is_personal("Sam <sam@x.com>",
                                          "list@group.com", J, "", ""))

        # ---------- disabled / not configured: free skips -------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            os.environ["MINERVA_MAIL_EVENTS"] = "0"
            tc._imap_factory = Factory(explode=True)
            check("disabled: no poll, returns 0",
                  mail_events.check(td, quiet=True) == 0)
            os.environ.pop("MINERVA_MAIL_EVENTS", None)
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = os.path.join(td, "missing.env")
            tc._imap_factory = Factory(explode=True)
            check("not configured: silent free skip",
                  mail_events.check(td, quiet=True) == 0)
            check("not configured: no state written",
                  not os.path.exists(mail_events.state_path(td)))

        # ---------- kill switch ---------------------------------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            with open(os.path.join(td, "STOP"), "w") as f:
                f.write("test")
            tc._imap_factory = Factory(explode=True)
            check("kill switch: no poll while STOPPED",
                  mail_events.check(td, quiet=True) == 0)

        # ---------- bootstrap then ping --------------------------------------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            fake = FakeImap([3, 5], {})
            tc._imap_factory = Factory(fake)
            check("bootstrap: first run alerts nothing",
                  mail_events.check(td, quiet=True) == 0)
            check("bootstrap: cursor = max unseen", st(td)["last_uid"] == 5)
            check("bootstrap: read-only EXAMINE used",
                  fake.readonly_seen is True)
            check("bootstrap: no headers fetched", fake.fetches == 0)

            fake = FakeImap([3, 5, 7],
                            {7: hdr("Ann Pal <ann@friends.org>",
                                    subject="lunch tomorrow?")})
            tc._imap_factory = Factory(fake)
            check("ping: personal mail queues ONE DM",
                  mail_events.check(td, quiet=True) == 1)
            ob = outbox_lines(td)
            check("ping: outbox record shape",
                  len(ob) == 1 and ob[0]["priority"] == "normal"
                  and ob[0]["message"].startswith("[mail]"))
            check("ping: sender + subject ride the DM (metadata only)",
                  "Ann Pal" in ob[0]["message"]
                  and "lunch tomorrow?" in ob[0]["message"])
            check("ping: cursor advanced", st(td)["last_uid"] == 7)
            check("ping: daily count = 1", st(td)["dm_count_day"] == 1)

            # ---------- cooldown: suppress AND advance ----------------------
            os.environ["MINERVA_MAIL_EVENT_COOLDOWN_S"] = "7200"
            fake = FakeImap([9], {9: hdr("Bob <bob@x.com>", subject="yo")})
            tc._imap_factory = Factory(fake)
            check("cooldown: suppressed", mail_events.check(td, quiet=True) == 0)
            check("cooldown: cursor still advanced (no backlog by design)",
                  st(td)["last_uid"] == 9)
            check("cooldown: outbox unchanged", len(outbox_lines(td)) == 1)
            os.environ["MINERVA_MAIL_EVENT_COOLDOWN_S"] = "0"

            # ---------- bulk-only poll: quiet, advance ----------------------
            fake = FakeImap([11], {11: hdr("Deals <deals@shop.com>",
                                           unsub="<http://u>")})
            tc._imap_factory = Factory(fake)
            check("bulk-only poll: no DM", mail_events.check(td, quiet=True) == 0)
            check("bulk-only poll: cursor advanced", st(td)["last_uid"] == 11)

            # ---------- daily cap -------------------------------------------
            s = st(td)
            s["dm_count_day"] = 5
            s["dm_day"] = time.strftime("%Y-%m-%d")
            mail_events._save(mail_events.state_path(td), s)
            fake = FakeImap([13], {13: hdr("Cara <cara@x.com>")})
            tc._imap_factory = Factory(fake)
            check("daily cap: suppressed at 5",
                  mail_events.check(td, quiet=True) == 0)
            check("daily cap: cursor advanced", st(td)["last_uid"] == 13)
            s = st(td)
            s["dm_count_day"] = 0
            mail_events._save(mail_events.state_path(td), s)

            # ---------- quiet hours (now injected) --------------------------
            os.environ["MINERVA_MAIL_EVENT_QUIET"] = "22-07"
            fake = FakeImap([15], {15: hdr("Dee <dee@x.com>")})
            tc._imap_factory = Factory(fake)
            check("quiet hours: suppressed at 23:00",
                  mail_events.check(td, now=_at(0, 23), quiet=True) == 0)
            check("quiet hours: cursor advanced", st(td)["last_uid"] == 15)
            os.environ["MINERVA_MAIL_EVENT_QUIET"] = "off"

            # ---------- lock busy: do NOT lose the event --------------------
            mail_events.LOCK_TRIES = 1
            mail_events.LOCK_WAIT_S = 0.01
            lock = os.path.join(td, "drive-tick.lock")
            check("lockbusy: pre-acquired", drive_tick.acquire_lock(lock))
            fake = FakeImap([17], {17: hdr("Eve <eve@x.com>", subject="urgent")})
            tc._imap_factory = Factory(fake)
            n = mail_events.check(td, now=_at(1, 12), quiet=True)
            drive_tick.release_lock(lock)
            check("lockbusy: no DM", n == 0)
            check("lockbusy: cursor NOT advanced (will re-detect)",
                  st(td)["last_uid"] == 15)
            fake = FakeImap([17], {17: hdr("Eve <eve@x.com>", subject="urgent")})
            tc._imap_factory = Factory(fake)
            check("lockfree retry: DM goes out",
                  mail_events.check(td, now=_at(1, 12, 1), quiet=True) == 1)
            check("lockfree retry: cursor now 17", st(td)["last_uid"] == 17)

            # ---------- combined single DM ----------------------------------
            fake = FakeImap([19, 20],
                            {19: hdr("F <f@x.com>", subject="a"),
                             20: hdr("G <g@x.com>", subject="b")})
            tc._imap_factory = Factory(fake)
            before = len(outbox_lines(td))
            check("combined: two personals -> ONE DM",
                  mail_events.check(td, now=_at(2, 12), quiet=True) == 1
                  and len(outbox_lines(td)) == before + 1)
            check("combined: both senders in the one message",
                  "f@x.com" in outbox_lines(td)[-1]["message"]
                  and "g@x.com" in outbox_lines(td)[-1]["message"])

            # ---------- poll throttle ---------------------------------------
            os.environ["MINERVA_MAIL_EVENT_POLL_S"] = "9999"
            f2 = Factory(FakeImap([21], {}))
            tc._imap_factory = f2
            check("throttle: within poll window no IMAP open",
                  mail_events.check(td, now=_at(2, 12, 0, 30), quiet=True) == 0
                  and f2.calls == 0)
            os.environ["MINERVA_MAIL_EVENT_POLL_S"] = "0"

            # ---------- imap failure tolerated ------------------------------
            class Boom:
                def __call__(self, host):
                    raise OSError("network down")
            tc._imap_factory = Boom()
            check("imap failure: quiet 0, no crash",
                  mail_events.check(td, now=_at(3, 12), quiet=True) == 0)
    finally:
        tc._imap_factory = saved_factory
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
