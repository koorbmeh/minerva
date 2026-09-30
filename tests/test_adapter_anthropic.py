"""BYOK Anthropic adapter — REAL gateway subprocess + mock Messages API.

The gateway side is the genuine article (run_gateway.py on a temp state dir):
tool listing, governance escalation, and ledger appends are exercised for
real. Only the Anthropic HTTP endpoint is mocked (scripted local http.server),
so the adapter's tool loop runs against true seams on both sides.
Run: python tests/test_adapter_anthropic.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.adapters import anthropic_byok as ab  # noqa: E402
from minerva_core.engine import SessionConfig            # noqa: E402
from minerva_core.events import EventType                # noqa: E402
from minerva_core.ledger import Ledger                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = [sys.executable, os.path.join(ROOT, "scripts", "run_gateway.py")]

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


# ---------------- scripted Anthropic mock ------------------------------------
class MockAnthropic(BaseHTTPRequestHandler):
    responses: deque = deque()
    requests: list = []

    def do_POST(self):                                    # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        MockAnthropic.requests.append(
            {"body": body, "headers": dict(self.headers)})
        if MockAnthropic.responses:
            status, payload, headers = MockAnthropic.responses.popleft()
        else:
            status, payload, headers = 500, {"error": {"message": "unscripted"}}, {}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):                         # silence
        pass


def script(*items):
    MockAnthropic.responses.clear()
    MockAnthropic.requests.clear()
    for status, payload, *rest in items:
        MockAnthropic.responses.append((status, payload, rest[0] if rest else {}))


def msg(stop, content, in_t=100, out_t=20, model="claude-test-1"):
    return {"id": "msg_x", "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop,
            "usage": {"input_tokens": in_t, "output_tokens": out_t,
                      "cache_read_input_tokens": 7}}


def text(t):
    return {"type": "text", "text": t}


def tool_use(call_id, name, args):
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def events_of(sess, prompt):
    evs = list(sess.send(prompt))
    return evs, [e.type for e in evs]


def main() -> None:
    # registry stays machine-independent: never read real gmail/calendar configs
    os.environ["MINERVA_GMAIL_ENV"] = os.path.join(tempfile.gettempdir(), "no-gmail.env")
    os.environ["MINERVA_CALENDAR_ENV"] = os.path.join(tempfile.gettempdir(), "no-cal.env")
    server = HTTPServer(("127.0.0.1", 0), MockAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api_url = f"http://127.0.0.1:{server.server_address[1]}/v1/messages"

    # ---- key loading ---------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        envf = os.path.join(td, "anthropic.env")
        with open(envf, "w", encoding="utf-8") as f:
            f.write("# comment\nANTHROPIC_API_KEY = sk-file-key\n")
        os.environ.pop("ANTHROPIC_API_KEY", None)
        check("key from env file", ab.load_api_key(envf) == "sk-file-key")
        os.environ["ANTHROPIC_API_KEY"] = "sk-env-key"
        check("env var beats file", ab.load_api_key(envf) == "sk-env-key")
        os.environ.pop("ANTHROPIC_API_KEY", None)
        check("no key -> None", ab.load_api_key(os.path.join(td, "nope")) is None)

    wire: list = []

    def wlog(direction, frame):
        wire.append((direction, json.dumps(frame)))

    # ---- session against the REAL gateway -------------------------------------
    with tempfile.TemporaryDirectory() as td:
        adapter = ab.AnthropicByokAdapter(
            api_key="sk-test-secret", gateway_argv=GATEWAY + ["--state-dir", td],
            state_dir=td, api_url=api_url, wire_log=wlog)
        cfg = SessionConfig(persona="You are Minerva.", model="claude-test-1")
        sess = adapter.create_session(cfg)
        try:
            names = {t["name"] for t in sess.tools}
            check("real gateway tools listed",   # round 26: + job_*
                  names == {"ping", "episodic_note", "pitch_idea", "notify_operator",
                            "web_fetch", "files_list", "files_read",
                            "files_write", "generate_image", "generate_video",
                            "connections_status", "request_connection",
                            "spend_status", "send_file_to_operator",
                            "agenda_list", "agenda_add", "agenda_update",
                            "job_submit", "job_list", "job_result",
                            "decisions_pending", "record_verdict",
                            "resolve_approval"})
            check("schema converted",
                  all("input_schema" in t for t in sess.tools))

            # text-only turn
            script((200, msg("end_turn", [text("Hi there")])))
            evs, types = events_of(sess, "hello")
            check("cost emitted",
                  any(e.type is EventType.COST and e.payload["input_tokens"] == 100
                      for e in evs))
            check("message assembled", any(e.type is EventType.MESSAGE and
                                           e.payload["text"] == "Hi there"
                                           for e in evs))
            check("done end_turn", evs[-1].type is EventType.DONE and
                  evs[-1].payload["stop_reason"] == "end_turn")
            req = MockAnthropic.requests[0]["body"]
            check("persona rides as system", req.get("system") == "You are Minerva.")
            check("model from cfg", req.get("model") == "claude-test-1")
            check("tools offered to model",
                  {t["name"] for t in req["tools"]} == names)

            # tool turn: model pings, REAL gateway answers, result fed back
            script((200, msg("tool_use",
                             [text("calling..."),
                              tool_use("toolu_1", "ping", {"echo": "hi"})])),
                   (200, msg("end_turn", [text("TICK-DONE pinged")])))
            evs, types = events_of(sess, "ping please")
            check("tool_call event",
                  any(e.type is EventType.TOOL_CALL and e.payload["tool"] == "ping"
                      for e in evs))
            tr = next(e for e in evs if e.type is EventType.TOOL_RESULT)
            check("real gateway executed ping",
                  tr.payload["ok"] and tr.payload["result"] == "pong hi")
            check("turn finished after loop",
                  evs[-1].payload["stop_reason"] == "end_turn")
            follow = MockAnthropic.requests[1]["body"]["messages"]
            check("assistant tool_use preserved in history",
                  any(isinstance(m["content"], list) and
                      any(b.get("type") == "tool_use" for b in m["content"])
                      for m in follow if m["role"] == "assistant"))
            check("tool_result fed back",
                  any(isinstance(m["content"], list) and
                      any(b.get("type") == "tool_result" and
                          b["content"][0]["text"] == "pong hi" and
                          b["is_error"] is False
                          for b in m["content"])
                      for m in follow if m["role"] == "user"))
            check("history is stateful across turns",
                  any(m.get("content") == "hello" for m in follow
                      if m["role"] == "user"))

            # the REAL ledger got the governed call (gateway is the appender)
            led = Ledger(os.path.join(td, "ledger.jsonl"))
            ok, n, _ = led.verify_chain()
            entries = led.entries()
            check("gateway ledgered the call, chain intact",
                  ok and any(e["type"] == "action" for e in entries)
                  and any(e["type"] == "tool_call" for e in entries))

            # http error path
            script((400, {"type": "error",
                          "error": {"message": "bad request"}}))
            evs, types = events_of(sess, "boom")
            check("api error -> ERROR+DONE",
                  types[-2:] == [EventType.ERROR, EventType.DONE] and
                  evs[-1].payload["stop_reason"] == "error")

            # 429 -> polite retry succeeds
            script((429, {"error": {"message": "slow down"}},
                    {"retry-after": "0"}),
                   (200, msg("end_turn", [text("ok now")])))
            evs, types = events_of(sess, "again")
            check("429 retried once",
                  len(MockAnthropic.requests) == 2 and
                  evs[-1].payload["stop_reason"] == "end_turn")

            # tool-round cap
            old_cap = ab.MAX_TOOL_ROUNDS
            ab.MAX_TOOL_ROUNDS = 2
            try:
                script(*[(200, msg("tool_use",
                                   [tool_use(f"t{i}", "ping", {})]))
                         for i in range(4)])
                evs, types = events_of(sess, "loop forever")
                check("tool-round cap stops the loop",
                      evs[-1].payload["stop_reason"] == "tool_round_cap")
            finally:
                ab.MAX_TOOL_ROUNDS = old_cap

            check("api key never in wire log",
                  all("sk-test-secret" not in frame for _, frame in wire))
        finally:
            sess.close()

    # ---- governance escalation through the REAL gate ---------------------------
    os.environ["MINERVA_ENABLE_TEST_TOOLS"] = "1"
    try:
        with tempfile.TemporaryDirectory() as td:
            adapter = ab.AnthropicByokAdapter(
                api_key="sk-test-secret",
                gateway_argv=GATEWAY + ["--state-dir", td],
                state_dir=td, api_url=api_url)
            sess = adapter.create_session(SessionConfig())
            try:
                check("test_spend registered when env-gated",
                      any(t["name"] == "test_spend" for t in sess.tools))
                script((200, msg("tool_use",
                                 [tool_use("toolu_9", "test_spend",
                                           {"item": "boat"})])),
                       (200, msg("end_turn", [text("standing down")])))
                evs, types = events_of(sess, "buy a boat")
                tr = next(e for e in evs if e.type is EventType.TOOL_RESULT)
                check("escalation surfaced to model",
                      not tr.payload["ok"] and
                      tr.payload["result"].startswith("ESCALATED to the operator"))
                ask = next(e for e in evs if e.type is EventType.ASK_USER)
                check("ASK_USER event carries the ref",
                      ask.payload["ref"] != "" and
                      ask.payload["ref"] in tr.payload["result"])
                pend = [json.loads(ln) for ln in
                        open(os.path.join(td, "approvals.jsonl"),
                             encoding="utf-8") if ln.strip()]
                check("approval queued for host_loop",
                      pend[0]["status"] == "pending" and
                      pend[0]["tool"] == "test_spend" and
                      pend[0]["ref"] == ask.payload["ref"])
                follow = MockAnthropic.requests[1]["body"]["messages"]
                check("escalation text fed back as error result",
                      any(isinstance(m["content"], list) and
                          any(b.get("type") == "tool_result" and b["is_error"]
                              for b in m["content"])
                          for m in follow if m["role"] == "user"))
            finally:
                sess.close()
    finally:
        os.environ.pop("MINERVA_ENABLE_TEST_TOOLS", None)

    # ---- per-session allowlist REALLY works on this lane ------------------------
    with tempfile.TemporaryDirectory() as td:
        adapter = ab.AnthropicByokAdapter(
            api_key="sk-test-secret", gateway_argv=GATEWAY + ["--state-dir", td],
            state_dir=td, api_url=api_url)
        sess = adapter.create_session(SessionConfig(tools=("ping",)))
        try:
            check("cfg.tools allowlists the gateway (the 0.16.0 gap, closed here)",
                  [t["name"] for t in sess.tools] == ["ping"])
        finally:
            sess.close()

    # ---- no key -> loud failure --------------------------------------------------
    os.environ.pop("ANTHROPIC_API_KEY", None)
    old_env_path, ab.ANTHROPIC_ENV = ab.ANTHROPIC_ENV, os.path.join("nowhere", "x.env")
    try:
        ab.AnthropicByokAdapter(api_key=None, api_url=api_url,
                                gateway_argv=GATEWAY)
        check("missing key raises", False)
    except RuntimeError as exc:
        check("missing key raises", "anthropic.env" in str(exc))
    finally:
        ab.ANTHROPIC_ENV = old_env_path

    server.shutdown()
    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
