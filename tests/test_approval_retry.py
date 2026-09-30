"""Approval-honoring retry (gate v0.1) — ask -> the operator decides -> retry honored.

Drives MCPGateway.call_tool in-process with a real Ledger + approvals file in a
temp dir; the operator's decisions recorded via host_loop.record_decision (the same
code path the CLI uses).  Run: python tests/test_approval_retry.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import host_loop  # noqa: E402
from minerva_core.governance import GovernanceGate  # noqa: E402
from minerva_core.gateway.mcp_stdio import MCPGateway  # noqa: E402
from minerva_core.gateway.registry import ToolRegistry, ToolSpec  # noqa: E402
from minerva_core.ledger import Ledger, ProposalIndex, VERDICT  # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def text(res: dict) -> str:
    return res["content"][0]["text"]


def rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        ledger_path = os.path.join(td, "ledger.jsonl")
        approvals = os.path.join(td, "approvals.jsonl")
        ledger = Ledger(ledger_path)
        gate = GovernanceGate(ledger, spend_ask_threshold_usd=5.0,
                              approvals_path=approvals)
        reg = ToolRegistry()
        reg.register(ToolSpec(name="ping", description="ok",
                              input_schema={"type": "object"},
                              handler=lambda a: "pong"))
        reg.register(ToolSpec(name="buy", description="spendy",
                              input_schema={"type": "object"},
                              classes={"spend"}, est_cost_usd=9.0,
                              handler=lambda a: f"bought {a.get('item')}"))
        gw = MCPGateway(reg, gate, ledger, approvals_path=approvals)

        # 1. fresh escalation queues exactly one pending line
        res = gw.call_tool("buy", {"item": "gpu"})
        check("spendy call escalates", res["isError"] and "ESCALATED" in text(res))
        check("retry guidance in escalation text", "retry this exact call" in text(res))
        pend = host_loop.pending_approvals(approvals)
        check("one pending row queued", len(pend) == 1 and pend[0]["tool"] == "buy")
        ref = pend[0]["ref"]

        # 2. identical call while pending: no duplicate queue, told to stand by
        res = gw.call_tool("buy", {"item": "gpu"})
        check("identical call -> STILL AWAITING (no re-queue)",
              res["isError"] and "STILL AWAITING" in text(res) and ref in text(res))
        check("still exactly one pending row",
              len(host_loop.pending_approvals(approvals)) == 1)

        # 3. different args -> different fingerprint -> fresh escalation
        res = gw.call_tool("buy", {"item": "boat"})
        check("changed args escalate fresh", "ESCALATED" in text(res))
        pend = host_loop.pending_approvals(approvals)
        check("two pending rows now", len(pend) == 2)
        boat_ref = [p["ref"] for p in pend if p["args"].get("item") == "boat"][0]

        # 4. approve gpu -> retry passes once, VERDICT ledgered, approval consumed
        host_loop.record_decision(ref, True, "go ahead", approvals)
        res = gw.call_tool("buy", {"item": "gpu"})
        check("approved retry executes", not res["isError"] and text(res) == "bought gpu")
        verdicts = [e for e in ledger.entries() if e["type"] == VERDICT]
        check("VERDICT ledgered, parented to the original ask",
              len(verdicts) == 1 and verdicts[0]["parent_id"] == ref
              and verdicts[0]["payload"]["decision"] == "allow"
              and verdicts[0]["payload"]["note"] == "go ahead"
              and verdicts[0]["actor"] == "operator")
        last = [r for r in rows(approvals) if r["ref"] == ref][-1]
        check("approval consumed (single-use)", last["status"] == "consumed"
              and last["by"] == "gateway")

        # 5. same call again -> consumed approval is gone -> fresh escalation
        res = gw.call_tool("buy", {"item": "gpu"})
        check("post-consumption call escalates fresh", "ESCALATED" in text(res))
        new_ref = [p["ref"] for p in host_loop.pending_approvals(approvals)
                   if p["args"].get("item") == "gpu"][0]
        check("fresh cycle has a new ref", new_ref != ref)

        # 6. denial stands for the fingerprint
        host_loop.record_decision(boat_ref, False, "no boats", approvals)
        res = gw.call_tool("buy", {"item": "boat"})
        check("denied retry refused with the operator's reason",
              res["isError"] and "operator denied" in text(res) and "no boats" in text(res))
        res = gw.call_tool("buy", {"item": "boat"})
        check("denial stands on repeat", res["isError"] and "operator denied" in text(res))
        check("denial did not re-queue",
              all(p["args"].get("item") != "boat"
                  for p in host_loop.pending_approvals(approvals)))

        # 7. unrelated tool unaffected; ledger chain intact end-to-end
        res = gw.call_tool("ping", {})
        check("non-ask tool unaffected", not res["isError"] and text(res) == "pong")
        ok, n, bad = ledger.verify_chain()
        check("ledger chain intact", ok and n > 8)

        # 8. env-gated test tool registration
        from minerva_core.gateway.tools_builtin import build_default_registry
        prop = ProposalIndex(ledger)
        r0 = build_default_registry(ledger, prop, os.path.join(td, "o.jsonl"))
        check("test_spend absent by default", r0.get("test_spend") is None)
        os.environ["MINERVA_ENABLE_TEST_TOOLS"] = "1"
        try:
            r1 = build_default_registry(ledger, prop, os.path.join(td, "o.jsonl"))
            spec = r1.get("test_spend")
            check("test_spend present when env-gated on",
                  spec is not None and spec.classes == {"spend"}
                  and spec.est_cost_usd > 5.0)
        finally:
            del os.environ["MINERVA_ENABLE_TEST_TOOLS"]

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
