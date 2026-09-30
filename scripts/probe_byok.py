r"""Live probe #3: the BYOK frontier lane - one governed round-trip on YOUR key.

Run ON THE HOST:  python scripts\probe_byok.py
What it does, in plain terms: starts Minerva's rulebook (the gateway) in an
ISOLATED practice folder (state/byok-probe - your real ledger is not touched),
asks Claude (via your Anthropic key) to call the 'ping' tool through that
rulebook, and verifies the whole paper trail. One short turn on the cheapest
model - costs a fraction of a cent.

Safe to run any time (even while Hermes is open): the practice folder keeps
this gateway's ledger separate, so the single-appender rule is respected.

Writes: state/byok-trace.jsonl (wire frames, key never logged)
        state/byok-report.json (verdicts)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from minerva_core.adapters.anthropic_byok import (   # noqa: E402
    AnthropicByokAdapter, load_api_key)
from minerva_core.engine import SessionConfig        # noqa: E402
from minerva_core.events import EventType            # noqa: E402
from minerva_core.ledger import Ledger               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
PROBE_STATE = os.path.join(STATE, "byok-probe")
TRACE_PATH = os.path.join(STATE, "byok-trace.jsonl")
REPORT_PATH = os.path.join(STATE, "byok-report.json")
DEFAULT_PROBE_MODEL = "claude-haiku-4-5-20251001"    # cheapest sane choice

PROMPT = ("Call the tool ping once with echo set to 'byok-live'. "
          "After it returns, reply with exactly: BYOK-OK")


def main() -> int:
    ap = argparse.ArgumentParser(prog="minerva-probe-byok")
    ap.add_argument("--model", default=DEFAULT_PROBE_MODEL)
    args = ap.parse_args()

    if not load_api_key():
        print("No Anthropic key found. Put it in config\\anthropic.env like:")
        print("  ANTHROPIC_API_KEY=sk-ant-...")
        return 1

    os.makedirs(PROBE_STATE, exist_ok=True)
    ledger_path = os.path.join(PROBE_STATE, "ledger.jsonl")
    n_before = 0
    if os.path.exists(ledger_path):
        with open(ledger_path, encoding="utf-8") as f:
            n_before = sum(1 for ln in f if ln.strip())

    trace_f = open(TRACE_PATH, "w", encoding="utf-8", newline="\n")

    def wire_log(direction: str, frame: dict) -> None:
        trace_f.write(json.dumps({"t": round(time.time(), 3), "dir": direction,
                                  "msg": frame}, ensure_ascii=True) + "\n")
        trace_f.flush()

    print(f"BYOK live probe - model {args.model}")
    print("(isolated practice folder; your real ledger is untouched; "
          "cost is a fraction of a cent)\n")

    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "model": args.model, "errors": [], "events": []}
    events = []
    sess = None
    adapter = None
    try:
        adapter = AnthropicByokAdapter(state_dir=PROBE_STATE,
                                       model=args.model, wire_log=wire_log)
        sess = adapter.create_session(SessionConfig(tools=("ping",)))
        report["tools_registered"] = [t["name"] for t in sess.tools]
        for ev in sess.send(PROMPT):
            events.append(ev.to_dict())
            print(f"  [{ev.type.value}] "
                  + json.dumps(ev.payload, ensure_ascii=True)[:160])
    except Exception as exc:                                    # noqa: BLE001
        report["errors"].append(repr(exc))
    finally:
        if sess is not None:
            sess.close()
        trace_f.close()
    report["events"] = events

    etypes = [e["type"] for e in events]
    results = [e["payload"].get("result", "") for e in events
               if e["type"] == "tool_result"]
    msg = next((e["payload"].get("text", "") for e in events
                if e["type"] == "message"), "")
    costs = [e["payload"] for e in events if e["type"] == "cost"]
    v = report["verdicts"] = {}
    v["allowlist_one_tool"] = report.get("tools_registered") == ["ping"]
    v["tool_call_made"] = EventType.TOOL_CALL.value in etypes
    v["gateway_executed_ping"] = any("pong byok-live" in r for r in results)
    v["byok_ok_text"] = "BYOK-OK" in msg
    v["turn_completed"] = any(e["type"] == "done" and
                              e["payload"].get("stop_reason") == "end_turn"
                              for e in events)
    # one COST event per API call; a tool turn makes 2+ calls and each
    # re-sends the conversation so far - SUM matches the Anthropic console
    v["tokens"] = {"in": sum(c.get("input_tokens", 0) for c in costs),
                   "out": sum(c.get("output_tokens", 0) for c in costs),
                   "api_calls": len(costs)}
    if os.path.exists(ledger_path):
        ok, n, _ = Ledger(ledger_path).verify_chain()
        v["probe_ledger_entries_added"] = n - n_before
        v["probe_ledger_chain_intact"] = ok
    else:
        v["probe_ledger_entries_added"] = 0
        v["probe_ledger_chain_intact"] = None

    with open(REPORT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2, ensure_ascii=True, default=str)

    passed = (v["tool_call_made"] and v["gateway_executed_ping"]
              and v["byok_ok_text"] and v["turn_completed"]
              and v["probe_ledger_chain_intact"] and not report["errors"])
    print("\n=== BYOK PROBE SUMMARY ===")
    for k, val in v.items():
        print(f"  {k}: {val}")
    print(f"report -> {REPORT_PATH}")
    if passed:
        print("\nALL GOOD - Minerva's frontier brain (your key, direct) is "
              "live and obeys the rulebook.")
    else:
        print("\nSomething did not check out - paste this summary to Claude.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
