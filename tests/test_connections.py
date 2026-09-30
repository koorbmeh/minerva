"""Connection agency (round 14): dormant-connector framework, gmail IMAP
(read-only) + calendar ICS tools, request_connection through the anti-nag
ledger, registry gating, real-gateway round-trip. Stdlib only:
  python3 tests/test_connections.py
"""
from __future__ import annotations

import datetime as dt
import email.mime.multipart
import email.mime.text
import imaplib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.gateway import tools_connections as tc
from minerva_core.gateway.tools_builtin import build_default_registry
from minerva_core.ledger import Ledger, ProposalIndex

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SECRET_PW = "abcd efgh ijkl mnop"

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def env_redirect(tmp: str, gmail: bool = False, cal_url: str = "") -> dict:
    """Point config reads at tmp files; '' values = dormant."""
    saved = {}
    g = os.path.join(tmp, "gmail.env")
    c = os.path.join(tmp, "calendar.env")
    if gmail:
        with open(g, "w", encoding="utf-8") as f:
            f.write(f"GMAIL_ADDRESS=operator@test\nGMAIL_APP_PASSWORD={SECRET_PW}\n")
    if cal_url:
        with open(c, "w", encoding="utf-8") as f:
            f.write(f"CALENDAR_ICS_URL={cal_url}\n")
    for k, v in (("MINERVA_GMAIL_ENV", g if gmail else os.path.join(tmp, "no-g.env")),
                 ("MINERVA_CALENDAR_ENV", c if cal_url else os.path.join(tmp, "no-c.env"))):
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    return saved


def env_restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
class FakeIMAP:
    """The imaplib surface the tools use; records the readonly flag."""
    fail_login = False
    messages: dict = {}            # uid(str) -> raw bytes
    unread: list = []              # uids as bytes

    def __init__(self, host: str) -> None:
        self.host = host
        self.selected_readonly = None
        FakeIMAP.last = self

    def login(self, addr: str, pw: str):
        if FakeIMAP.fail_login:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"ok"]

    def select(self, folder: str, readonly: bool = False):
        self.selected_readonly = readonly
        if folder == "Nope":
            return "NO", [b"no such folder"]
        return "OK", [b"3"]

    def uid(self, cmd: str, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(FakeIMAP.unread)]
        if cmd == "FETCH":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            raw = FakeIMAP.messages.get(uid)
            if raw is None:
                return "OK", [None]
            return "OK", [(f"{uid} (BODY[] {{{len(raw)}}}".encode(), raw)]
        return "NO", [b""]

    def logout(self):
        return "BYE", [b""]


def msg_bytes(frm: str, subject: str, body: str, html: bool = False) -> bytes:
    sub = email.mime.text.MIMEText(body, "html" if html else "plain", "utf-8")
    sub["From"] = frm
    sub["Subject"] = subject
    sub["Date"] = "Thu, 12 Jun 2026 08:00:00 -0500"
    return sub.as_bytes()


def test_status_and_requests(tmp: str) -> None:
    saved = env_redirect(tmp)                      # both dormant
    try:
        out = tc.connections_status({})
        check("status: both connectors DORMANT with unlock + enable steps",
              out.count("DORMANT") == 2 and "App passwords" in out
              and "Secret address" in out and "request_connection" in out)
        state = os.path.join(tmp, "s1")
        os.makedirs(state, exist_ok=True)
        ledger = Ledger(os.path.join(state, "ledger.jsonl"))
        proposals = ProposalIndex(ledger)
        check("request: garbage name refused with the ready-made list",
              "ready-made names" in tc.request_connection(
                  proposals, {"name": "!!!", "reason": "x"}))
        check("request: new name without a reason asks for one",
              "concrete reason" in tc.request_connection(
                  proposals, {"name": "fitbit", "reason": ""}))
        out = tc.request_connection(
            proposals, {"name": "fitbit",
                        "reason": "heart-rate trends would sharpen the "
                                  "workout suggestions"})
        check("request: NON-catalog want -> build proposal via the ledger",
              out.startswith("new-connection proposal logged"))
        fit = [e for e in ledger.entries() if e["type"] == "pitch"
               and "fitbit" in e["payload"]["title"]]
        check("request: build proposal flagged as new in title + detail",
              fit and "(new)" in fit[0]["payload"]["title"]
              and "BUILT" in fit[0]["payload"]["detail"])
        check("request: free-form re-ask also anti-nagged",
              tc.request_connection(proposals,
                                    {"name": "fitbit", "reason": "again"})
              .startswith("REJECTED by anti-nag"))

        from minerva_core.ledger import similar, slugify
        check("request: different connector titles never similarity-collide",
              not similar(slugify("gmail connection"),
                          slugify("calendar connection"))
              and not similar(slugify("fitbit connection (new)"),
                              slugify("homeassistant connection (new)"))
              and not similar(slugify("gmail connection"),
                              slugify("fitbit connection (new)")))

        state2 = os.path.join(tmp, "s1b")        # fresh ledger: the 24h/domain
        os.makedirs(state2, exist_ok=True)       # pitch interval is by design
        proposals = ProposalIndex(Ledger(os.path.join(state2, "ledger.jsonl")))
        out = tc.request_connection(
            proposals, {"name": "gmail",
                        "reason": "I want to flag bills before they are due."})
        check("request: dormant gmail -> logged through the pitch ledger",
              out.startswith("connection request logged ("))
        entries = Ledger(os.path.join(state2, "ledger.jsonl")).entries()
        pitch = next(e for e in entries if e["type"] == "pitch")
        check("request: pitch carries domain, unlock and the enable steps",
              pitch["payload"]["domain"] == "connection"
              and "App passwords" in pitch["payload"]["detail"]
              and "bills" in pitch["payload"]["detail"])
        out2 = tc.request_connection(proposals, {"name": "gmail",
                                                 "reason": "asking again"})
        check("request: re-ask -> REJECTED by anti-nag (final answer)",
              out2.startswith("REJECTED by anti-nag"))
    finally:
        env_restore(saved)

    saved = env_redirect(tmp, gmail=True)
    try:
        out = tc.connections_status({})
        check("status: configured gmail shows ACTIVE with its tools",
              "gmail: ACTIVE" in out and "email_unread" in out)
        check("status: secrets never appear", SECRET_PW not in out)
        state = os.path.join(tmp, "s2")
        os.makedirs(state, exist_ok=True)
        proposals = ProposalIndex(Ledger(os.path.join(state, "ledger.jsonl")))
        check("request: active connector -> already connected",
              "already connected" in tc.request_connection(
                  proposals, {"name": "gmail", "reason": "x"}))
    finally:
        env_restore(saved)


def test_registry_gating(tmp: str) -> None:
    state = os.path.join(tmp, "reg")
    os.makedirs(state, exist_ok=True)
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))

    saved = env_redirect(tmp + "/r1")
    os.makedirs(tmp + "/r1", exist_ok=True)
    saved2 = env_redirect(tmp + "/r1")
    try:
        reg = build_default_registry(ledger, ProposalIndex(ledger),
                                     outbox_path=os.path.join(state, "o.jsonl"))
        names = set(reg.names())
        check("registry: metadata tools ALWAYS on",
              {"connections_status", "request_connection"} <= names)
        check("registry: content tools absent without configs",
              not {"email_unread", "email_read", "calendar_upcoming"} & names)
    finally:
        env_restore(saved2)
        env_restore(saved)

    os.makedirs(tmp + "/r2", exist_ok=True)
    saved = env_redirect(tmp + "/r2", gmail=True, cal_url="https://example.com/x.ics")
    try:
        reg = build_default_registry(ledger, ProposalIndex(ledger),
                                     outbox_path=os.path.join(state, "o.jsonl"))
        names = set(reg.names())
        check("registry: configured connectors register their tools",
              {"email_unread", "email_read", "calendar_upcoming"} <= names)
        check("registry: content tools carry the private_read class",
              reg.get("email_read").classes == {"private_read"}
              and reg.get("calendar_upcoming").classes == {"private_read"})
    finally:
        env_restore(saved)

    chat_src = open(os.path.join(ROOT_DIR, "scripts", "minerva_chat.py"),
                    encoding="utf-8").read().split("BUSY_REPLY", 1)[0]
    check("chat: allowlist carries the connection tools",
          all(f'"{t}"' in chat_src for t in
              ("connections_status", "request_connection", "email_unread",
               "email_read", "calendar_upcoming")))
    tick_src = open(os.path.join(ROOT_DIR, "scripts", "drive_tick.py"),
                    encoding="utf-8").read()
    check("tick: prompt offers request_connection as the pitch",
          "request_connection" in tick_src and "counts as your one pitch"
          in tick_src)


def test_email(tmp: str) -> None:
    saved = env_redirect(tmp, gmail=True)
    old_factory = tc._imap_factory
    tc._imap_factory = FakeIMAP
    FakeIMAP.fail_login = False
    FakeIMAP.unread = [b"1", b"2", b"3"]
    FakeIMAP.messages = {
        "1": msg_bytes("Robin <a@x.com>", "Bike swap signs", "plain one"),
        "2": msg_bytes("Bank <b@x.com>", "Statement ready", "plain two"),
        "3": msg_bytes("Bob <bob@x.com>", "=?utf-8?b?Q2Fmw6k=?= plans",
                       "<p>html <b>only</b> body</p>", html=True),
    }
    try:
        out = tc.email_unread({})
        check("email: unread lists newest first with ids",
              out.startswith("3 unread in INBOX")
              and out.index("id 3") < out.index("id 1")
              and "Statement ready" in out)
        check("email: encoded subject decoded (no raw =?utf-8?= form)",
              "=?utf-8?" not in out and "Caf" in out and "never marks" in out)
        check("email: IMAP session opened READ-ONLY (EXAMINE)",
              FakeIMAP.last.selected_readonly is True)
        out = tc.email_unread({"max_results": 2})
        check("email: max_results cap honored",
              "id 3" in out and "id 2" in out and "id 1" not in out)
        FakeIMAP.unread = []
        check("email: zero unread says so plainly",
              tc.email_unread({}) == "no unread mail in INBOX")
        FakeIMAP.unread = [b"1"]
        check("email: bad folder is a plain error",
              "no such mail folder" in tc.email_unread({"folder": "Nope"}))

        out = tc.email_read({"id": "1"})
        check("email: read returns body with the DATA tag",
              "plain one" in out and tc.DATA_NOTE in out
              and "Bike swap signs" in out)
        check("email: read session also READ-ONLY",
              FakeIMAP.last.selected_readonly is True)
        out = tc.email_read({"id": "3"})
        check("email: html-only body converted to text",
              "html" in out and "<p>" not in out and "<b>" not in out)
        check("email: missing id is a plain error",
              "no message with id 99" in tc.email_read({"id": "99"}))
        check("email: no id given",
              tc.email_read({}).startswith("no message id"))
        FakeIMAP.fail_login = True
        check("email: login failure names the app password plainly",
              "app password" in tc.email_unread({}))
    finally:
        tc._imap_factory = old_factory
        env_restore(saved)


# ---------------------------------------------------------------------------
class _ICSServer(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):                                     # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar")
        self.send_header("Content-Length", str(len(_ICSServer.body)))
        self.end_headers()
        self.wfile.write(_ICSServer.body)

    def log_message(self, *a):
        pass


def ics_fixture(now: dt.datetime) -> str:
    def stamp(d: dt.datetime) -> str:
        return d.strftime("%Y%m%dT%H%M%S")
    tomorrow = (now + dt.timedelta(days=1)).replace(hour=14, minute=0, second=0)
    allday = now + dt.timedelta(days=2)
    weekly_start = (now - dt.timedelta(days=7)).replace(hour=9, minute=30,
                                                        second=0)
    # an instance of the weekly event ~tomorrow-ish to EXDATE out:
    daily_start = (now - dt.timedelta(days=1)).replace(hour=7, minute=0,
                                                       second=0)
    monthly_start = (now + dt.timedelta(days=3)).replace(hour=18, minute=0,
                                                         second=0)
    past = now - dt.timedelta(days=3)
    far = now + dt.timedelta(days=20)
    ev = []
    ev.append(f"""BEGIN:VEVENT
DTSTART:{stamp(tomorrow)}
DTEND:{stamp(tomorrow + dt.timedelta(hours=1))}
SUMMARY:Dentist
LOCATION:Springfield
END:VEVENT""")
    ev.append(f"""BEGIN:VEVENT
DTSTART;VALUE=DATE:{allday.strftime("%Y%m%d")}
SUMMARY:Bike swap prep
END:VEVENT""")
    ev.append(f"""BEGIN:VEVENT
DTSTART:{stamp(weekly_start)}
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;COUNT=60
SUMMARY:Stretch block
END:VEVENT""")
    ev.append(f"""BEGIN:VEVENT
DTSTART:{stamp(daily_start)}
RRULE:FREQ=DAILY;INTERVAL=2;COUNT=40
SUMMARY:Walk
END:VEVENT""")
    ev.append(f"""BEGIN:VEVENT
DTSTART:{stamp(monthly_start)}
RRULE:FREQ=MONTHLY;COUNT=12
SUMMARY:Rent check
END:VEVENT""")
    ev.append(f"""BEGIN:VEVENT
DTSTART:{stamp(now + dt.timedelta(days=2, hours=3))}
RRULE:FREQ=HOURLY;COUNT=5
SUMMARY:Weird hourly thing
END:VEVENT""")
    ev.append(f"""BEGIN:VEVENT
DTSTART:{stamp(past)}
SUMMARY:Old thing
END:VEVENT""")
    ev.append(f"""BEGIN:VEVENT
DTSTART:{stamp(far)}
SUMMARY:Far thing
END:VEVENT""")
    return ("BEGIN:VCALENDAR\n"
            + "\n".join(ev).replace("\n", "\r\n") + "\r\nEND:VCALENDAR\r\n")


def test_calendar(tmp: str) -> None:
    saved = env_redirect(tmp)
    try:
        check("calendar: unconfigured says so",
              "not configured" in tc.calendar_upcoming({}))
    finally:
        env_restore(saved)

    os.makedirs(tmp + "/c1", exist_ok=True)
    saved = env_redirect(tmp + "/c1", cal_url="http://evil.example.com/x.ics")
    try:
        check("calendar: non-loopback http refused (secret URL wants https)",
              tc.calendar_upcoming({}).startswith("refused:"))
    finally:
        env_restore(saved)

    try:                                             # a host may have no tz database
        _chi = tc.ZoneInfo("America/Chicago") if tc.ZoneInfo is not None else None
    except Exception:
        _chi = None
    if _chi is not None:                             # offset-aware TZID check
        got, ad = tc._parse_ics_dt("20260615T140000",
                                   {"TZID": "America/Chicago"})
        want = dt.datetime(2026, 6, 15, 14, 0, 0).replace(
            tzinfo=_chi).astimezone(None)             .replace(tzinfo=None)
        check("calendar: TZID converted to host-local time",
              got == want and not ad)

    now = dt.datetime.now()
    _ICSServer.body = ics_fixture(now).encode()
    srv = HTTPServer(("127.0.0.1", 0), _ICSServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/cal.ics"
    os.makedirs(tmp + "/c2", exist_ok=True)
    saved = env_redirect(tmp + "/c2", cal_url=url)
    try:
        out = tc.calendar_upcoming({"days": 7})
        check("calendar: timed event with duration + location",
              "Dentist" in out and "14:00-15:00" in out and "Springfield" in out)
        check("calendar: all-day event labeled",
              "(all day) | Bike swap prep" in out)
        check("calendar: weekly RRULE expanded to multiple instances",
              out.count("Stretch block") >= 5)
        check("calendar: daily INTERVAL=2 expanded",
              out.count("Walk") >= 2)
        check("calendar: monthly instance inside the window present",
              "Rent check" in out)
        check("calendar: unsupported FREQ listed unexpanded with a note",
              "Weird hourly thing (recurring" in out)
        check("calendar: past + beyond-window events excluded",
              "Old thing" not in out and "Far thing" not in out)
        body_lines = [l for l in out.splitlines()
                      if "|" in l and "recurring" not in l]
        firsts = [l.split("|")[0].strip() for l in body_lines]
        check("calendar: sorted ascending",
              firsts == sorted(firsts, key=lambda s: s.split(" ", 1)[1]))
        out2 = tc.calendar_upcoming({"days": 7, "max_events": 3})
        check("calendar: max_events cap honored",
              len([l for l in out2.splitlines() if " | " in l]) <= 3 + 5)
        _ICSServer.body = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        check("calendar: empty calendar says so",
              "no events in the next" in tc.calendar_upcoming({}))
    finally:
        env_restore(saved)
        srv.shutdown()
        srv.server_close()


def test_real_gateway(tmp: str) -> None:
    from minerva_core.adapters.anthropic_byok import GatewayClient
    state = os.path.join(tmp, "gw")
    os.makedirs(state, exist_ok=True)
    gw = GatewayClient(
        [sys.executable, os.path.join(ROOT_DIR, "scripts", "run_gateway.py"),
         "--state-dir", state],
        extra_env={"MINERVA_TOOLS": "connections_status,request_connection",
                   "MINERVA_GMAIL_ENV": os.path.join(tmp, "no-g.env"),
                   "MINERVA_CALENDAR_ENV": os.path.join(tmp, "no-c.env")})
    try:
        gw.initialize()
        names = sorted(t["name"] for t in gw.list_tools())
        check("gateway: metadata tools exposed under the allowlist",
              names == ["connections_status", "request_connection"])
        text, err = gw.call("connections_status", {})
        check("gateway: status round-trip (both dormant)",
              not err and text.count("DORMANT") == 2)
        text, err = gw.call("request_connection",
                            {"name": "calendar",
                             "reason": "to know your day before the tick"})
        check("gateway: connection ask rides the pitch ledger",
              not err and text.startswith("connection request logged"))
    finally:
        gw.close()
    ok, n, _ = Ledger(os.path.join(state, "ledger.jsonl")).verify_chain()
    check("gateway: ledgered + chain intact", ok and n >= 2)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_status_and_requests(tmp)
        test_registry_gating(tmp)
        test_email(tmp)
        test_calendar(tmp)
        test_real_gateway(tmp)
    print(f"\nall {PASSED} checks passed")
