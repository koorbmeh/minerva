"""Drive tick — lock, prompt invariants, rehearsal tick against the fake agent.
Run: python tests/test_drive_tick.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import drive_tick  # noqa: E402
from minerva_core.ledger import Ledger, NOTE, PITCH  # noqa: E402

FAKE_CMD = [sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "test_adapter_hermes_acp.py"),
            "--fake-agent"]

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def read_log(state: str) -> list[dict]:
    path = os.path.join(state, "drive-tick-log.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def main() -> None:
    # ---- prompt invariants (the anti-nag contract rides in the prompt) ----
    p = drive_tick.BRAINSTORM_PROMPT
    check("prompt names the pitch tool", "mcp_minerva_pitch_idea" in p)
    check("prompt teaches anti-nag finality", "REJECTED" in p and "FINAL" in p)
    check("prompt allows episodic notes", "mcp_minerva_episodic_note" in p)
    check("prompt forbids notify", "Do NOT call mcp_minerva_notify_operator" in p)
    check("prompt demands TICK-DONE", "TICK-DONE" in p)
    check("prompt is ASCII-safe", p.encode("ascii", "strict") is not None)
    check("one pitch per tick", "ONCE" in p and "ONE" in p)
    check("template carries the seed slot", "{context_seed}" in p)
    check("domain guidance present (v1.1)",
          "home-vehicle-upkeep ONLY" in p
          and "NEVER home-vehicle-upkeep" in p)
    check("taste-generalization rule present (v1.1)", "whole CATEGORY" in p)

    # ---- context seed v1.1: verdict reasons + discord messages --------------
    with tempfile.TemporaryDirectory() as td:
        led = Ledger(os.path.join(td, "ledger.jsonl"))
        titles = [f"Idea number {i} about topic {i}" for i in range(7)]
        for i, t in enumerate(titles):
            pe = led.append(PITCH, {"domain": "testing", "title": t,
                                    "slug": f"idea-{i}"})
            reason = (f"Reason {i}: shelf fully sorted, nothing left to sell."
                      + (" café thought" if i == 5 else ""))
            led.append("pitch_verdict",
                       {"verdict": "decline_forever", "reason": reason},
                       parent_id=pe["id"], actor="operator")
        now = time.time()
        with open(os.path.join(td, "discord-inbox.jsonl"), "a",
                  encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": now - 120, "msg_id": "1",
                                "text": "the bike swap is Saturday"}) + "\n")
            f.write(json.dumps({"ts": now - 30 * 86400, "msg_id": "2",
                                "text": "ancient history message"}) + "\n")
            f.write(json.dumps({"ts": now - 60, "msg_id": "3",
                                "text": ""}) + "\n")
        seed = drive_tick.build_context_seed(td, now=now)
        check("seed keeps the base areas", "active areas" in seed)
        check("seed carries the operator's verdict reasons",
              "NEVER (dead forever)" in seed and "shelf fully sorted" in seed)
        check("seed caps verdicts to the most recent",
              titles[6] in seed and titles[2] in seed
              and titles[0] not in seed and titles[1] not in seed)
        check("seed teaches reason-generalization", "whole category" in seed)
        check("seed carries recent discord messages",
              "bike swap is Saturday" in seed)
        check("seed drops stale + empty messages",
              "ancient history" not in seed)
        check("seed is ASCII-safe even with unicode reasons",
              seed.encode("ascii", "strict") is not None)
        prompt = drive_tick.build_prompt(td, now=now)
        check("built prompt embeds the seed, no slot left",
              "{context_seed}" not in prompt and "bike swap" in prompt
              and "TICK-DONE" in prompt)
    with tempfile.TemporaryDirectory() as td:
        seed = drive_tick.build_context_seed(td)
        check("empty state -> base seed only",
              seed.startswith("Context seed") and "verdicts" not in seed)

    # ---- schedule command (printed, never auto-registered) -----------------
    sc = drive_tick.schedule_command()
    check("schtasks create line", sc.startswith("schtasks /Create"))
    check("schedule points at drive_tick.py", "drive_tick.py" in sc)

    # ---- lock semantics -----------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        lock = os.path.join(td, "drive-tick.lock")
        check("lock acquired", drive_tick.acquire_lock(lock))
        check("second acquire refused", not drive_tick.acquire_lock(lock))
        drive_tick.release_lock(lock)
        check("release allows reacquire", drive_tick.acquire_lock(lock))
        # stale lock: pretend the holder died an age ago
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 1, "ts": time.time() - 7200}, f)
        check("stale lock displaced", drive_tick.acquire_lock(lock))
        drive_tick.release_lock(lock)
        drive_tick.release_lock(lock)          # double release harmless
        check("double release harmless", True)

    # ---- rehearsal tick: fake agent, tool-call turn -------------------------
    with tempfile.TemporaryDirectory() as td:
        # pre-existing ledger must come through BYTE-IDENTICAL (read-only rule)
        led = Ledger(os.path.join(td, "ledger.jsonl"))
        led.append(NOTE, {"text": "pre-existing entry", "about": "",
                          "source": "engine"})
        bytes_before = open(os.path.join(td, "ledger.jsonl"), "rb").read()

        rep = drive_tick.run_tick(td, engine_cmd=FAKE_CMD, prompt="hello",
                                  timeout=30.0, quiet=True)
        v = rep["verdicts"]
        check("tick ok", rep["ok"] is True)
        check("turn completed end_turn", v["stop_reason"] == "end_turn")
        check("tool call observed", "mcp_minerva_ping" in v["tool_calls"])
        check("no pitch in this rehearsal", v["pitch_attempted"] is False)
        check("notify not called", v["notify_called"] is False)
        check("message assembled", any(e["type"] == "message" and
                                       "Hello there" in e["payload"]["text"]
                                       for e in rep["events"]))
        check("ledger untouched by tick",
              open(os.path.join(td, "ledger.jsonl"), "rb").read() == bytes_before)
        check("ledger delta zero", v["ledger_entries_added"] == 0)
        check("chain verified intact", v["ledger_chain_intact"] is True)
        check("report file written",
              os.path.exists(os.path.join(td, "drive-tick-report.json")))
        check("trace file written",
              os.path.exists(os.path.join(td, "drive-tick-trace.jsonl")))
        log = read_log(td)
        check("tick log one line", len(log) == 1 and log[0]["ok"] is True)

        # second tick appends (history accumulates, report overwrites)
        rep2 = drive_tick.run_tick(td, engine_cmd=FAKE_CMD, prompt="hello",
                                   timeout=30.0, quiet=True)
        check("second tick ok", rep2["ok"] is True)
        check("tick log accumulates", len(read_log(td)) == 2)

    # ---- engine death mid-turn -> ok False, error logged ---------------------
    with tempfile.TemporaryDirectory() as td:
        rep = drive_tick.run_tick(td, engine_cmd=FAKE_CMD, prompt="die",
                                  timeout=30.0, quiet=True)
        check("dead engine -> not ok", rep["ok"] is False)
        check("error recorded", len(rep["errors"]) >= 1)
        log = read_log(td)
        check("failure logged", len(log) == 1 and log[0]["ok"] is False)

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
