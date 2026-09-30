"""Live exercise: ask -> the operator decides -> retry honored, on the REAL gateway.

Run ON THE HOST (no Hermes session running - single ledger writer rule):
  python scripts\\probe_approval_retry.py

What happens:
  1. spawns the production gateway (MCP stdio) with MINERVA_ENABLE_TEST_TOOLS=1
     against the MAIN state dir (this is a real, ledgered exercise);
  2. calls test_spend ($9.99 est > $5 threshold) -> gate ESCALATES, approval
     queued;
  3. drops you into host_loop's approve/deny prompt (type 'a' to test the
     approve path, 'd' for the deny path - both are valid exercises);
  4. retries the IDENTICAL call -> gate honors your decision: approved ->
     executes once + VERDICT ledgered (parented to the ask, actor=operator) +
     approval consumed; denied -> refused with your reason, denial stands;
  5. verifies the ledger chain + writes state/approval-retry-report.json.

The gateway subprocess is the only ledger writer while this runs; the probe
itself only reads state. test_spend never exists in production registries
(env-gated), so the leftover approval cycle lines are inert history.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# MINERVA_PROBE_STATE overrides the state dir (rehearsal isolation; default = real)
STATE = os.environ.get("MINERVA_PROBE_STATE") or os.path.join(ROOT, "state")
REPORT_PATH = os.path.join(STATE, "approval-retry-report.json")
GW_STDERR = os.path.join(STATE, "approval-retry-gateway-stderr.txt")
APPROVALS_PATH = os.path.join(STATE, "approvals.jsonl")

sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import host_loop  # noqa: E402
from minerva_core.ledger import Ledger  # noqa: E402

ARGS = {"item": "approval-flow-live-test"}


class GatewayClient:
    """Minimal MCP stdio client - the gateway answers synchronously per line."""

    def __init__(self) -> None:
        os.makedirs(STATE, exist_ok=True)
        env = {**os.environ, "MINERVA_STATE_DIR": STATE,
               "MINERVA_ENABLE_TEST_TOOLS": "1"}
        self._stderr_f = open(GW_STDERR, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "scripts", "run_gateway.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr_f, env=env)
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method,
               "params": params or {}}
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=True) + "\n").encode("ascii"))
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        if not raw:
            raise RuntimeError("gateway closed the pipe (see "
                               + GW_STDERR + ")")
        return json.loads(raw.decode("utf-8", "replace"))

    def notify(self, method: str) -> None:
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method},
                                          ensure_ascii=True) + "\n").encode("ascii"))
        self.proc.stdin.flush()

    def call(self, tool: str, args: dict) -> tuple[bool, str]:
        resp = self.request("tools/call", {"name": tool, "arguments": args})
        result = resp.get("result") or {}
        txt = ((result.get("content") or [{}])[0]).get("text", "")
        return bool(result.get("isError")), txt

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:                                         # noqa: BLE001
            self.proc.kill()
        self._stderr_f.close()


def main() -> int:
    print("LIVE approval-retry exercise (real gateway, real ledger).")
    if input("Confirm NO Hermes session is running [y/N] > ").strip().lower() != "y":
        print("aborted - close Hermes first (single ledger writer rule)")
        return 1

    os.makedirs(STATE, exist_ok=True)
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "errors": []}
    led_before = Ledger(os.path.join(STATE, "ledger.jsonl"))
    n_before = len(led_before.entries())

    gw = GatewayClient()
    try:
        init = gw.request("initialize", {"protocolVersion": "2025-06-18",
                                         "clientInfo": {"name": "retry-probe"}})
        gw.notify("notifications/initialized")
        tools = [t["name"] for t in gw.request("tools/list")["result"]["tools"]]
        report["tools"] = tools
        if "test_spend" not in tools:
            report["errors"].append("test_spend not registered (env gating broken?)")
            return 1
        print(f"gateway up; tools: {tools}")

        # 1. escalate
        is_err, txt = gw.call("test_spend", ARGS)
        report["escalation_text"] = txt
        m = re.search(r"ref ([0-9a-f]+-[0-9a-f]+)", txt)
        if not (is_err and "ESCALATED" in txt and m):
            report["errors"].append(f"expected ESCALATED, got: {txt}")
            return 1
        ref = m.group(1)
        report["ref"] = ref
        print(f"\nESCALATED as expected (ref {ref}). Your move:")

        # 2. The operator decides (the real host_loop prompt)
        host_loop.resolve_approvals(interactive=True,
                                    approvals_path=APPROVALS_PATH)

        # 3. identical retry - gate honors the decision
        is_err, txt = gw.call("test_spend", ARGS)
        report["retry_text"] = txt
        if not is_err and "test purchase complete" in txt:
            report["path"] = "approved"
            print(f"\nretry EXECUTED: {txt}")
        elif is_err and "operator denied" in txt:
            report["path"] = "denied"
            print(f"\nretry REFUSED with your denial: {txt}")
        else:
            report["errors"].append(f"unexpected retry outcome: {txt}")
    finally:
        gw.close()

    # 4. verify ledger from outside (gateway is dead; we are the only reader)
    led = Ledger(os.path.join(STATE, "ledger.jsonl"))
    ok, n, bad = led.verify_chain()
    entries = led.entries()
    report["ledger_entries_added"] = n - n_before
    report["chain_intact"] = ok
    if report.get("path") == "approved":
        v = [e for e in entries if e["type"] == "verdict" and e["parent_id"] == ref]
        report["verdict_ok"] = bool(v) and v[-1]["actor"] == "operator" \
            and v[-1]["payload"]["decision"] == "allow"
        last = [r for r in host_loop.read_jsonl(APPROVALS_PATH)
                if r.get("ref") == ref][-1]
        report["approval_consumed"] = last.get("status") == "consumed"
    elif report.get("path") == "denied":
        d = [e for e in entries if e["type"] == "action" and e["parent_id"] == ref]
        report["deny_ledgered"] = bool(d) and "operator denied" in d[-1]["payload"]["reason"]

    with open(REPORT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print("\n=== APPROVAL-RETRY LIVE SUMMARY ===")
    for k in ("path", "ref", "ledger_entries_added", "chain_intact",
              "verdict_ok", "approval_consumed", "deny_ledgered"):
        if k in report:
            print(f"  {k}: {report[k]}")
    print(f"report -> {REPORT_PATH}")
    print(f"errors: {len(report['errors'])}")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
