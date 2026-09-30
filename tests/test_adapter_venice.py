"""Venice adapter (#3, hosted-open/uncensored lane) tests: scripted
OpenAI-compatible API mock + the REAL gateway subprocess, broker routing,
key handling. Stdlib only:  python3 tests/test_adapter_venice.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.adapters.venice import (VeniceAdapter, default_model,
                                          load_venice_key)
from minerva_core.engine import (BrokerPolicy, EngineRegistry, RoutingHints,
                                 SessionConfig)
from minerva_core.events import EventType
from minerva_core.ledger import Ledger

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GATEWAY = [sys.executable, os.path.join(ROOT_DIR, "scripts", "run_gateway.py")]
KEY = "sk-ven-secret-XYZ"

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


# ---------------------------------------------------------------------------
class _MockVenice(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:                                # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(body)                     # type: ignore[attr-defined]
        script = self.server.script                           # type: ignore[attr-defined]
        step = script.pop(0) if script else {"status": 200, "body": _done("(exhausted)")}
        if "body" not in step:                       # bare body = 200 response
            step = {"status": 200, "body": step}
        payload = json.dumps(step["body"]).encode()
        self.send_response(step.get("status", 200))
        for k, v in step.get("headers", {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve(script: list[dict]) -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), _MockVenice)
    srv.script = list(script)                                 # type: ignore[attr-defined]
    srv.requests = []                                         # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _tool_call(name: str, args: str, call_id: str = "c1") -> dict:
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": args}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _done(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


def _run(td: str, script: list[dict], tools=("ping",), persona="You are Minerva.",
         env: dict | None = None):
    """-> (events, requests, frames). Real gateway child against td."""
    saved = {}
    for k, v in (env or {}).items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    frames: list = []
    srv, base = _serve(script)
    try:
        adapter = VeniceAdapter(api_key=KEY,
                                gateway_argv=GATEWAY + ["--state-dir", td],
                                api_base=base,
                                wire_log=lambda d, f: frames.append((d, f)))
        cfg = SessionConfig(persona=persona, model="venice-uncensored",
                            tools=tuple(tools))
        sess = adapter.create_session(cfg)
        try:
            events = list(sess.send("hello"))
        finally:
            sess.close()
        return events, srv.requests, frames                   # type: ignore[attr-defined]
    finally:
        srv.shutdown()
        srv.server_close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_tool_roundtrip(td: str) -> None:
    events, reqs, frames = _run(
        td, [_tool_call("ping", json.dumps({"echo": "venice"})), _done("VENICE-OK")])
    check("two API calls made", len(reqs) == 2)
    check("persona rides as system message",
          reqs[0]["messages"][0] == {"role": "system", "content": "You are Minerva."})
    fns = [t["function"]["name"] for t in reqs[0].get("tools", [])]
    check("allowlist enforced: model saw exactly [ping]", fns == ["ping"])
    tc = [e for e in events if e.type is EventType.TOOL_CALL]
    check("TOOL_CALL event with parsed args",
          len(tc) == 1 and tc[0].payload["tool"] == "ping"
          and tc[0].payload["args"] == {"echo": "venice"})
    tr = [e for e in events if e.type is EventType.TOOL_RESULT]
    check("governed result returned", tr and tr[0].payload["ok"]
          and "pong venice" in tr[0].payload["result"])
    roles = [m.get("role") for m in reqs[1]["messages"]]
    check("tool result fed back OpenAI-style", "tool" in roles
          and any(m.get("tool_call_id") == "c1" for m in reqs[1]["messages"]
                  if m.get("role") == "tool"))
    msg = [e for e in events if e.type is EventType.MESSAGE]
    check("final MESSAGE assembled", msg and msg[-1].payload["text"] == "VENICE-OK")
    done = [e for e in events if e.type is EventType.DONE]
    check("DONE stop", done and done[-1].payload["stop_reason"] == "stop")
    costs = [e for e in events if e.type is EventType.COST]
    check("COST token-only (usd 0)", len(costs) == 2
          and all(c.payload["usd"] == 0.0 for c in costs)
          and costs[0].payload["input_tokens"] == 10)
    blob = json.dumps([f for _d, f in frames])
    check("key never enters wire log", KEY not in blob and frames)
    ok, n, _ = Ledger(os.path.join(td, "ledger.jsonl")).verify_chain()
    check("real gateway ledgered the call, chain intact", ok and n >= 2)


def test_bad_args(td: str) -> None:
    events, _r, _f = _run(td, [_tool_call("ping", "{not json"), _done("ok")])
    tr = [e for e in events if e.type is EventType.TOOL_RESULT]
    check("malformed tool-args degrade to {} (call still governed)",
          tr and tr[0].payload["ok"] and "pong" in tr[0].payload["result"])


def test_retry_and_error(td: str) -> None:
    events, reqs, _f = _run(td, [
        {"status": 429, "body": {"error": {"message": "slow down"}},
         "headers": {"retry-after": "0"}},
        _done("after retry")])
    check("429 retried once then succeeds",
          len(reqs) == 2 and not [e for e in events if e.type is EventType.ERROR]
          and [e for e in events if e.type is EventType.MESSAGE])

    events, reqs, _f = _run(td, [
        {"status": 500, "body": {"error": {"message": "boom"}}},
        {"status": 500, "body": {"error": {"message": "boom"}}}])
    errs = [e for e in events if e.type is EventType.ERROR]
    done = [e for e in events if e.type is EventType.DONE]
    check("persistent 5xx -> fatal ERROR + DONE(error)",
          errs and errs[0].payload["fatal"]
          and done and done[-1].payload["stop_reason"] == "error")


def test_escalation(td: str) -> None:
    events, _r, _f = _run(
        td, [_tool_call("test_spend", json.dumps({"item": "gpu"})), _done("ok")],
        tools=("ping", "test_spend"), env={"MINERVA_ENABLE_TEST_TOOLS": "1"})
    tr = [e for e in events if e.type is EventType.TOOL_RESULT]
    check("escalated tool returns ESCALATED (not executed)",
          tr and not tr[0].payload["ok"]
          and tr[0].payload["result"].startswith("ESCALATED to the operator"))
    asks = [e for e in events if e.type is EventType.ASK_USER]
    check("ASK_USER surfaced with ref", asks and asks[0].payload["ref"])
    with open(os.path.join(td, "approvals.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    check("approval queued in the lane's state dir",
          any(r.get("status") == "pending" and r.get("tool") == "test_spend"
              for r in rows))


def test_key_loading(tmp: str) -> None:
    saved = os.environ.pop("VENICE_API_KEY", None)
    try:
        check("missing env+file -> None",
              load_venice_key(os.path.join(tmp, "nope.env")) is None)
        p = os.path.join(tmp, "venice.env")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# comment\nVENICE_API_KEY=\nVENICE_MODEL=\n")
        check("placeholder (empty value) counts as missing",
              load_venice_key(p) is None)
        with open(p, "w", encoding="utf-8") as f:
            f.write("VENICE_API_KEY=sk-from-file\n")
        check("file key loads", load_venice_key(p) == "sk-from-file")
        os.environ["VENICE_API_KEY"] = "sk-from-env"
        check("env wins over file", load_venice_key(p) == "sk-from-env")
        a = VeniceAdapter(api_key="explicit", gateway_argv=["x"])
        check("explicit api_key accepted", a.api_key == "explicit")
    finally:
        os.environ.pop("VENICE_API_KEY", None)
        if saved is not None:
            os.environ["VENICE_API_KEY"] = saved


def test_model_default(tmp: str) -> None:
    saved = os.environ.pop("VENICE_MODEL", None)
    saved_env = os.environ.get("MINERVA_VENICE_ENV")
    os.environ["MINERVA_VENICE_ENV"] = os.path.join(tmp, "no-such.env")
    try:
        check("default model alias", default_model() == "venice-uncensored")
        p = os.path.join(tmp, "venice-pin.env")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# c\nVENICE_API_KEY=k\nVENICE_MODEL=pin-from-file\n")
        os.environ["MINERVA_VENICE_ENV"] = p
        check("VENICE_MODEL pin in venice.env honored (round-13 fix)",
              default_model() == "pin-from-file")
        os.environ["VENICE_MODEL"] = "qwen-2026-uncensored"
        check("env var beats the file pin",
              default_model() == "qwen-2026-uncensored")
        a = VeniceAdapter(api_key=KEY, gateway_argv=["x"])
        check("adapter picks up the override", a.model == "qwen-2026-uncensored")
    finally:
        os.environ.pop("VENICE_MODEL", None)
        if saved is not None:
            os.environ["VENICE_MODEL"] = saved
        if saved_env is None:
            os.environ.pop("MINERVA_VENICE_ENV", None)
        else:
            os.environ["MINERVA_VENICE_ENV"] = saved_env


class _FakeFrontier:
    name = "fake-frontier"

    def capabilities(self) -> set[str]:
        return {"frontier", "tools_mcp"}

    def create_session(self, cfg):                            # pragma: no cover
        raise NotImplementedError


def test_routing() -> None:
    reg = EngineRegistry(BrokerPolicy())
    reg.register(_FakeFrontier(), default=True)
    try:
        reg.pick(RoutingHints(guardrail_tier="hosted-open"))
        loud = False
    except RuntimeError as e:
        loud = "venice" in str(e).lower()
    check("hosted-open with no Venice -> loud refusal naming the fix", loud)
    venice = VeniceAdapter(api_key=KEY, gateway_argv=["x"])
    reg.register(venice)
    for tier in ("hosted-open", "open", "uncensored"):
        a, cfg = reg.route(RoutingHints(guardrail_tier=tier))
        check(f"guardrail_tier={tier} routes to venice",
              a is venice and cfg.model == "")
    a, _cfg = reg.route(RoutingHints())
    check("default jobs stay on the default engine", a.name == "fake-frontier")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_tool_roundtrip(os.path.join(tmp, "t1"))
        test_bad_args(os.path.join(tmp, "t2"))
        test_retry_and_error(os.path.join(tmp, "t3"))
        test_escalation(os.path.join(tmp, "t4"))
        test_key_loading(tmp)
        test_model_default(tmp)
        test_routing()
    print(f"\nall {PASSED} checks passed")
