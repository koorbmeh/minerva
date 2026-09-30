"""Kill switch (round 16): the STOP file halts the gate, ticks, and chat -
externally, instantly, audibly (denials are ledgered). Stdlib only:
  python3 tests/test_killswitch.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from minerva_core.governance import GovernanceGate
from minerva_core.killswitch import is_stopped, resume, stop, stop_path
from minerva_core.ledger import Ledger

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def test_file(tmp: str) -> None:
    sd = os.path.join(tmp, "s1")
    check("file: not stopped when absent", is_stopped(sd) is None)
    p = stop(sd, "testing the brakes")
    check("file: stop() creates the STOP file",
          os.path.exists(p) and p == stop_path(sd))
    why = is_stopped(sd)
    check("file: reason + timestamp readable",
          "testing the brakes" in why and "since" in why)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{garbage")
    check("file: unreadable STOP still stops (fail safe)",
          "unreadable" in is_stopped(sd))
    check("file: resume removes it", resume(sd) and is_stopped(sd) is None)
    check("file: resume with nothing to remove says so", resume(sd) is False)


def test_gate(tmp: str) -> None:
    sd = os.path.join(tmp, "s2")
    os.makedirs(sd, exist_ok=True)
    ledger = Ledger(os.path.join(sd, "ledger.jsonl"))
    gate = GovernanceGate(ledger, approvals_path=os.path.join(sd, "a.jsonl"),
                          state_dir=sd)
    d = gate.check("ping", {}, classes=set(), est_cost_usd=0.0)
    check("gate: runs normally with no STOP", d.decision == "allow")
    stop(sd, "hold everything")
    d = gate.check("ping", {}, classes=set(), est_cost_usd=0.0)
    check("gate: STOP denies even free calls",
          d.decision == "deny" and "KILL SWITCH" in d.reason
          and "hold everything" in d.reason)
    d = gate.check("brain_recall", {"query": "x"}, classes=set())
    check("gate: STOP beats everything else", d.decision == "deny")
    entries = ledger.entries()
    check("gate: denials are LEDGERED (audit of what she tried while stopped)",
          sum(1 for e in entries
              if e["payload"].get("decision") == "deny"
              and "KILL SWITCH" in e["payload"].get("reason", "")) == 2)
    resume(sd)
    d = gate.check("ping", {}, classes=set())
    check("gate: resume restores normal operation", d.decision == "allow")
    ok, _n, _bad = ledger.verify_chain()
    check("gate: chain intact through stop/resume", ok)
    gate2 = GovernanceGate(ledger, approvals_path=None)
    check("gate: no state_dir wired -> legacy behavior unchanged",
          gate2.check("ping", {}, classes=set()).decision == "allow")


def test_tick(tmp: str) -> None:
    import drive_tick
    sd = os.path.join(tmp, "s3")
    os.makedirs(sd, exist_ok=True)
    stop(sd, "no ticks today")
    report = drive_tick.run_tick(sd, engine_cmd=["definitely-not-a-binary"],
                                 quiet=True)
    check("tick: refuses to start while stopped (no engine spawn)",
          report["ok"] is False
          and any("kill switch" in e for e in report["errors"]))
    log = [json.loads(x) for x in
           open(os.path.join(sd, "drive-tick-log.jsonl"), encoding="utf-8")]
    check("tick: skip is logged honestly",
          log and log[-1]["stop_reason"] == "killswitch")
    check("tick: no trace written (nothing ran)",
          not os.path.exists(os.path.join(sd, "drive-tick-trace.jsonl")))


def test_chat(tmp: str) -> None:
    import minerva_chat
    sd = os.path.join(tmp, "s4")
    os.makedirs(sd, exist_ok=True)
    stop(sd, "quiet time")

    def exploding_factory(state_dir):
        raise AssertionError("engine must not be created while stopped")

    reply = minerva_chat.run_chat("hello?", state_dir=sd,
                                  engine_factory=exploding_factory)
    check("chat: stopped reply, no engine session, no spend",
          reply.startswith("I'm STOPPED") and "quiet time" in reply)
    check("chat: exchange still logged",
          os.path.exists(minerva_chat.chat_log_path(sd)))
    check("chat: no spend line written",
          not os.path.exists(os.path.join(sd, "spend.jsonl")))


def test_real_gateway(tmp: str) -> None:
    from minerva_core.adapters.anthropic_byok import GatewayClient
    sd = os.path.join(tmp, "gw")
    os.makedirs(sd, exist_ok=True)
    stop(sd, "live stop test")
    gw = GatewayClient(
        [sys.executable, os.path.join(ROOT_DIR, "scripts", "run_gateway.py"),
         "--state-dir", sd],
        extra_env={"MINERVA_TOOLS": "ping"})
    try:
        gw.initialize()
        text, err = gw.call("ping", {"echo": "anyone home?"})
        check("gateway: STOP file denies through the real gateway",
              err and "KILL SWITCH" in text and "live stop test" in text)
    finally:
        gw.close()
    ok, n, _ = Ledger(os.path.join(sd, "ledger.jsonl")).verify_chain()
    check("gateway: denial ledgered, chain intact", ok and n >= 1)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_file(tmp)
        test_gate(tmp)
        test_tick(tmp)
        test_chat(tmp)
        test_real_gateway(tmp)
    print(f"\nall {PASSED} checks passed")
