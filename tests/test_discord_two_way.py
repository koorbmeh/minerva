"""Discord two-way (host loop v0.5) — reply grammar, dispatch, inbound poll.
Local mock of the Discord REST endpoints (no real Discord in the suite).
Run: python tests/test_discord_two_way.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import host_loop  # noqa: E402

PASSED = 0
SEEN = {"sent": [], "inbound": [], "auth": set(), "fail_get": False}

P1 = "19eb01aaa-aa11cc33"        # waiting pitch ids (hexy, like new_id())
P2 = "19eb02bbb-bb22dd44"
REF1 = "19eb09fff-ee55ff66"      # pending approval ref


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def jl(path: str, *recs: dict) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


class MockDiscord(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        SEEN["auth"].add(self.headers.get("Authorization", ""))
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/users/@me/channels":
            self._json(200, {"id": "chan-77"})
        elif self.path == "/channels/chan-77/messages":
            SEEN["sent"].append(body.get("content", ""))
            self._json(200, {"id": f"out-{len(SEEN['sent'])}"})
        else:
            self._json(404, {})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/channels/chan-77/messages":
            if SEEN["fail_get"]:
                SEEN["fail_get"] = False
                self._json(500, {})
                return
            q = urllib.parse.parse_qs(parsed.query)
            after = int(q.get("after", ["0"])[0] or 0)
            fresh = [m for m in SEEN["inbound"] if int(m["id"]) > after]
            fresh.sort(key=lambda m: int(m["id"]), reverse=True)  # Discord: newest first
            self._json(200, fresh[:50])
        elif parsed.path == "/cdn/cat.png":
            data = b"PNG-BYTES-FOR-TEST"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {})


def seed_state(td: str) -> tuple[str, str, str, str, str]:
    """ledger with two waiting pitches + one pending approval ask."""
    ledger = os.path.join(td, "ledger.jsonl")
    verdicts = os.path.join(td, "verdicts.jsonl")
    approvals = os.path.join(td, "approvals.jsonl")
    inbox = os.path.join(td, "discord-inbox.jsonl")
    files_dir = os.path.join(td, "discord-files")
    jl(ledger,
       {"id": P1, "type": "pitch", "ts": 1.0,
        "payload": {"domain": "income", "title": "Sell the spare GPU",
                    "detail": "It idles."}},
       {"id": P2, "type": "pitch", "ts": 2.0,
        "payload": {"domain": "health", "title": "Morning sun walk",
                    "detail": "10 minutes."}})
    jl(approvals,
       {"ref": REF1, "ts": 3.0, "tool": "test_spend", "args": {"item": "x"},
        "reason": "action class: spend", "status": "pending"})
    return ledger, verdicts, approvals, inbox, files_dir


def grammar_checks() -> None:
    pr = host_loop.parse_reply
    pool_p, pool_r = [P1, P2], [REF1]

    r = pr("approve", pool_p, pool_r)
    check("bare approve", r["kind"] == "approval" and r["approve"] is True
          and r["ref"] is None and r["note"] == "" and r["error"] is None)
    r = pr("Deny. ee55ff66 too risky", pool_p, pool_r)
    check("deny + ref piece + note", r["kind"] == "approval"
          and r["approve"] is False and r["ref"] == REF1
          and r["note"] == "too risky" and r["error"] is None)
    r = pr("never aa11cc33 not my thing", pool_p, pool_r)
    check("verdict + id piece + note", r["kind"] == "verdict"
          and r["word"] == "never" and r["pitch"] == P1
          and r["note"] == "not my thing")
    r = pr("never dead simple idea", pool_p, pool_r)
    check("short hexy word w/o match stays note text",
          r["kind"] == "verdict" and r["pitch"] is None
          and r["note"] == "dead simple idea" and r["error"] is None)
    r = pr("not now 19eb01 cooling please", pool_p, pool_r)
    check("'not now' two-word alias resolves",
          r["kind"] == "verdict" and r["word"] == "not-now"
          and r["pitch"] == P1 and r["note"] == "cooling please")
    r = pr("yes", pool_p, pool_r)
    check("'yes' maps to adopt", r["kind"] == "verdict" and r["word"] == "yes")
    r = pr("park bb22dd44 until winter", pool_p, pool_r)
    check("park resolves", r["pitch"] == P2 and r["note"] == "until winter")
    r = pr("later 19eb0", pool_p, pool_r)
    check("ambiguous piece errors with both ids",
          r["error"] is not None and "matches 2" in r["error"]
          and P1 in r["error"] and P2 in r["error"])
    r = pr("adopt abcdef99", pool_p, pool_r)
    check("long hexy no-match errors (typo never becomes a note)",
          r["error"] is not None and "abcdef99" in r["error"])
    check("free text is a message",
          pr("hey Minerva, how was the tick?", pool_p, pool_r)
          == {"kind": "message"})
    check("empty is a message", pr("", pool_p, pool_r) == {"kind": "message"})
    check("'approved' is NOT a command (grammar stays small)",
          pr("approved", pool_p, pool_r) == {"kind": "message"})


def dispatch_checks() -> None:
    with tempfile.TemporaryDirectory() as td:
        ledger, verdicts, approvals, inbox, files_dir = seed_state(td)
        kw = dict(ledger_path=ledger, verdicts_path=verdicts,
                  approvals_path=approvals, inbox_path=inbox,
                  files_dir=files_dir)
        ledger_bytes = open(ledger, "rb").read()

        # bare verdict with TWO waiting -> hint, nothing recorded
        reply, kind = host_loop.process_reply("m1", "never", **kw)
        check("bare verdict w/ two waiting -> hint", kind == "hint"
              and P1 in reply and P2 in reply)
        check("hint records nothing", not os.path.exists(verdicts))

        # verdict by id piece -> decision line w/ provenance + CLI string
        reply, kind = host_loop.process_reply("m2", "never aa11cc33 junk idea",
                                              **kw)
        check("verdict recorded", kind == "verdict")
        lines = host_loop.read_jsonl(verdicts)
        check("verdict line shape + provenance",
              len(lines) == 1 and lines[0]["pitch"] == P1
              and lines[0]["verdict"] == "decline_forever"
              and lines[0]["note"] == "junk idea"
              and lines[0]["via"] == "discord" and lines[0]["msg_id"] == "m2"
              and lines[0]["by"] == "operator" and lines[0]["status"] == "decided")
        check("confirmation uses the CLI plain string",
              host_loop.VERDICT_PLAIN["decline_forever"] in reply
              and "folds it into her ledger" in reply and P1 in reply)

        # idempotence: same msg_id re-polled -> no double record, no DM
        reply, kind = host_loop.process_reply("m2", "never aa11cc33 junk idea",
                                              **kw)
        check("duplicate msg_id skipped", kind == "duplicate" and reply is None
              and len(host_loop.read_jsonl(verdicts)) == 1)

        # changed mind = NEW msg -> fresh line appended (latest wins downstream)
        reply, kind = host_loop.process_reply("m3", "park aa11cc33 maybe later",
                                              **kw)
        check("changed mind appends fresh line", kind == "verdict"
              and len(host_loop.read_jsonl(verdicts)) == 2)

        # bare verdict now: P1 decided, only P2 waiting -> single-pending rule
        reply, kind = host_loop.process_reply("m4", "adopt sounds good", **kw)
        lines = host_loop.read_jsonl(verdicts)
        check("bare verdict applies to the single waiting pitch",
              kind == "verdict" and lines[-1]["pitch"] == P2
              and lines[-1]["verdict"] == "adopt"
              and lines[-1]["note"] == "sounds good")

        # approvals: bare approve w/ exactly one pending
        reply, kind = host_loop.process_reply("m5", "approve go ahead", **kw)
        check("bare approve hits the single pending ref", kind == "approval")
        arecs = [r for r in host_loop.read_jsonl(approvals)
                 if r.get("status") == "approved"]
        check("approval line shape + provenance",
              len(arecs) == 1 and arecs[0]["ref"] == REF1
              and arecs[0]["note"] == "go ahead"
              and arecs[0]["via"] == "discord" and arecs[0]["msg_id"] == "m5")
        check("approve confirmation is the CLI string",
              host_loop.CONFIRM_APPROVED in reply and REF1 in reply)

        # nothing pending anymore -> gentle hints, never silence
        reply, kind = host_loop.process_reply("m6", "deny", **kw)
        check("approval hint when none pending", kind == "hint"
              and "nothing is waiting" in reply)
        reply, kind = host_loop.process_reply("m7", "never", **kw)
        check("verdict hint when none waiting", kind == "hint"
              and "no pitches" in reply)

        # cross-lane hint: verdict word while an approval pends
        jl(approvals, {"ref": "19eb0aaaa-77778888", "ts": 9.0, "tool": "t",
                       "args": {}, "reason": "r", "status": "pending"})
        reply, kind = host_loop.process_reply("m8", "yes", **kw)
        check("verdict word w/ only approvals pending points at approve/deny",
              kind == "hint" and "approve" in reply)

        # free text -> queued as context, acked
        reply, kind = host_loop.process_reply("m9", "the bike swap is Saturday",
                                              **kw)
        q = host_loop.read_jsonl(inbox)
        check("free text queued", kind == "message" and len(q) == 1
              and q[0]["text"] == "the bike swap is Saturday"
              and q[0]["via"] == "discord" and q[0]["msg_id"] == "m9")
        check("free text acked", "next run" in reply)
        reply, kind = host_loop.process_reply("m9", "the bike swap is Saturday",
                                              **kw)
        check("free text duplicate skipped", kind == "duplicate"
              and len(host_loop.read_jsonl(inbox)) == 1)

        # attachments: saved w/ sha256; oversize skipped; never executed
        data = b"PNG-BYTES-FOR-TEST"
        att_ok = {"filename": "ca t&.png", "size": len(data),
                  "url": "http://x/cdn/cat.png"}
        att_big = {"filename": "huge.bin",
                   "size": host_loop.FILE_CAP_BYTES + 1, "url": "http://x/n"}
        reply, kind = host_loop.process_reply(
            "m10", "photos for the selling pile", [att_ok, att_big],
            fetch=lambda url: data, **kw)
        q = host_loop.read_jsonl(inbox)
        files = q[-1].get("files", [])
        saved = [x for x in files if x.get("path")]
        skipped = [x for x in files if x.get("skipped")]
        check("attachment saved + capped sibling skipped",
              kind == "message" and len(saved) == 1 and len(skipped) == 1)
        check("filename sanitized", os.path.basename(saved[0]["path"])
              == "m10-ca_t_.png")
        check("sha256 recorded", saved[0]["sha256"]
              == hashlib.sha256(data).hexdigest())
        check("bytes on disk match",
              open(saved[0]["path"], "rb").read() == data)
        check("file ack mentions the skip",
              "1 file(s)" in reply and "skipped" in reply)

        # empty message (no text, no files) -> nothing
        reply, kind = host_loop.process_reply("m11", "", **kw)
        check("empty message ignored", kind == "empty" and reply is None)

        # the ledger never changed (single-appender holds on this lane)
        check("ledger bytes untouched by dispatch",
              open(ledger, "rb").read() == ledger_bytes)


def poll_checks() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockDiscord)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as td:
        ledger, verdicts, approvals, inbox, files_dir = seed_state(td)
        cursor = os.path.join(td, "cursor.json")
        saved_globals = (host_loop.DISCORD_API, host_loop.LEDGER,
                         host_loop.VERDICTS, host_loop.APPROVALS,
                         host_loop.CURSOR, host_loop.DISCORD_INBOX,
                         host_loop.DISCORD_FILES, host_loop.OUTBOX,
                         host_loop.DISCORD_ENV)
        host_loop.DISCORD_API = f"http://127.0.0.1:{port}"
        host_loop.LEDGER = ledger
        host_loop.VERDICTS = verdicts
        host_loop.APPROVALS = approvals
        host_loop.CURSOR = cursor
        host_loop.DISCORD_INBOX = inbox
        host_loop.DISCORD_FILES = files_dir
        host_loop.OUTBOX = os.path.join(td, "outbox.jsonl")
        host_loop.DISCORD_ENV = os.path.join(td, "no-such.env")  # NEVER the
        # real config: a test must not be able to poll live Discord
        try:
            cfg = {"DISCORD_BOT_TOKEN": "tok-1", "DISCORD_USER_ID": "42"}

            # history present BEFORE two-way ever ran: must not become decisions
            SEEN["inbound"] = [
                {"id": "100", "author": {"id": "42"}, "content": "never"},
                {"id": "101", "author": {"id": "99"},
                 "content": "[PITCH] old bot message"},
            ]
            n = host_loop.poll_discord_inbound(cfg, quiet=True)
            cur = host_loop.load_cursor(cursor)
            check("first poll bootstraps without processing",
                  n == 0 and cur["discord_inbound_last_id"] == "101"
                  and not os.path.exists(verdicts))

            # new replies after bootstrap: The operator decides, bot echo skipped
            SEEN["inbound"] += [
                {"id": "102", "author": {"id": "42"},
                 "content": "never aa11cc33 already sorted the shelf"},
                {"id": "103", "author": {"id": "99"},
                 "content": "recorded: NEVER - ..."},     # bot's own echo
                {"id": "104", "author": {"id": "42"},
                 "content": "remember the sale is Saturday"},
            ]
            n = host_loop.poll_discord_inbound(cfg, quiet=True)
            check("second poll processes the operator's two", n == 2)
            lines = host_loop.read_jsonl(verdicts)
            check("poll recorded the verdict w/ snowflake provenance",
                  len(lines) == 1 and lines[0]["pitch"] == P1
                  and lines[0]["msg_id"] == "102")
            check("free text queued via poll",
                  host_loop.read_jsonl(inbox)[0]["msg_id"] == "104")
            check("confirmations DMed",
                  any("recorded:" in s for s in SEEN["sent"])
                  and any("next run" in s for s in SEEN["sent"]))
            check("cursor at newest snowflake",
                  host_loop.load_cursor(cursor)["discord_inbound_last_id"]
                  == "104")

            # idle poll: nothing new
            check("idle poll processes nothing",
                  host_loop.poll_discord_inbound(cfg, quiet=True) == 0)

            # cursor rollback (crash replay): msg_id dedupe holds the line
            sent_before = len(SEEN["sent"])
            cur = host_loop.load_cursor(cursor)
            cur["discord_inbound_last_id"] = "101"
            host_loop.save_cursor(cur, cursor)
            n = host_loop.poll_discord_inbound(cfg, quiet=True)
            check("replay re-processes nothing into files",
                  len(host_loop.read_jsonl(verdicts)) == 1
                  and len(host_loop.read_jsonl(inbox)) == 1)
            check("replay sends no duplicate confirmations",
                  len(SEEN["sent"]) == sent_before)
            check("replay still advances the cursor",
                  host_loop.load_cursor(cursor)["discord_inbound_last_id"]
                  == "104")

            # poll failure: quiet no-op, cursor stands
            SEEN["fail_get"] = True
            check("failed GET is a safe no-op",
                  host_loop.poll_discord_inbound(cfg, quiet=True) == 0
                  and host_loop.load_cursor(cursor)["discord_inbound_last_id"]
                  == "104")

            # unconfigured -> 0 (env patched to a missing file above)
            check("unconfigured poll is a no-op",
                  host_loop.poll_discord_inbound(None, quiet=True) == 0)
        finally:
            (host_loop.DISCORD_API, host_loop.LEDGER, host_loop.VERDICTS,
             host_loop.APPROVALS, host_loop.CURSOR, host_loop.DISCORD_INBOX,
             host_loop.DISCORD_FILES, host_loop.OUTBOX,
             host_loop.DISCORD_ENV) = saved_globals
    server.shutdown()


def main() -> None:
    print("grammar:")
    grammar_checks()
    print("dispatch:")
    dispatch_checks()
    print("inbound poll:")
    poll_checks()
    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
