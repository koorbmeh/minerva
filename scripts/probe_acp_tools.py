r"""Live probe #2: a GOVERNED TOOL CALL through ACP — captures the unobserved
tool_call update kinds and proves client-supplied mcpServers[] + per-session
tool allowlists in one run.

Run ON THE HOST:  python scripts\probe_acp_tools.py
Writes: state/acp-tools-trace.jsonl   (every frame incl. stderr, timestamped)
        state/acp-tools-report.json   (verdicts)
        state/acp-probe/              (the probe gateway's OWN state dir)

This probe dogfoods adapters/hermes_acp.py rather than re-speaking the
protocol by hand — if the adapter mistranslates the live frames, the report
shows it (raw trace vs translated events, side by side).

Design notes:
* The gateway rides in session/new mcpServers[] under the name "minerva2" so
  it is distinguishable from the GLOBAL 'minerva' registration Hermes loads
  from its own config (stderr shows both; the prompt targets minerva2's ping).
* minerva2 gets an ISOLATED state dir (state/acp-probe). Two gateway processes
  must never share a hash-chained ledger file — concurrent appenders fork the
  chain. The main state/ledger.jsonl is untouched by this probe.
* The allowlist is exercised live: cfg.tools=("ping",) -> MINERVA_TOOLS env ->
  minerva2 should register exactly 1 tool (global minerva registers 4).
* Belt and suspenders on gateway config: explicit --state-dir/--tools args AND
  env twins ({name,value} list form). __main__.py: CLI flag wins; `hermes mcp
  add`'s dashed-token bug was a CLI parser issue, JSON args may pass dashes —
  this probe is also the test of that.
* cfg.model pins the current :free model id — a no-op switch that verifies
  whether session/set_model exists without risking paid routing.

Stdlib only. One prompt, a few free-tier tokens.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from minerva_core.adapters.hermes_acp import HermesAcpAdapter   # noqa: E402
from minerva_core.engine import SessionConfig                   # noqa: E402
from minerva_core.events import EventType                       # noqa: E402
from minerva_core.ledger import Ledger                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
PROBE_STATE = os.path.join(STATE, "acp-probe")
TRACE_PATH = os.path.join(STATE, "acp-tools-trace.jsonl")
REPORT_PATH = os.path.join(STATE, "acp-tools-report.json")

# update kinds already captured in state/acp-trace.jsonl (round 6 probe)
KNOWN_KINDS = {"agent_message_chunk", "agent_thought_chunk", "usage_update",
               "available_commands_update"}

PROMPT = ("Call the tool mcp_minerva2_ping once with no arguments. "
          "After it returns, reply with exactly: GOVERNED-OK")


def main() -> int:
    os.makedirs(PROBE_STATE, exist_ok=True)
    trace_f = open(TRACE_PATH, "w", encoding="utf-8", newline="\n")
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "errors": [], "verdicts": {}}

    def wire_log(direction: str, frame: dict) -> None:
        trace_f.write(json.dumps({"t": round(time.time(), 3), "dir": direction,
                                  "msg": frame}, ensure_ascii=False) + "\n")
        trace_f.flush()

    gateway = {
        "name": "minerva2",
        "command": sys.executable,
        "args": [os.path.join(ROOT, "scripts", "run_gateway.py"),
                 "--state-dir", PROBE_STATE],
        "env": [{"name": "MINERVA_STATE_DIR", "value": PROBE_STATE}],
    }
    probe_ledger_path = os.path.join(PROBE_STATE, "ledger.jsonl")
    n_ledger_before = 0
    if os.path.exists(probe_ledger_path):
        with open(probe_ledger_path, encoding="utf-8") as f:
            n_ledger_before = sum(1 for line in f if line.strip())

    # MINERVA_PROBE_CMD overrides the engine command (rehearsal against the
    # fake agent: set it to  <python> tests/test_adapter_hermes_acp.py --fake-agent)
    override = os.environ.get("MINERVA_PROBE_CMD")
    adapter = HermesAcpAdapter(
        hermes_cmd=override.split("|") if override else None,
        mcp_servers=[gateway], timeout=240.0, wire_log=wire_log)
    events = []
    sess = None
    try:
        cfg = SessionConfig(model="nous:stepfun/step-3.7-flash:free",
                            tools=("ping",))
        t0 = time.time()
        sess = adapter.create_session(cfg)
        report["session_id"] = sess.id
        report["agent_info"] = adapter.agent_info
        report["provenance"] = sess.provenance
        report["set_model_error"] = sess.model_error      # None => method exists
        report["session_new_latency_s"] = round(time.time() - t0, 2)

        for ev in sess.send(PROMPT):
            events.append(ev.to_dict())
            print(f"  [{ev.type.value}] "
                  + json.dumps(ev.payload, ensure_ascii=True)[:160])
        report["unknown_update_kinds_seen_by_adapter"] = sorted(sess.unknown_kinds)
    except Exception as exc:                              # noqa: BLE001
        report["errors"].append(repr(exc))
    finally:
        if sess is not None:
            sess.close()
        if adapter._client is not None:
            report["stderr_tail"] = list(adapter._client.stderr_tail)
            adapter._client.close()
        trace_f.close()

    report["events"] = events

    # ---- mine the raw trace for the update-kind taxonomy -------------------
    kinds: dict[str, int] = {}
    tool_frames: list[dict] = []
    with open(TRACE_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("msg") or {}
            if rec.get("dir") == "recv" and msg.get("method") == "session/update":
                upd = (msg.get("params") or {}).get("update") or {}
                k = upd.get("sessionUpdate", "?")
                kinds[k] = kinds.get(k, 0) + 1
                if "tool" in k:
                    tool_frames.append(upd)
    report["update_kinds"] = kinds
    report["new_kinds_vs_round6"] = sorted(set(kinds) - KNOWN_KINDS)
    report["tool_call_frames_raw"] = tool_frames          # finalize _translate on THESE

    # ---- verdicts ----------------------------------------------------------
    etypes = [e["type"] for e in events]
    v = report["verdicts"]
    v["per_session_mcp_server_registered"] = any(
        "minerva2" in s for s in report.get("stderr_tail", []))
    v["allowlist_one_tool"] = any(
        "minerva2" in s and "1 tool" in s for s in report.get("stderr_tail", []))
    v["tool_call_translated"] = EventType.TOOL_CALL.value in etypes
    v["tool_result_translated"] = EventType.TOOL_RESULT.value in etypes
    v["turn_completed"] = EventType.DONE.value in etypes
    v["governed_ok_text"] = any(e["type"] == "message" and
                                "GOVERNED-OK" in e["payload"].get("text", "")
                                for e in events)
    v["set_model_supported"] = report.get("set_model_error") is None
    if os.path.exists(probe_ledger_path):
        led = Ledger(probe_ledger_path)
        ok, n, bad = led.verify_chain()
        v["probe_ledger_entries_added"] = n - n_ledger_before
        v["probe_ledger_chain_intact"] = ok
    else:
        v["probe_ledger_entries_added"] = 0
        v["probe_ledger_chain_intact"] = None

    with open(REPORT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print("\n=== ACP GOVERNED-TOOL PROBE SUMMARY ===")
    for k, val in v.items():
        print(f"  {k}: {val}")
    print(f"  update kinds seen: {kinds}")
    print(f"  new kinds to fold into _translate: {report['new_kinds_vs_round6']}")
    print(f"frames -> {TRACE_PATH}\nreport -> {REPORT_PATH}")
    print(f"errors: {len(report['errors'])}")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
