"""Mail-event lane v0 (round 21) - the first EVENT-DRIVEN trigger.

Until now Minerva was clock-driven (two ticks + the morning brief). This
module gives host_loop's watch daemon a cheap awareness pulse: every
MINERVA_MAIL_EVENT_POLL_S (default 300s) it peeks at UNSEEN mail over the
SAME strictly read-only IMAP path the gmail connector uses (EXAMINE +
header-only BODY.PEEK - nothing is ever marked read), and DMs the operator ONLY
when something looks like a real person wrote to him directly.

ANTI-NAG IS THE DESIGN CENTER (the operator's standing scar):
  * cheap local heuristics, no model call, no spend: bulk senders
    (List-Unsubscribe / Precedence: bulk|list / no-reply-style local parts)
    never ping; mail not addressed TO him never pings;
  * cooldown between pings (MINERVA_MAIL_EVENT_COOLDOWN_S, default 2h);
  * daily cap (MINERVA_MAIL_EVENT_DAILY_CAP, default 5);
  * quiet hours (MINERVA_MAIL_EVENT_QUIET, default 22-07, local) - mail
    seen during quiet/cooldown/cap is deliberately NOT queued for later
    (the morning brief is the catch-up surface; a backlog dump is nagging);
  * several personal mails in one poll = ONE combined DM;
  * first run ever BOOTSTRAPS the cursor without alerting (old unread can
    never become surprise pings - the round-10 inbound rule).

Rails: kill switch (state/STOP) suppresses polling entirely; the outbox
append happens under the SHARED ENGINE LOCK (warden pattern; single-appender
hygiene) - if the lock is busy the cursor does NOT advance, so the event
re-detects next cycle instead of vanishing. gmail not configured or
MINERVA_MAIL_EVENTS=0 = silent free skip. DM carries ONLY sender + subject
(metadata) - bodies never ride a ping; the operator asks in chat when he wants the
content.

Round 22: when a ping is allowed, a bounded governed triage session
(mail_triage.py) may rewrite it in Minerva's own words or HOLD it for the
morning brief; ANY triage failure falls back to the plain ping built here.
Bodies still never ride a ping raw - she reads read-only and writes her own
one-liner.

State: state/mail-events.json (uid cursor, poll/dm timestamps, daily count,
triage session counters). Stdlib only. ASCII out (cp1252 rule).
"""
from __future__ import annotations

import email
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from minerva_core.killswitch import is_stopped                    # noqa: E402
from minerva_core.gateway import tools_connections as tc          # noqa: E402

import drive_tick                                                 # noqa: E402  (shared lock)

STATE = os.path.join(ROOT, "state")
STATE_NAME = "mail-events.json"
ENGINE_LOCK_NAME = "drive-tick.lock"
LOCK_TRIES = 2
LOCK_WAIT_S = 0.5
FETCH_CAP = 10                  # newest N new UIDs examined per poll
DM_CAP = 1500

# local parts that are machines, not people (tunable; conservative = quieter)
BULK_LOCAL_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|mailer(-daemon)?|"
    r"newsletters?|news|updates?|alerts?|offers?|deals?|sales|marketing|"
    r"promo(tions)?|info|support|help|team|hello|contact|billing|receipts?|"
    r"bounce.*|postmaster|noreply.*|.*-noreply|.*-no-reply)$", re.I)

_announced = False              # one-time console status line per process


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def enabled() -> bool:
    return (os.environ.get("MINERVA_MAIL_EVENTS", "1").strip().lower()
            not in ("0", "off", "no", "false"))


def state_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or STATE, STATE_NAME)


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save(path: str, st: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f)


def in_quiet_hours(now: float, spec: str | None = None) -> bool:
    """spec "22-07" = suppress from 22:00 to 06:59 local. "" / "off" = never."""
    spec = (spec if spec is not None
            else os.environ.get("MINERVA_MAIL_EVENT_QUIET", "22-07")).strip()
    if not spec or spec.lower() == "off":
        return False
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", spec)
    if not m:
        return False
    start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
    hour = time.localtime(now).tm_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def is_personal(from_addr: str, to_cc: str, operator_addr: str,
                list_unsub: str, precedence: str) -> bool:
    """Cheap, free, local: does this look like a person writing to the operator?"""
    if list_unsub.strip():
        return False
    if precedence.strip().lower() in ("bulk", "list", "junk"):
        return False
    addr = from_addr.strip().lower()
    m = re.search(r"<([^>]+)>", addr)
    if m:
        addr = m.group(1)
    local = addr.split("@", 1)[0]
    if BULK_LOCAL_RE.match(local):
        return False
    if operator_addr and operator_addr.lower() not in to_cc.lower():
        return False                       # not addressed to him directly
    return True


def _peek_headers(client, uid: bytes) -> dict:
    """Header-only read (BODY.PEEK - never marks read)."""
    typ, data = client.uid(
        "FETCH", uid,
        "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)])")
    if typ != "OK" or not data or data[0] is None:
        return {}
    blob = b""
    for part in data:
        if isinstance(part, tuple) and len(part) > 1:
            blob = part[1]
            break
    if not blob:
        return {}
    msg = email.message_from_bytes(blob)
    return {"uid": uid.decode() if isinstance(uid, bytes) else str(uid),
            "from": tc._decode_header(msg.get("From")),
            "to_cc": tc._decode_header(msg.get("To"))
                     + " " + tc._decode_header(msg.get("Cc")),
            "subject": tc._decode_header(msg.get("Subject")),
            "list_unsub": str(msg.get("List-Unsubscribe") or ""),
            "precedence": str(msg.get("Precedence") or "")}


def _ascii(s: str) -> str:
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


def check(state_dir: str | None = None, now: float | None = None,
          quiet: bool = True) -> int:
    """One awareness pulse. Returns the number of DMs queued (0 or 1).
    Never raises (the caller is the delivery daemon)."""
    global _announced
    state_dir = state_dir or STATE
    now = now or time.time()

    if not enabled():
        if not _announced and not quiet:
            print("mail alerts: OFF (MINERVA_MAIL_EVENTS=0)")
            _announced = True
        return 0
    try:
        configured = bool(tc.CONNECTORS["gmail"]["configured"]())
    except Exception:                                             # noqa: BLE001
        configured = False
    if not configured:
        if not _announced and not quiet:
            print("mail alerts: waiting (gmail not connected)")
            _announced = True
        return 0
    if not _announced and not quiet:
        try:
            import mail_triage
            _trg = "on" if mail_triage.enabled() else "off"
        except Exception:                                         # noqa: BLE001
            _trg = "off"
        print("mail alerts: ON (person-looking mail pings you; "
              f"cooldown {int(_f('MINERVA_MAIL_EVENT_COOLDOWN_S', 7200.0))}s, "
              f"cap {int(_f('MINERVA_MAIL_EVENT_DAILY_CAP', 5.0))}/day, "
              f"quiet {os.environ.get('MINERVA_MAIL_EVENT_QUIET', '22-07') or 'off'}; "
              f"triage {_trg})")
        _announced = True
    if is_stopped(state_dir):
        return 0

    sp = state_path(state_dir)
    st = _load(sp)
    if now - float(st.get("last_poll_ts", 0.0)) < _f("MINERVA_MAIL_EVENT_POLL_S", 300.0):
        return 0
    st["last_poll_ts"] = now

    try:
        client = tc._imap_open("INBOX")
    except Exception as exc:                                      # noqa: BLE001
        if not quiet:
            print(f"mail alerts: poll failed ({_ascii(repr(exc))[:120]})",
                  file=sys.stderr)
        _save(sp, st)                       # poll throttle still advances
        return 0
    try:
        typ, data = client.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            _save(sp, st)
            return 0
        uids = [int(u) for u in (data[0].split() if data and data[0] else [])]
        last_uid = int(st.get("last_uid", -1))
        if last_uid < 0:                    # FIRST run: bootstrap, never alert
            st["last_uid"] = max(uids) if uids else 0
            _save(sp, st)
            return 0
        new = sorted(u for u in uids if u > last_uid)
        if not new:
            _save(sp, st)
            return 0
        examined = new[-FETCH_CAP:]
        operator_addr = tc._env_get(tc._gmail_env(), "GMAIL_ADDRESS")
        hits = []
        for u in examined:
            h = _peek_headers(client, str(u).encode())
            if h and is_personal(h["from"], h["to_cc"], operator_addr,
                                 h["list_unsub"], h["precedence"]):
                hits.append(h)
    finally:
        try:
            client.logout()
        except Exception:                                         # noqa: BLE001
            pass

    day = time.strftime("%Y-%m-%d", time.localtime(now))
    if st.get("dm_day") != day:
        st["dm_day"], st["dm_count_day"] = day, 0

    suppressed = (in_quiet_hours(now)
                  or now - float(st.get("last_dm_ts", 0.0))
                  < _f("MINERVA_MAIL_EVENT_COOLDOWN_S", 7200.0)
                  or int(st.get("dm_count_day", 0))
                  >= int(_f("MINERVA_MAIL_EVENT_DAILY_CAP", 5.0)))

    if not hits or suppressed:
        # deliberate: suppressed/bulk mail never queues for later - the
        # morning brief is the catch-up surface (anti-nag, see module doc)
        st["last_uid"] = max(new)
        _save(sp, st)
        return 0

    lines = []
    for h in hits[:5]:
        sender = h["from"][:60] or "(unknown sender)"
        subj = h["subject"][:90] or "(no subject)"
        lines.append(f'{sender} - "{subj}"')
    more = len(hits) - len(lines)
    msg = ("[mail] " + ("New email that looks like it needs you:\n"
                        if len(lines) > 1 else "New email from ")
           + "\n".join(lines)
           + (f"\n(+{more} more)" if more > 0 else ""))
    msg = _ascii(msg)[:DM_CAP]

    lock = os.path.join(state_dir, ENGINE_LOCK_NAME)
    got = False
    for _ in range(LOCK_TRIES):
        if drive_tick.acquire_lock(lock):
            got = True
            break
        time.sleep(LOCK_WAIT_S)
    if not got:
        # don't advance last_uid: re-detect next cycle instead of losing it
        _save(sp, st)
        return 0
    try:
        # round 22: the trigger THINKS before it pings - a bounded governed
        # session (mail_triage.py) may rewrite the ping in her own words or
        # HOLD it for the morning brief. Any triage failure = the plain ping
        # composed above (thinking can only improve a ping, never lose one).
        decision, t_text = "fallback", ""
        try:
            import mail_triage
            decision, t_text, _tinfo = mail_triage.decide(
                hits, state_dir, st, now=now, quiet=quiet)
        except Exception:                                         # noqa: BLE001
            decision = "fallback"
        if decision == "hold":
            # her call: it waits for the morning brief. Cursor advances;
            # cooldown/daily cap deliberately UNSPENT (they limit DMs sent,
            # not thinking) - held mail stays unread for the brief.
            st["last_uid"] = max(new)
            _save(sp, st)
            if not quiet:
                print("mail alerts: held for the brief "
                      f"({_ascii(t_text)[:80]})")
            return 0
        if decision == "ping" and t_text.strip():
            msg = _ascii("[mail] " + t_text.strip())[:DM_CAP]
        try:
            with open(os.path.join(state_dir, "outbox.jsonl"), "a",
                      encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"ts": now, "priority": "normal",
                                    "message": msg}, ensure_ascii=True) + "\n")
        except OSError:
            _save(sp, st)
            return 0
    finally:
        drive_tick.release_lock(lock)

    st["last_uid"] = max(new)
    st["last_dm_ts"] = now
    st["dm_count_day"] = int(st.get("dm_count_day", 0)) + 1
    _save(sp, st)
    if not quiet:
        print(f"mail alerts: pinged the operator ({len(hits)} personal-looking)")
    return 1


if __name__ == "__main__":
    # manual one-shot: force a poll regardless of throttle (testing aid)
    os.environ.setdefault("MINERVA_MAIL_EVENT_POLL_S", "0")
    n = check(quiet=False)
    print(f"mail-events: {n} DM(s) queued (deliver via host_loop)")
