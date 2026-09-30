"""Discord file lane (round 19): send_file_to_operator tool, multipart upload,
file-aware outbox delivery, inbound-to-workspace. Stdlib only:
  python3 tests/test_discord_files.py
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

from minerva_core.gateway.tools_builtin import build_default_registry
from minerva_core.ledger import Ledger, ProposalIndex

PASSED = 0
PNG = b"\x89PNG-attachment-bytes"


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class _MockDiscord(BaseHTTPRequestHandler):
    raw: list = []
    json_msgs: list = []

    def do_POST(self):                                    # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        if self.path.endswith("/users/@me/channels"):
            data = json.dumps({"id": "chan-9"}).encode()
        else:
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                _MockDiscord.raw.append((ctype, body))
            else:
                _MockDiscord.json_msgs.append(json.loads(body))
            data = json.dumps({"id": "m1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def make_registry(tmp: str):
    state = os.path.join(tmp, "state")
    ws = os.path.join(state, "workspace")
    os.makedirs(os.path.join(ws, "generated"), exist_ok=True)
    with open(os.path.join(ws, "generated", "owl.png"), "wb") as f:
        f.write(PNG)
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))
    reg = build_default_registry(ledger, ProposalIndex(ledger),
                                 outbox_path=os.path.join(state, "outbox.jsonl"),
                                 workspace_dir=ws)
    return reg, state


def test_tool(tmp: str) -> None:
    reg, state = make_registry(tmp)
    out = reg.get("send_file_to_operator").handler(
        {"path": "generated/owl.png", "note": "your owl, as requested"})
    check("tool: queues a workspace file for delivery",
          out.startswith("queued for Discord delivery"))
    rows = [json.loads(x) for x in
            open(os.path.join(state, "outbox.jsonl"), encoding="utf-8")]
    check("tool: outbox line carries file + note",
          rows[-1]["file"] == "generated/owl.png"
          and rows[-1]["message"] == "your owl, as requested")
    check("tool: containment escape refused",
          reg.get("send_file_to_operator").handler(
              {"path": "../ledger.jsonl"}).startswith("refused:"))
    check("tool: missing file refused plainly",
          "no such workspace file" in
          reg.get("send_file_to_operator").handler({"path": "generated/nope.png"}))
    os.environ["MINERVA_DM_FILE_CAP"] = "5"
    try:
        check("tool: size cap refusal (env knob)",
              "over the 5-byte" in reg.get("send_file_to_operator").handler(
                  {"path": "generated/owl.png"}))
    finally:
        os.environ.pop("MINERVA_DM_FILE_CAP", None)
    check("tool: honest class (send_file)",
          reg.get("send_file_to_operator").classes == {"send_file"})


def test_delivery(tmp: str) -> None:
    import host_loop
    srv = HTTPServer(("127.0.0.1", 0), _MockDiscord)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state = os.path.join(tmp, "dstate")
    ws = os.path.join(state, "workspace")
    os.makedirs(os.path.join(ws, "generated"), exist_ok=True)
    with open(os.path.join(ws, "generated", "vid.mp4"), "wb") as f:
        f.write(PNG * 3)
    outbox = os.path.join(state, "outbox.jsonl")
    with open(outbox, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "message": "plain text"}) + "\n")
        f.write(json.dumps({"ts": time.time(), "file": "generated/vid.mp4",
                            "message": "here is your video"}) + "\n")
        f.write(json.dumps({"ts": time.time(), "file": "generated/gone.png",
                            "message": "oops"}) + "\n")
    saved = {k: getattr(host_loop, k) for k in
             ("DISCORD_API", "OUTBOX", "CURSOR")}
    host_loop.DISCORD_API = f"http://127.0.0.1:{srv.server_address[1]}"
    host_loop.OUTBOX = outbox
    host_loop.CURSOR = os.path.join(state, "cursor.json")
    cfg = {"DISCORD_BOT_TOKEN": "tkn", "DISCORD_USER_ID": "u1"}
    try:
        n = host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("delivery: all three outbox lines delivered", n == 3)
        check("delivery: plain text rode the json lane",
              any(m.get("content") == "plain text"
                  for m in _MockDiscord.json_msgs))
        check("delivery: missing file -> honest text fallback",
              any("file missing: generated/gone.png" in m.get("content", "")
                  for m in _MockDiscord.json_msgs))
        check("delivery: file line rode MULTIPART", len(_MockDiscord.raw) == 1)
        ctype, body = _MockDiscord.raw[0]
        boundary = ctype.split("boundary=", 1)[1]
        check("delivery: attachment bytes + filename + note all present",
              PNG * 3 in body
              and b'filename="vid.mp4"' in body
              and b"here is your video" in body
              and boundary.encode("ascii") in body)
        n2 = host_loop.deliver_outbox_discord(cfg, quiet=True)
        check("delivery: cursor advanced (no double-send)", n2 == 0)
    finally:
        for k, v in saved.items():
            setattr(host_loop, k, v)
        srv.shutdown()
        srv.server_close()


def test_inbound_location() -> None:
    import host_loop
    check("inbound: attachments now land INSIDE her workspace (incoming/)",
          host_loop.DISCORD_FILES.replace("\\", "/").endswith(
              "state/workspace/incoming"))
    chat_src = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                                 "minerva_chat.py"), encoding="utf-8").read()
    check("chat: allowlist + persona carry the file lane",
          '"send_file_to_operator"' in chat_src
          and "incoming/" in chat_src)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_tool(tmp)
        test_delivery(tmp)
        test_inbound_location()
    print(f"\nall {PASSED} checks passed")
