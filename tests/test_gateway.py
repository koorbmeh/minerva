"""Gateway tests: real subprocess over stdio (the way an engine launches it),
plus in-process governance ask-path. Stdlib only:  python3 tests/test_gateway.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minerva_core.gateway.mcp_stdio import MCPGateway
from minerva_core.gateway.registry import ToolRegistry, ToolSpec
from minerva_core.governance import GovernanceGate
from minerva_core.ledger import Ledger, ProposalIndex

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class GatewayProc:
    def __init__(self, state_dir: str, brain_dir: str) -> None:
        env = dict(os.environ)
        src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        # keep the default registry machine-independent: never read the real
        # gmail/calendar configs (round 14)
        env["MINERVA_GMAIL_ENV"] = os.path.join(state_dir, "no-gmail.env")
        env["MINERVA_CALENDAR_ENV"] = os.path.join(state_dir, "no-cal.env")
        self.p = subprocess.Popen(
            [sys.executable, "-m", "minerva_core.gateway",
             "--state-dir", state_dir, "--brain", brain_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def notify(self, method: str) -> None:
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def call(self, tool: str, args: dict) -> tuple[str, bool]:
        r = self.request("tools/call", {"name": tool, "arguments": args})
        res = r["result"]
        return res["content"][0]["text"], res["isError"]

    def close(self) -> int:
        self.p.stdin.close()
        return self.p.wait(timeout=10)


def test_stdio_protocol(tmp: str) -> None:
    brain = os.path.join(tmp, "brain")
    os.makedirs(brain)
    with open(os.path.join(brain, "vehicles.md"), "w", encoding="utf-8") as f:
        f.write("# Vehicles\nvehicle: 2019 Honda Civic owned by the operator\n")
    state = os.path.join(tmp, "state")

    gw = GatewayProc(state, brain)
    try:
        r = gw.request("initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {}, "clientInfo": {"name": "test"}})
        check("initialize echoes protocol", r["result"]["protocolVersion"] == "2025-06-18")
        check("serverInfo present", r["result"]["serverInfo"]["name"] == "minerva-gateway")
        gw.notify("notifications/initialized")

        r = gw.request("tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        check("all starter tools listed",          # round 26: + job_*
              names == {"ping", "brain_recall", "episodic_note", "pitch_idea",
                        "notify_operator", "web_fetch", "files_list", "files_read",
                        "files_write", "generate_image", "generate_video",
                        "connections_status", "request_connection",
                        "spend_status", "send_file_to_operator",
                        "agenda_list", "agenda_add", "agenda_update",
                        "job_submit", "job_list", "job_result",
                        "decisions_pending", "record_verdict",
                        "resolve_approval"})

        text, err = gw.call("ping", {"echo": "minerva"})
        check("ping works", text == "pong minerva" and not err)

        text, err = gw.call("brain_recall", {"query": "civic"})
        check("brain_recall finds fact", "Honda Civic" in text and not err)

        text, err = gw.call("pitch_idea", {"domain": "income", "title": "Sell spare camera gear"})
        check("pitch accepted via MCP", "pitch logged" in text and not err)
        text, err = gw.call("pitch_idea", {"domain": "income", "title": "sell spare camera gear!!"})
        check("anti-nag rejects duplicate via MCP", "REJECTED by anti-nag" in text and not err)

        text, err = gw.call("episodic_note", {"text": "The operator mentioned wanting quieter keyboard"})
        check("episodic note lands", text.startswith("noted (") and not err)

        text, err = gw.call("notify_operator", {"message": "build green", "priority": "low"})
        check("notify queues to outbox", "queued" in text and not err)

        text, err = gw.call("files_write", {"path": "notes/hello.txt",
                                            "content": "workspace works"})
        check("files_write lands in workspace",
              text.startswith("wrote") and not err)
        text, err = gw.call("files_read", {"path": "notes/hello.txt"})
        check("files_read round-trips", "workspace works" in text and not err)
        text, err = gw.call("files_read", {"path": "../ledger.jsonl"})
        check("files_* containment holds (no state-dir reach)",
              err and "tool failed" in text)
        text, err = gw.call("web_fetch", {"url": "ftp://example.com/x"})
        check("web_fetch rejects non-http scheme",
              "not allowed" in text and not err)

        text, err = gw.call("nonexistent_tool", {})
        check("unknown tool is isError", err and "unknown tool" in text)

        r = gw.request("bogus/method")
        check("unknown method -> -32601", r.get("error", {}).get("code") == -32601)
    finally:
        code = gw.close()
    check("clean exit on stdin close", code == 0)

    with open(os.path.join(state, "outbox.jsonl"), encoding="utf-8") as f:
        check("outbox content correct", json.loads(f.readline())["message"] == "build green")
    ok, n, _ = Ledger(os.path.join(state, "ledger.jsonl")).verify_chain()
    check("ledger chain intact after session", ok and n >= 8)


def test_ask_escalation(tmp: str) -> None:
    state = os.path.join(tmp, "state2")
    os.makedirs(state)
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))
    gate = GovernanceGate(ledger, spend_ask_threshold_usd=5.0)
    reg = ToolRegistry()
    reg.register(ToolSpec(name="send_email", description="…",
                          input_schema={"type": "object"},
                          handler=lambda a: "sent",
                          classes={"outbound_comms"}))
    reg.register(ToolSpec(name="rent_gpu", description="…",
                          input_schema={"type": "object"},
                          handler=lambda a: "rented",
                          est_cost_usd=12.0))
    approvals = os.path.join(state, "approvals.jsonl")
    gw = MCPGateway(reg, gate, ledger, approvals_path=approvals)

    res = gw.call_tool("send_email", {"to": "robin@example.com"})
    check("outbound comms escalates, not executes",
          res["isError"] and "ESCALATED" in res["content"][0]["text"]
          and "sent" not in res["content"][0]["text"])
    res = gw.call_tool("rent_gpu", {"gpu": "H100"})
    check("big spend escalates", res["isError"] and "ESCALATED" in res["content"][0]["text"])

    with open(approvals, encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    check("approvals queued with refs", len(rows) == 2
          and all(r["status"] == "pending" and r["ref"] for r in rows))

    res = gw.call_tool("ping", {})
    check("registry restriction boundary", res["isError"])  # ping not in this registry


def test_restrict(tmp: str) -> None:
    state = os.path.join(tmp, "state3")
    os.makedirs(state)
    ledger = Ledger(os.path.join(state, "ledger.jsonl"))
    from minerva_core.gateway.tools_builtin import build_default_registry
    reg = build_default_registry(ledger, ProposalIndex(ledger),
                                 outbox_path=os.path.join(state, "outbox.jsonl"),
                                 brain_dir=None)
    check("no brain dir -> no brain_recall", "brain_recall" not in reg.names())
    reg.restrict({"ping", "pitch_idea"})
    check("allowlist restriction works", reg.names() == ["ping", "pitch_idea"])


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_stdio_protocol(tmp)
        test_ask_escalation(tmp)
        test_restrict(tmp)
    print(f"\nall {PASSED} checks passed")
