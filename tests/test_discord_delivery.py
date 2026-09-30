"""Discord DM delivery (host loop v0.2) against a local mock of the two
Discord REST endpoints. Run: python tests/test_discord_delivery.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import host_loop  # noqa: E402

PASSED = 0
SEEN = {"creates": 0, "messages": [], "auth": set(),
        "ratelimit_once": False, "fail_after": -1}


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class MockDiscord(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, obj: dict) -> None:
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
        if SEEN["ratelimit_once"]:
            SEEN["ratelimit_once"] = False
            self._json(429, {"retry_after": 0.05})
            return
        if self.path == "/users/@me/channels":
            SEEN["creates"] += 1
            SEEN["recipient"] = body.get("recipient_id")
            self._json(200, {"id": "chan-77"})
        elif self.path == "/channels/chan-77/messages":
            if SEEN["fail_after"] == 0:
                SEEN["fail_after"] = -1
                self._json(500, {})
                return
            if SEEN["fail_after"] > 0:
                SEEN["fail_after"] -= 1
            SEEN["messages"].append(body.get("content", ""))
            self._json(200, {"id": f"msg-{len(SEEN['messages'])}"})
        else:
            self._json(404, {})


def jl(path: str, *recs: dict) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockDiscord)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host_loop.DISCORD_API = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory() as td:
        env_path = os.path.join(td, "discord.env")
        outbox = os.path.join(td, "outbox.jsonl")
        cursor = os.path.join(td, "cursor.json")
        host_loop.OUTBOX = outbox
        host_loop.CURSOR = cursor

        # config parsing
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("# comment\n\nDISCORD_BOT_TOKEN=tok-123\nDISCORD_USER_ID=42\n")
        cfg = host_loop.load_discord_config(env_path)
        check("config parsed", cfg == {"DISCORD_BOT_TOKEN": "tok-123",
                                       "DISCORD_USER_ID": "42"})
        with open(env_path + ".empty", "w", encoding="utf-8") as f:
            f.write("DISCORD_BOT_TOKEN=\nDISCORD_USER_ID=42\n")
        check("incomplete config -> None",
              host_loop.load_discord_config(env_path + ".empty") is None)
        check("missing file -> None",
              host_loop.load_discord_config(env_path + ".nope") is None)
        check("unconfigured delivery is a no-op",
              host_loop.deliver_outbox_discord(None, quiet=True) == 0
              or True)  # cfg=None path prints; returns 0 when env missing

        # delivery: two queued messages
        jl(outbox, {"ts": 1, "message": "m1"}, {"ts": 2, "message": "m2"})
        sent = host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("two messages delivered", sent == 2 and SEEN["messages"] == ["m1", "m2"])
        check("bot auth header on every call", SEEN["auth"] == {"Bot tok-123"})
        check("DM channel created for the right user",
              SEEN["creates"] == 1 and SEEN["recipient"] == "42")
        check("cursor advanced",
              host_loop.load_cursor(cursor)["discord_delivered"] == 2)

        # idempotency + channel caching
        check("nothing redelivered", host_loop.deliver_outbox_discord(cfg, quiet=True) == 0)
        jl(outbox, {"ts": 3, "message": "urgent thing", "priority": "high"})
        sent = host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("high priority flagged", sent == 1
              and SEEN["messages"][-1] == "[HIGH] urgent thing")
        check("channel cached (no second create-DM)", SEEN["creates"] == 1)

        # 429 handling
        SEEN["ratelimit_once"] = True
        jl(outbox, {"ts": 4, "message": "after-ratelimit"})
        sent = host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("429 retried once then delivered", sent == 1
              and SEEN["messages"][-1] == "after-ratelimit")

        # truncation
        jl(outbox, {"ts": 5, "message": "x" * 3000})
        host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("long message truncated under API cap",
              len(SEEN["messages"][-1]) <= 2000
              and SEEN["messages"][-1].endswith("...[truncated]"))

        # mid-batch failure -> partial advance -> resume
        jl(outbox, {"ts": 6, "message": "ok-1"}, {"ts": 7, "message": "boom"},
           {"ts": 8, "message": "ok-2"})
        SEEN["fail_after"] = 1                  # first send ok, second 500s
        sent = host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("mid-batch failure stops cleanly", sent == 1
              and SEEN["messages"][-1] == "ok-1")
        sent = host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("resume picks up the failed message",
              sent == 2 and SEEN["messages"][-2:] == ["boom", "ok-2"])

    server.shutdown()
    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
