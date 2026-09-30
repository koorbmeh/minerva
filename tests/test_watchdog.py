"""Watchdog (round 18): heartbeat pulse + staleness alerts with an
independent Discord path and cooldown. Stdlib only:
  python3 tests/test_watchdog.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class _MockDiscord(BaseHTTPRequestHandler):
    messages: list = []

    def do_POST(self):                                    # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path.endswith("/users/@me/channels"):
            data = json.dumps({"id": "chan-1"}).encode()
        else:
            _MockDiscord.messages.append(body.get("content", ""))
            data = json.dumps({"id": "msg-1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_heartbeat(tmp: str) -> None:
    import host_loop
    p = os.path.join(tmp, "hb.json")
    host_loop.write_heartbeat(p)
    rec = json.load(open(p, encoding="utf-8"))
    check("pulse: host_loop.write_heartbeat writes ts+pid",
          abs(time.time() - rec["ts"]) < 5 and rec["pid"] == os.getpid())


def test_checks(tmp: str) -> None:
    import watchdog
    srv = HTTPServer(("127.0.0.1", 0), _MockDiscord)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    watchdog.DISCORD_API = f"http://127.0.0.1:{srv.server_address[1]}"
    saved = {k: os.environ.get(k) for k in
             ("DISCORD_BOT_TOKEN", "DISCORD_USER_ID",
              "MINERVA_WATCHDOG_STALE_S", "MINERVA_WATCHDOG_COOLDOWN_S")}
    os.environ.update({"DISCORD_BOT_TOKEN": "tkn", "DISCORD_USER_ID": "u1",
                       "MINERVA_WATCHDOG_STALE_S": "180",
                       "MINERVA_WATCHDOG_COOLDOWN_S": "3600"})
    try:
        sd = os.path.join(tmp, "s1")
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "heartbeat.json"), "w") as f:
            json.dump({"ts": time.time(), "pid": 1}, f)
        rep = watchdog.check(sd, quiet=True)
        check("watchdog: fresh pulse -> ok, no alert",
              rep["ok"] and not rep["alerted"] and not _MockDiscord.messages)

        with open(os.path.join(sd, "heartbeat.json"), "w") as f:
            json.dump({"ts": time.time() - 900, "pid": 1}, f)
        rep = watchdog.check(sd, quiet=True)
        check("watchdog: stale pulse -> alert DM'd independently",
              not rep["ok"] and rep["alerted"]
              and _MockDiscord.messages
              and _MockDiscord.messages[-1].startswith("[WATCHDOG]")
              and "15 minutes ago" in _MockDiscord.messages[-1])
        n_before = len(_MockDiscord.messages)
        rep = watchdog.check(sd, quiet=True)
        check("watchdog: cooldown suppresses repeat alerts",
              rep.get("suppressed") == "cooldown"
              and len(_MockDiscord.messages) == n_before)
        with open(os.path.join(sd, "watchdog-state.json"), "w") as f:
            json.dump({"last_alert_ts": time.time() - 7200}, f)
        watchdog.check(sd, quiet=True)
        check("watchdog: alerts again after the cooldown",
              len(_MockDiscord.messages) == n_before + 1)
        log = [json.loads(x) for x in
               open(os.path.join(sd, "watchdog-log.jsonl"), encoding="utf-8")]
        check("watchdog: every stale check logged", len(log) == 3)

        sd2 = os.path.join(tmp, "s2")
        os.makedirs(sd2, exist_ok=True)
        rep = watchdog.check(sd2, quiet=True)
        check("watchdog: missing pulse alerts as not-running",
              not rep["ok"] and rep["age_s"] is None
              and "NO pulse" in _MockDiscord.messages[-1])

        with open(os.path.join(sd2, "heartbeat.json"), "w") as f:
            f.write("{garbage")
        with open(os.path.join(sd2, "watchdog-state.json"), "w") as f:
            json.dump({"last_alert_ts": 0}, f)
        rep = watchdog.check(sd2, quiet=True)
        check("watchdog: unreadable pulse = bad pulse (fail safe)",
              not rep["ok"] and rep["age_s"] == float("inf"))

        os.environ.pop("DISCORD_BOT_TOKEN", None)
        os.environ.pop("DISCORD_USER_ID", None)
        old_env_path = watchdog.DISCORD_ENV
        watchdog.DISCORD_ENV = os.path.join(tmp, "no-discord.env")
        try:
            sd3 = os.path.join(tmp, "s3")
            os.makedirs(sd3, exist_ok=True)
            rep = watchdog.check(sd3, quiet=True)
            check("watchdog: no discord config -> still reports + logs",
                  not rep["ok"] and not rep["alerted"]
                  and os.path.exists(os.path.join(sd3, "watchdog-log.jsonl")))
        finally:
            watchdog.DISCORD_ENV = old_env_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_heartbeat(tmp)
        test_checks(tmp)
    print(f"\nall {PASSED} checks passed")
