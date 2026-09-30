"""Hermes adapter test against a stdlib mock of the documented API surface.

Validates OUR side of the contract (session create, run submit, SSE parse,
event translation, auth header, cancel, error path). The real contract gets
verified hands-on at the host install.  Run: python3 tests/test_adapter_hermes.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.adapters.hermes import HermesAdapter
from minerva_core.engine import RoutingHints, SessionConfig
from minerva_core.events import EventType

PASSED = 0
SEEN: dict = {"auth": set(), "run_body": None, "stopped": False}

SSE_EVENTS = [
    ("assistant.delta", {"text": "Hel"}),
    ("assistant.delta", {"text": "lo there"}),
    ("tool.started", {"tool": "pitch_idea", "args": {"domain": "income"}, "call_id": "t1"}),
    ("tool.completed", {"call_id": "t1", "result": "pitch logged (abc)"}),
    ("usage", {"input_tokens": 100, "output_tokens": 42}),
    ("message.complete", {"text": "Hello there"}),
    ("run.completed", {"status": "ok"}),
]


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class MockHermes(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
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
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/sessions":
            self._json(200, {"id": "sess-1"})
        elif self.path == "/v1/runs":
            SEEN["run_body"] = body
            self._json(200, {"id": "run-1"})
        elif self.path == "/v1/runs/run-1/stop":
            SEEN["stopped"] = True
            self._json(200, {})
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        SEEN["auth"].add(self.headers.get("Authorization", ""))
        if self.path == "/v1/runs/run-1/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for name, data in SSE_EVENTS:
                self.wfile.write(f"event: {name}\n".encode())
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        else:
            self._json(404, {"error": "not found"})


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHermes)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = HermesAdapter(base_url=f"http://127.0.0.1:{port}", api_key="test-key-123")
        check("capabilities sane", "tools_mcp" in adapter.capabilities())

        cfg = SessionConfig(persona="You are Minerva.", model="qwen-test",
                            routing=RoutingHints(), tools=("ping",))
        sess = adapter.create_session(cfg)
        check("session created", sess.id == "sess-1")

        events = list(sess.send("hi"))
        types = [e.type for e in events]
        check("event sequence translated", types == [
            EventType.TEXT_DELTA, EventType.TEXT_DELTA, EventType.TOOL_CALL,
            EventType.TOOL_RESULT, EventType.COST, EventType.MESSAGE, EventType.DONE])
        check("deltas assemble", "".join(e.payload["text"] for e in events[:2]) == "Hello there")
        check("tool call payload mapped", events[2].payload["tool"] == "pitch_idea"
              and events[2].payload["args"] == {"domain": "income"})
        check("tool result mapped", events[3].payload["ok"]
              and "pitch logged" in events[3].payload["result"])
        check("cost tick mapped", events[4].payload["input_tokens"] == 100
              and events[4].payload["output_tokens"] == 42)
        check("final message intact", events[5].payload["text"] == "Hello there")
        check("session id on every event", all(e.session_id == "sess-1" for e in events))

        check("bearer auth sent on every request",
              SEEN["auth"] == {"Bearer test-key-123"})
        check("run body carries session + model",
              SEEN["run_body"]["session_id"] == "sess-1"
              and SEEN["run_body"]["model"] == "qwen-test"
              and SEEN["run_body"]["input"] == "hi")

        sess.cancel()
        check("cancel hits /stop", SEEN["stopped"])

        # error path: dead server -> ERROR + DONE, never an exception
        dead = HermesAdapter(base_url="http://127.0.0.1:9", api_key="x", timeout=2)
        try:
            dead.create_session(cfg)
            check("dead server raises on create (caller handles)", False)
        except Exception:
            check("dead server raises on create (caller handles)", True)
        sess2 = adapter.create_session(cfg)
        sess2.adapter = dead  # simulate engine dying mid-conversation
        evs = list(sess2.send("hello?"))
        check("mid-conversation death -> ERROR then DONE",
              [e.type for e in evs] == [EventType.ERROR, EventType.DONE]
              and evs[0].payload["fatal"])
    finally:
        server.shutdown()
    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
