r"""Live exercise: prove the gateway folds the operator's pitch verdicts in.

What it does, in plain terms: you already recorded your verdict(s) with
host_loop (they sit in state/verdicts.jsonl, NOT yet in Minerva's ledger).
This starts the REAL gateway on the REAL state folder for a moment - the
gateway folds your verdicts into the hash-chained ledger at startup (that
is the new behavior being proven), answers one ping, and exits. Then this
script verifies the whole paper trail and prints a plain summary.

Run it ON THE HOST from the project root, with no brainstorm tick running:
  python scripts\probe_verdict_fold.py

Writes: state/verdict-fold-report.json (verdicts of this probe)
Appends (via the gateway, the single ledger appender): your pitch_verdict
entries + one ping action/tool_call pair.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
STATE = os.path.join(ROOT, "state")
LEDGER_PATH = os.path.join(STATE, "ledger.jsonl")
VERDICTS_PATH = os.path.join(STATE, "verdicts.jsonl")
TICK_LOCK = os.path.join(STATE, "drive-tick.lock")
REPORT_PATH = os.path.join(STATE, "verdict-fold-report.json")

sys.path.insert(0, SRC)
from minerva_core.ledger import Ledger, ProposalIndex          # noqa: E402
from minerva_core.verdicts import pending_decisions, read_lines  # noqa: E402


def rpc(proc, msg: dict) -> None:
    proc.stdin.write((json.dumps(msg, ensure_ascii=True) + "\n").encode("ascii"))
    proc.stdin.flush()


def read_response(proc, want_id: int, timeout_s: float = 20.0) -> dict | None:
    end = time.time() + timeout_s
    while time.time() < end:
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            m = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if m.get("id") == want_id:
            return m
    return None


def main() -> int:
    report = {"ts": time.time(), "verdicts": {}, "errors": []}
    v = report["verdicts"]

    if os.path.exists(TICK_LOCK):
        print("STOP: a brainstorm tick appears to be running (state\\drive-tick.lock"
              " exists). Wait for it to finish, then rerun.")
        return 1

    pend_before = pending_decisions(VERDICTS_PATH)
    if not pend_before:
        print("Nothing to fold: no recorded verdicts are waiting.")
        print("Record one first:  python scripts\\host_loop.py verdict")
        return 1
    led = Ledger(LEDGER_PATH)
    ok0, n0, _ = led.verify_chain()
    v["chain_intact_before"] = ok0
    v["entries_before"] = n0
    v["pending_before"] = len(pend_before)
    print(f"before: ledger {n0} entries (chain {'OK' if ok0 else 'BROKEN'}), "
          f"{len(pend_before)} verdict(s) waiting to fold")
    if not ok0:
        print("STOP: chain broken BEFORE the probe - investigate first.")
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "minerva_core.gateway", "--state-dir", STATE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, creationflags=flags)
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18",
                              "capabilities": {}, "clientInfo":
                              {"name": "verdict-fold-probe", "version": "0"}}})
        init = read_response(proc, 1)
        v["gateway_initialized"] = bool(init and "result" in init)
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "ping",
                              "arguments": {"echo": "verdict-fold-probe"}}})
        pong = read_response(proc, 2)
        v["ping_ok"] = bool(pong and "result" in pong and
                            "pong" in json.dumps(pong["result"]))
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            report["errors"].append("gateway did not exit; killed")
        stderr_tail = proc.stderr.read().decode("utf-8", "replace")[-2000:]
        report["gateway_stderr_tail"] = stderr_tail

    led2 = Ledger(LEDGER_PATH)
    ok1, n1, bad = led2.verify_chain()
    v["chain_intact_after"] = ok1
    v["entries_after"] = n1
    pend_after = pending_decisions(VERDICTS_PATH)
    v["pending_after"] = len(pend_after)
    folded_markers = [r for r in read_lines(VERDICTS_PATH)
                      if r.get("status") == "folded"]
    v["folded_markers"] = len(folded_markers)
    pv = [e for e in led2.entries() if e["type"] == "pitch_verdict"]
    v["pitch_verdicts_in_ledger"] = len(pv)

    idx = ProposalIndex(led2)
    states = {}
    for d in pend_before:
        states[d["pitch"]] = idx.state(d["pitch"])
    report["pitch_states_after"] = states

    passed = (v["gateway_initialized"] and v["ping_ok"] and ok1
              and v["pending_after"] == 0 and len(pv) >= len(pend_before)
              and not report["errors"])
    report["passed"] = passed

    with open(REPORT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2, ensure_ascii=True)

    print("\n=== VERDICT FOLD PROBE ===")
    for k, val in v.items():
        print(f"  {k}: {val}")
    for pid, st in states.items():
        print(f"  pitch {pid} is now: {st}")
    print(f"  PASSED: {passed}")
    print(f"report -> {REPORT_PATH}")
    if not passed and report["errors"]:
        print("errors:", "; ".join(report["errors"]))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
