"""Budget governor (round 15): SpendMeter math, gate escalation on the daily
cap, lane recording (generation + chat), spend_status, real-gateway breach.
Stdlib only:  python3 tests/test_spend_budget.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.governance import GovernanceGate
from minerva_core.ledger import Ledger
from minerva_core.spend import (SpendMeter, daily_cap_usd, record_spend,
                                spend_path, spent_since)

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def test_meter(tmp: str) -> None:
    p = os.path.join(tmp, "spend.jsonl")
    check("meter: empty file -> $0", spent_since(p, 0) == 0.0)
    now = time.time()
    record_spend(p, 0.50, "video (wan)")
    record_spend(p, 0.04, "image x1", kind="est")
    record_spend(p, 0.0, "free thing")               # ignored
    record_spend(p, -3, "negative nonsense")         # ignored
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"ts": ' + str(now) + ', "usd": "garbage"}\n')
        f.write("{torn line\n")
    old = {"ts": now - 90000, "usd": 9.99, "what": "yesterday"}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(old) + "\n")
    check("meter: tolerant sum counts valid recent lines only",
          abs(spent_since(p, now - 60) - 0.54) < 1e-6)
    m = SpendMeter(p, cap_usd=1.0)
    ok, spent, cap = m.check(0.40)
    check("meter: under cap allows", ok and cap == 1.0
          and abs(spent - 0.54) < 1e-6)
    ok, _s, _c = m.check(0.50)
    check("meter: would-exceed blocks", not ok)
    ok, _s, _c = m.check(0.0)
    check("meter: free calls never budget-blocked", ok)
    check("meter: status text plain and numeric",
          "spend last 24h: $0.54 of $1.00" in m.status_text())
    saved = os.environ.get("MINERVA_DAILY_BUDGET_USD")
    os.environ["MINERVA_DAILY_BUDGET_USD"] = "2.5"
    try:
        check("meter: env knob read live when cap not pinned",
              SpendMeter(p).cap_usd == 2.5 and daily_cap_usd() == 2.5)
    finally:
        if saved is None:
            os.environ.pop("MINERVA_DAILY_BUDGET_USD", None)
        else:
            os.environ["MINERVA_DAILY_BUDGET_USD"] = saved


def test_gate(tmp: str) -> None:
    state = os.path.join(tmp, "gate")
    os.makedirs(state, exist_ok=True)
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))
    p = spend_path(state)
    meter = SpendMeter(p, cap_usd=1.0)
    approvals = os.path.join(state, "approvals.jsonl")
    gate = GovernanceGate(ledger, spend_ask_threshold_usd=5.0,
                          approvals_path=approvals, spend_meter=meter)

    d = gate.check("generate_image", {"prompt": "x"}, classes={"media_gen"},
                   est_cost_usd=0.04)
    check("gate: under budget -> allow (ledgered)", d.decision == "allow")
    record_spend(p, 0.99, "video")
    d = gate.check("generate_image", {"prompt": "x"}, classes={"media_gen"},
                   est_cost_usd=0.04)
    check("gate: over budget -> ASK with a plain budget reason",
          d.decision == "ask" and "daily budget" in d.reason
          and "$1.00" in d.reason)
    fp = gate.fingerprint("generate_image", {"prompt": "x"})
    with open(approvals, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ref": d.entry_id, "ts": time.time(),
                            "status": "pending", "tool": "generate_image",
                            "args": {"prompt": "x"}}) + "\n")
        f.write(json.dumps({"ref": d.entry_id, "ts": time.time(),
                            "status": "approved"}) + "\n")
    d2 = gate.check("generate_image", {"prompt": "x"}, classes={"media_gen"},
                    est_cost_usd=0.04)
    check("gate: The operator's approval overrides the budget block once",
          d2.decision == "allow" and "operator approved" in d2.reason)
    d3 = gate.check("ping", {}, classes=set(), est_cost_usd=0.0)
    check("gate: free calls unaffected by a blown budget",
          d3.decision == "allow")
    d4 = gate.check("rent_gpu", {}, classes=set(), est_cost_usd=12.0)
    check("gate: per-call threshold reason still wins over budget wording",
          d4.decision == "ask" and d4.reason.startswith("cost $12.00"))
    ok, _n, _bad = ledger.verify_chain()
    check("gate: chain intact through budget decisions", ok)


# ---- minimal Venice mock for the generation-lane recording ------------------
class _Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, raw=None, ctype="application/json"):
        payload = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):                                    # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(n)
        if path == "/image/generate":
            self._send({"images": [base64.b64encode(b"PNG").decode()]})
        elif path == "/video/quote":
            self._send({"quote": 0.42})
        elif path == "/video/queue":
            self._send({"queue_id": "q1"})
        elif path == "/video/retrieve":
            self._send(None, raw=b"MP4DATA", ctype="video/mp4")
        else:
            self._send({"error": "?"})


def test_lane_recording(tmp: str) -> None:
    from minerva_core.gateway import tools_generation as tg
    srv = HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    p = os.path.join(tmp, "lane-spend.jsonl")
    meter = SpendMeter(p, cap_usd=50.0)
    saved = {k: os.environ.get(k) for k in
             ("VENICE_API_KEY", "VENICE_API_BASE", "MINERVA_VENICE_ENV",
              "MINERVA_VIDEO_POLL_S")}
    os.environ.update({"VENICE_API_KEY": "k", "VENICE_API_BASE": base,
                       "MINERVA_VENICE_ENV": os.path.join(tmp, "no.env"),
                       "MINERVA_VIDEO_POLL_S": "0.01"})
    tg._MODEL_CACHE.clear()
    try:
        out = tg.generate_image(ws, {"prompt": "x", "model": "m"}, meter)
        check("lanes: image records an est on the meter",
              out.startswith("image saved:")
              and abs(meter.spent_24h() - 0.04) < 1e-6)
        out = tg.generate_video(ws, {"prompt": "x", "model": "m"}, meter)
        check("lanes: video records the REAL quote on the meter",
              out.startswith("video saved:")
              and abs(meter.spent_24h() - 0.46) < 1e-6)
        rows = [json.loads(x) for x in open(p, encoding="utf-8")]
        check("lanes: kinds honest (est vs actual)",
              {r["kind"] for r in rows} == {"est", "actual"})
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tg._MODEL_CACHE.clear()
        srv.shutdown()
        srv.server_close()


def test_chat_recording(tmp: str) -> None:
    sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
    import minerva_chat
    state = os.path.join(tmp, "chatstate")
    os.makedirs(state, exist_ok=True)

    class _Sess:
        id = "s1"
        def send(self, text):
            from minerva_core.events import EngineEvent, EventType
            yield EngineEvent(EventType.COST, "s1",
                              {"usd": 0.0123, "input_tokens": 1,
                               "output_tokens": 1})
            yield EngineEvent(EventType.MESSAGE, "s1", {"text": "hi there"})
            yield EngineEvent(EventType.DONE, "s1", {"stop_reason": "end"})
        def cancel(self):
            pass
        def close(self):
            pass

    class _Adapter:
        name = "fake"
        def capabilities(self):
            return set()
        def create_session(self, cfg):
            return _Sess()

    def factory(sd):
        from minerva_core.engine import SessionConfig
        return _Adapter(), SessionConfig(persona="p"), "fake"

    reply = minerva_chat.run_chat("hello", state_dir=state,
                                  engine_factory=factory)
    check("chat: reply produced", reply == "hi there")
    spent = spent_since(spend_path(state), time.time() - 60)
    check("chat: token cost recorded on the shared meter file",
          abs(spent - 0.0123) < 1e-6)


def test_real_gateway(tmp: str) -> None:
    from minerva_core.adapters.anthropic_byok import GatewayClient
    state = os.path.join(tmp, "gw")
    os.makedirs(state, exist_ok=True)
    record_spend(spend_path(state), 4.99, "preload")     # nearly at the $5 cap
    gw = GatewayClient(
        [sys.executable, os.path.join(ROOT_DIR, "scripts", "run_gateway.py"),
         "--state-dir", state],
        extra_env={"MINERVA_TOOLS": "generate_image,spend_status",
                   "VENICE_API_KEY": "k",
                   "MINERVA_VENICE_ENV": os.path.join(tmp, "no.env"),
                   "MINERVA_DAILY_BUDGET_USD": "5.0"})
    try:
        gw.initialize()
        text, err = gw.call("spend_status", {})
        check("gateway: spend_status reports the preloaded spend",
              not err and "$4.99 of $5.00" in text)
        text, err = gw.call("generate_image", {"prompt": "owl", "model": "m"})
        check("gateway: over-budget generation ESCALATES, not runs",
              err and text.startswith("ESCALATED to the operator"))
        with open(os.path.join(state, "approvals.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        check("gateway: budget ask queued for host_loop delivery",
              any(r.get("status") == "pending"
                  and r.get("tool") == "generate_image" for r in rows))
    finally:
        gw.close()
    ok, n, _ = Ledger(os.path.join(state, "ledger.jsonl")).verify_chain()
    check("gateway: chain intact", ok and n >= 2)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_meter(tmp)
        test_gate(tmp)
        test_lane_recording(tmp)
        test_chat_recording(tmp)
        test_real_gateway(tmp)
    print(f"\nall {PASSED} checks passed")
