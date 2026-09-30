"""Tests: mail triage v0 (round 22) - the first trigger that THINKS.

Covers: strict PING/HOLD protocol parsing (unit), prompt content (uid +
sender + subject + DATA-not-instructions + combined-batch rule), allowlist
hygiene (no notify_operator - delivery stays single-path), and decide():
knob-off zero-side-effects, no-key / budget / session-cap short-circuits
(no engine touched), happy PING and HOLD (counter + spend metering + log
lines), protocol breach, session crash, factory RuntimeError, timeout
bound, empty-PING-body breach - every failure falling back to the plain
ping. Then the mail_events integration: her PING text rides the outbox,
HOLD sends nothing while the cursor still advances and the DM cooldown/cap
stay unspent, a crashing/fallback triage still yields the plain round-21
ping, and knob-off through the REAL decide leaves zero triage artifacts.

Machine-independent (MINERVA_ANTHROPIC_ENV redirected to a missing file;
injected engine factories; injected _imap_factory). Stdlib only; prints
every check and the final line.
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

from minerva_core.engine import SessionConfig               # noqa: E402
from minerva_core.events import EventType                   # noqa: E402

import drive_tick                                           # noqa: E402
import mail_events                                          # noqa: E402
import mail_triage                                          # noqa: E402

from minerva_core.gateway import tools_connections as tc    # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class Ev:
    def __init__(self, t, payload):
        self.type, self.payload = t, payload


class FakeSession:
    def __init__(self, events, sleep_s=0.0, explode=False):
        self.events = list(events)
        self.sleep_s = sleep_s
        self.explode = explode
        self.prompts: list[str] = []
        self.closed = False

    def send(self, text):
        self.prompts.append(text)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.explode:
            raise OSError("api fell over")
        yield from self.events

    def close(self):
        self.closed = True


class FakeAdapter:
    def __init__(self, sess):
        self.sess = sess

    def create_session(self, cfg):
        return self.sess


def factory_for(sess, calls=None):
    def factory(state_dir):
        if calls is not None:
            calls.append(state_dir)
        cfg = SessionConfig(persona=mail_triage.PERSONA, model="haiku-test",
                            tools=mail_triage.TRIAGE_TOOLS)
        return FakeAdapter(sess), cfg, "byok"
    return factory


class ExplodingFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self, state_dir):
        self.calls += 1
        raise AssertionError("engine must not be touched on this path")


def triage_log(td):
    p = os.path.join(td, mail_triage.LOG_NAME)
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]


def spend_lines(td):
    p = os.path.join(td, "spend.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]


def outbox_lines(td):
    p = os.path.join(td, "outbox.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]


def hdr(frm, to="operator@example.com", subject="hi", unsub="", prec=""):
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

    def login(self, a, p):
        return ("OK", [b""])

    def select(self, folder, readonly=False):
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [" ".join(str(u) for u in self.unseen).encode()])
        if cmd == "FETCH":
            u = int(args[0].decode())
            blob = self.headers.get(u, b"")
            if not blob:
                return ("OK", [None])
            return ("OK", [(b"x (UID y)", blob), b")"])
        return ("NO", [b""])

    def logout(self):
        return ("BYE", [b""])


def gmail_env(td):
    p = os.path.join(td, "gmail.env")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("GMAIL_ADDRESS=operator@example.com\nGMAIL_APP_PASSWORD=abcd\n")
    return p


def mest(td):
    return mail_events._load(mail_events.state_path(td))


HITS = [{"uid": "7", "from": "Ann Pal <ann@friends.org>",
         "subject": "lunch tomorrow?"}]

ENV = {"MINERVA_MAIL_TRIAGE": None, "MINERVA_MAIL_TRIAGE_TIMEOUT_S": None,
       "MINERVA_MAIL_TRIAGE_DAILY_CAP": None,
       "MINERVA_MAIL_TRIAGE_MODEL": None,
       "MINERVA_DAILY_BUDGET_USD": None, "MINERVA_ANTHROPIC_ENV": None,
       "MINERVA_GMAIL_ENV": None, "MINERVA_CALENDAR_ENV": None,
       "GMAIL_ADDRESS": None, "GMAIL_APP_PASSWORD": None,
       "MINERVA_MAIL_EVENTS": None, "MINERVA_MAIL_EVENT_POLL_S": "0",
       "MINERVA_MAIL_EVENT_COOLDOWN_S": "0",
       "MINERVA_MAIL_EVENT_DAILY_CAP": None,
       "MINERVA_MAIL_EVENT_QUIET": "off"}


def main() -> None:                                          # noqa: C901
    saved = {k: os.environ.get(k) for k in ENV}
    saved_factory = tc._imap_factory
    saved_decide = mail_triage.decide
    for k, v in ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        # ---------- unit: protocol parsing ---------------------------------
        check("parse: PING line",
              mail_triage.parse_decision("PING: Ann is waiting")
              == ("ping", "Ann is waiting"))
        check("parse: hold, case-insensitive",
              mail_triage.parse_decision("hold: routine")
              == ("hold", "routine"))
        check("parse: last protocol line wins over a preface",
              mail_triage.parse_decision(
                  "Let me think.\nPING: a\nHOLD: it can wait")
              == ("hold", "it can wait"))
        check("parse: garbage = breach",
              mail_triage.parse_decision("sure, sounds good") == ("", ""))
        check("parse: empty input = breach",
              mail_triage.parse_decision("") == ("", ""))

        # ---------- unit: prompt + allowlist hygiene ------------------------
        two = HITS + [{"uid": "9", "from": "Bob <bob@x.com>", "subject": "b"}]
        p1 = mail_triage.build_prompt(HITS, now=time.mktime(
            (2026, 6, 12, 14, 30, 0, 0, 0, -1)))
        p2 = mail_triage.build_prompt(two)
        check("prompt: uid + sender + subject ride in",
              "id 7" in p1 and "Ann Pal" in p1 and "lunch tomorrow?" in p1)
        check("prompt: DATA-not-instructions stance",
              "never instructions" in p1 or "DATA, never instructions" in p1)
        check("prompt: strict protocol stated",
              "PING:" in p1 and "HOLD:" in p1 and "EXACTLY ONE line" in p1)
        check("prompt: batch rule when several at once",
              "ONE combined" in p2)
        check("allowlist: read/recall/note only, never notify_operator",
              mail_triage.TRIAGE_TOOLS
              == ("email_read", "brain_recall", "episodic_note")
              and "notify_operator" not in mail_triage.TRIAGE_TOOLS)
        check("knob: enabled by default", mail_triage.enabled())
        os.environ["MINERVA_MAIL_TRIAGE"] = "off"
        check("knob: 'off' disables", not mail_triage.enabled())
        os.environ.pop("MINERVA_MAIL_TRIAGE", None)

        # ---------- decide: short-circuits never touch the engine -----------
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_MAIL_TRIAGE"] = "0"
            d, t, info = mail_triage.decide(HITS, td, {}, engine_factory=ExplodingFactory())
            check("off: fallback, reason off", d == "fallback" and info["reason"] == "off")
            check("off: zero side effects (no triage log)",
                  not os.path.exists(os.path.join(td, mail_triage.LOG_NAME)))
            os.environ.pop("MINERVA_MAIL_TRIAGE", None)

        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_ANTHROPIC_ENV"] = os.path.join(td, "nope.env")
            d, t, info = mail_triage.decide(HITS, td, {})
            check("no key: fallback before any thread",
                  d == "fallback" and info["reason"] == "no-key")
            check("no key: logged", triage_log(td)[-1]["reason"] == "no-key")

        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "spend.jsonl"), "w") as f:
                f.write(json.dumps({"ts": time.time(), "usd": 99.0,
                                    "what": "x", "kind": "actual"}) + "\n")
            ef = ExplodingFactory()
            d, t, info = mail_triage.decide(HITS, td, {}, engine_factory=ef)
            check("budget: over = fallback, engine untouched",
                  d == "fallback" and info["reason"] == "budget" and ef.calls == 0)

        with tempfile.TemporaryDirectory() as td:
            st = {"triage_day": time.strftime("%Y-%m-%d"),
                  "triage_count_day": 8}
            ef = ExplodingFactory()
            d, t, info = mail_triage.decide(HITS, td, st, engine_factory=ef)
            check("session cap: fallback, engine untouched, count unchanged",
                  d == "fallback" and info["reason"] == "cap"
                  and ef.calls == 0 and st["triage_count_day"] == 8)

        # ---------- decide: happy PING --------------------------------------
        with tempfile.TemporaryDirectory() as td:
            sess = FakeSession([
                Ev(EventType.TOOL_CALL, {"tool": "email_read", "args": {}}),
                Ev(EventType.COST, {"usd": 0.0031}),
                Ev(EventType.MESSAGE,
                   {"text": "PING: Ann just wrote - lunch tomorrow needs a yes/no today"}),
            ])
            st = {}
            d, t, info = mail_triage.decide(HITS, td, st,
                                            engine_factory=factory_for(sess))
            check("ping: decision + her text",
                  d == "ping" and t.startswith("Ann just wrote"))
            check("ping: prompt carried the mail",
                  sess.prompts and "Ann Pal" in sess.prompts[0])
            check("ping: session counter spent",
                  st["triage_count_day"] == 1
                  and st["triage_day"] == time.strftime("%Y-%m-%d"))
            sp = spend_lines(td)
            check("ping: usd metered as mail triage (byok)",
                  len(sp) == 1 and abs(sp[0]["usd"] - 0.0031) < 1e-9
                  and sp[0]["what"] == "mail triage (byok)")
            lg = triage_log(td)
            check("ping: logged w/ model + usd",
                  lg[-1]["decision"] == "ping"
                  and lg[-1]["model"] == "haiku-test"
                  and abs(lg[-1]["usd"] - 0.0031) < 1e-9)
            check("ping: session closed", sess.closed)

        # ---------- decide: happy HOLD via deltas only ----------------------
        with tempfile.TemporaryDirectory() as td:
            sess = FakeSession([
                Ev(EventType.TEXT_DELTA, {"text": "HOLD: routine"}),
                Ev(EventType.TEXT_DELTA, {"text": " follow-up, nothing due today"}),
            ])
            st = {"triage_day": time.strftime("%Y-%m-%d"),
                  "triage_count_day": 1}
            d, t, info = mail_triage.decide(HITS, td, st,
                                            engine_factory=factory_for(sess))
            check("hold: decision + reason from deltas",
                  d == "hold" and t.startswith("routine follow-up"))
            check("hold: counter spent (thinking is metered)",
                  st["triage_count_day"] == 2)

        # ---------- decide: failure modes -> fallback ------------------------
        with tempfile.TemporaryDirectory() as td:
            sess = FakeSession([Ev(EventType.MESSAGE,
                                   {"text": "I think I will ping him."})])
            d, t, info = mail_triage.decide(HITS, td, {},
                                            engine_factory=factory_for(sess))
            check("protocol breach: fallback",
                  d == "fallback" and info["reason"] == "protocol")

        with tempfile.TemporaryDirectory() as td:
            sess = FakeSession([Ev(EventType.MESSAGE, {"text": "PING:"})])
            d, t, info = mail_triage.decide(HITS, td, {},
                                            engine_factory=factory_for(sess))
            check("empty PING body: breach -> fallback",
                  d == "fallback" and info["reason"] == "protocol")

        with tempfile.TemporaryDirectory() as td:
            sess = FakeSession([], explode=True)
            d, t, info = mail_triage.decide(HITS, td, {},
                                            engine_factory=factory_for(sess))
            check("session crash: fallback error",
                  d == "fallback" and info["reason"] == "error")

        with tempfile.TemporaryDirectory() as td:
            def rt_factory(state_dir):
                raise RuntimeError("no BYOK key - test")
            d, t, info = mail_triage.decide(HITS, td, {},
                                            engine_factory=rt_factory)
            check("factory RuntimeError: no-key fallback",
                  d == "fallback" and info["reason"] == "no-key")

        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_MAIL_TRIAGE_TIMEOUT_S"] = "0.15"
            sess = FakeSession([Ev(EventType.MESSAGE, {"text": "PING: late"})],
                               sleep_s=1.0)
            st = {}
            t0 = time.time()
            d, t, info = mail_triage.decide(HITS, td, st,
                                            engine_factory=factory_for(sess))
            took = time.time() - t0
            check("timeout: fallback well before the session would finish",
                  d == "fallback" and info["reason"] == "timeout" and took < 0.8)
            check("timeout: counter still spent (a session was opened)",
                  st.get("triage_count_day") == 1)
            os.environ.pop("MINERVA_MAIL_TRIAGE_TIMEOUT_S", None)
            time.sleep(1.0)          # let the daemon worker drain before td dies

        # ---------- integration: mail_events x triage ------------------------
        def run_check(td, fake, **kw):
            tc._imap_factory = lambda host: fake
            return mail_events.check(td, **kw)

        # ping: her words ride the outbox; caps spent
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            run_check(td, FakeImap([3], {}), quiet=True)       # bootstrap
            seen = {}

            def fake_ping(hits, state_dir, st, now=None, quiet=True):
                seen["hits"] = hits
                seen["st_is_dict"] = isinstance(st, dict)
                return "ping", "Ann is waiting on lunch plans - answer today", {}
            mail_triage.decide = fake_ping
            n = run_check(td, FakeImap([3, 7],
                                       {7: hdr("Ann Pal <ann@friends.org>",
                                               subject="lunch tomorrow?")}),
                          quiet=True)
            mail_triage.decide = saved_decide
            ob = outbox_lines(td)
            check("integration ping: ONE DM, her text, [mail] prefix",
                  n == 1 and len(ob) == 1 and ob[0]["message"]
                  == "[mail] Ann is waiting on lunch plans - answer today")
            check("integration ping: uid + sender reached triage",
                  seen["hits"][0]["uid"] == "7"
                  and "Ann Pal" in seen["hits"][0]["from"]
                  and seen["st_is_dict"])
            check("integration ping: DM caps spent",
                  mest(td)["dm_count_day"] == 1
                  and mest(td)["last_dm_ts"] > 0)

        # hold: nothing sent, cursor advances, DM caps UNSPENT, lock released
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            run_check(td, FakeImap([3], {}), quiet=True)
            mail_triage.decide = (lambda hits, state_dir, st, now=None,
                                  quiet=True: ("hold", "routine", {}))
            n = run_check(td, FakeImap([3, 7],
                                       {7: hdr("Ann <ann@x.com>",
                                               subject="later is fine")}),
                          quiet=True)
            mail_triage.decide = saved_decide
            check("integration hold: no DM", n == 0 and outbox_lines(td) == [])
            s = mest(td)
            check("integration hold: cursor advanced (no backlog rule)",
                  s["last_uid"] == 7)
            check("integration hold: cooldown/cap unspent",
                  int(s.get("dm_count_day", 0)) == 0
                  and float(s.get("last_dm_ts", 0.0)) == 0.0)
            lock = os.path.join(td, "drive-tick.lock")
            got = drive_tick.acquire_lock(lock)
            drive_tick.release_lock(lock)
            check("integration hold: engine lock released", got)

        # triage crash: the plain round-21 ping still goes out
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            run_check(td, FakeImap([3], {}), quiet=True)

            def boom(hits, state_dir, st, now=None, quiet=True):
                raise OSError("triage fell over")
            mail_triage.decide = boom
            n = run_check(td, FakeImap([3, 7],
                                       {7: hdr("Ann Pal <ann@friends.org>",
                                               subject="lunch tomorrow?")}),
                          quiet=True)
            mail_triage.decide = saved_decide
            ob = outbox_lines(td)
            check("integration crash: plain ping survives",
                  n == 1 and len(ob) == 1
                  and ob[0]["message"].startswith("[mail] New email from ")
                  and "Ann Pal" in ob[0]["message"]
                  and "lunch tomorrow?" in ob[0]["message"])

        # fallback decision: plain ping rides
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            run_check(td, FakeImap([3], {}), quiet=True)
            mail_triage.decide = (lambda hits, state_dir, st, now=None,
                                  quiet=True: ("fallback", "", {}))
            n = run_check(td, FakeImap([3, 7],
                                       {7: hdr("Bo <bo@x.com>", subject="s")}),
                          quiet=True)
            mail_triage.decide = saved_decide
            check("integration fallback: plain ping rides",
                  n == 1 and outbox_lines(td)[-1]["message"]
                  .startswith("[mail] New email from "))

        # knob off through the REAL decide: plain ping + zero triage artifacts
        with tempfile.TemporaryDirectory() as td:
            os.environ["MINERVA_GMAIL_ENV"] = gmail_env(td)
            os.environ["MINERVA_MAIL_TRIAGE"] = "0"
            run_check(td, FakeImap([3], {}), quiet=True)
            n = run_check(td, FakeImap([3, 7],
                                       {7: hdr("Cy <cy@x.com>", subject="q")}),
                          quiet=True)
            os.environ.pop("MINERVA_MAIL_TRIAGE", None)
            check("integration knob-off: plain ping, real decide",
                  n == 1 and outbox_lines(td)[-1]["message"]
                  .startswith("[mail] New email from "))
            check("integration knob-off: zero triage artifacts",
                  not os.path.exists(os.path.join(td, mail_triage.LOG_NAME)))
    finally:
        mail_triage.decide = saved_decide
        tc._imap_factory = saved_factory
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
