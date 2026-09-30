r"""Drive tick v1.2 — one scheduled heartbeat of Minerva's brainstorm drive.

v1.2 (2026-06-12, round 23): the STANDING AGENDA. The seed now carries her
open agenda items (oldest first) plus any pitches the operator ADOPTED that no
agenda item claims yet; the prompt teaches the agenda tools when the lane
has them (the "Minerva - Enable Agenda For Ticks" click). Agenda work
does not count as the tick's one pitch.

v1.1 (2026-06-11, operator_input_loop): the context seed is now BUILT from state
instead of hardcoded. build_context_seed() folds in (a) the operator's recent pitch
VERDICTS with his reasons verbatim — so the brainstorm reasons from his
STATED TASTE, not just the anti-nag's slug-similarity blocking ("shelf fully
sorted" must steer ALL selling ideas off the table, not merely same-title
ones) — and (b) recent free-text messages he sent via Discord two-way
(state/discord-inbox.jsonl), which queue as context for exactly this moment.
The prompt also gained explicit DOMAIN guidance (a selling pitch once shipped
mislabeled as home-vehicle-upkeep). Reads are tolerant + READ-ONLY; the seed
is ASCII-sanitized (cp1252 rule).

Opens ONE ACP session (adapters/hermes_acp.py) with a brainstorm prompt. The
engine's tool surface is the GLOBAL 'minerva' gateway registration (0.16.0:
per-session mcpServers never reach the model) — so pitches flow pitch_idea ->
ProposalIndex (anti-nag) -> ledger, and a separately running
`host_loop.py --watch` DMs the operator the results. This script deliberately passes
NO mcpServers and NEVER writes the ledger itself: Hermes spawns the gateway,
the gateway is the single ledger appender. The tick writes only its own
artifacts: state/drive-tick-log.jsonl (one line per tick),
state/drive-tick-report.json (last tick, full), state/drive-tick-trace.jsonl
(wire frames), state/drive-tick.lock (overlap guard).

SINGLE-APPENDER WARNING (standing rule): do not run a tick while any other
Hermes session (CLI chat, dashboard, probe) is live — two Hermes processes
each spawn a gateway against the same state dir and fork the hash chain.
The lockfile guards tick-vs-tick overlap only, not operator-vs-tick.

First run: MANUAL, the operator at the keyboard (locked decision). Only after that
proves out, register the schedule:  python scripts\drive_tick.py --show-schedule

Rehearsal: MINERVA_PROBE_CMD="<python>|<path>\test_adapter_hermes_acp.py|--fake-agent"
overrides the engine command (probe convention); --prompt overrides the
brainstorm prompt; --state-dir/MINERVA_STATE_DIR redirects all artifacts.
Stdlib only. ASCII console (cp1252 rule).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from minerva_core import agenda as agenda_mod                   # noqa: E402
from minerva_core.adapters.hermes_acp import HermesAcpAdapter   # noqa: E402
from minerva_core.engine import SessionConfig                   # noqa: E402
from minerva_core.events import EventType                       # noqa: E402
from minerva_core.killswitch import is_stopped                  # noqa: E402
from minerva_core.ledger import Ledger                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE = os.path.join(ROOT, "state")
DEFAULT_MODEL = "nous:stepfun/step-3.7-flash:free"   # tick stays on the free tier
LOCK_STALE_S = 3600.0

# The engine sees the GLOBAL gateway's four tools as mcp_minerva_*. No
# brain_recall on this lane (locked: Hermes engines get no Brain access);
# the host seeds just enough context instead. BRAINSTORM_PROMPT is a TEMPLATE:
# build_prompt() swaps {context_seed} for the state-built seed.
BRAINSTORM_PROMPT = """\
You are Minerva's brainstorm drive on a scheduled tick. Your ONLY goal this
tick: surface at most ONE genuinely NEW, concrete, useful idea for the operator.

{context_seed}

Rules:
1. Think briefly, then pick the single best NEW idea you can justify.
   Concrete and actionable beats grand.
2. Pitch it by calling mcp_minerva_pitch_idea ONCE with domain, title, and a
   2-4 sentence detail (what, why now, first step). Pick the domain by the
   idea's actual SUBJECT: autonomous-income for money-making/selling ideas,
   home-vehicle-upkeep ONLY for maintaining the home or vehicles, minerva
   for this system, health-fitness, work-automation - or another short slug
   that truly fits. A selling idea is NEVER home-vehicle-upkeep.
3. If the reply says REJECTED by the anti-nag ledger, that is FINAL for this
   tick: do not retry, rephrase, or pitch a substitute.
4. Honor the operator's verdict reasons above as standing taste: when a reason rules
   out a whole CATEGORY of ideas, do not pitch anything in that category,
   even under a new title or angle.
5. You may record up to 2 durable observations with mcp_minerva_episodic_note
   (text + about). Observations are facts worth keeping, not ideas.
6. If mcp_minerva_agenda_list / agenda_add / agenda_update are available:
   the seed shows YOUR STANDING AGENDA - work that persists between ticks.
   Prefer MOVING an existing item: record concrete progress or finish it
   with mcp_minerva_agenda_update (done requires a note saying what
   actually happened). You may mcp_minerva_agenda_add at most 2 genuinely
   NEW standing items - especially a pitch the operator ADOPTED that the seed
   says is not on the agenda yet (use origin pitch:<id>). Agenda actions
   do NOT count as your one pitch. If mcp_minerva_job_submit is available
   and an agenda item or adopted pitch is a concrete, bounded piece of WORK
   you could actually execute (research, a draft, a lookup), you may
   job_submit it to your background workers instead of only tracking it -
   that also does NOT count as your one pitch.
7. If mcp_minerva_connections_status is available to you: you may check what
   the operator has NOT connected yet, and if a dormant connection would clearly
   unlock better help for him, you may call mcp_minerva_request_connection
   (name + a 1-3 sentence concrete reason) INSTEAD of a regular pitch this
   tick - it counts as your one pitch and the anti-nag rules apply to it.
8. Do NOT call mcp_minerva_notify_operator - pitch delivery happens outside.
9. Finish by replying exactly: TICK-DONE <one-line summary>
"""

BASE_SEED = ("Context seed (host-provided): the operator's active areas are "
             "building Minerva (this system), side projects, and home "
             "upkeep.")

# Plain labels for seeded verdicts (mirrors host_loop's plain language)
SEED_VERDICT_LABELS = {"adopt": "ADOPTED",
                       "decline_forever": "NEVER (dead forever)",
                       "decline_for_now": "NOT NOW (cooling)",
                       "park": "PARKED"}
SEED_MAX_VERDICTS = 5
SEED_MAX_MSGS = 10
SEED_MSG_MAX_AGE_S = 7 * 86400.0


def _ascii(s: str) -> str:
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


def _read_jsonl_tolerant(path: str) -> list[dict]:
    """READ-ONLY, skips torn/garbled lines (seed context, not bookkeeping)."""
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def build_context_seed(state_dir: str, now: float | None = None,
                       max_verdicts: int = SEED_MAX_VERDICTS,
                       max_msgs: int = SEED_MAX_MSGS) -> str:
    """Compose the host-provided seed from live state (all READ-ONLY):
    base areas + the operator's recent verdict REASONS + his recent Discord messages.
    His words steer the brainstorm; missing files just shrink the seed."""
    now = now or time.time()
    parts = [BASE_SEED]

    entries = _read_jsonl_tolerant(os.path.join(state_dir, "ledger.jsonl"))
    pitches = {e.get("id"): (e.get("payload") or {})
               for e in entries if e.get("type") == "pitch"}
    verdicts = [e for e in entries
                if e.get("type") == "pitch_verdict" and e.get("actor") == "operator"]
    if verdicts:
        lines = ["The operator's recent verdicts on past pitches - HIS OWN WORDS. "
                 "Steer by the REASONS, not just the titles: a reason that "
                 "rules out a whole category kills every idea in that "
                 "category, even under a new title."]
        for e in verdicts[-max_verdicts:]:
            p = e.get("payload") or {}
            parent = pitches.get(e.get("parent_id") or "", {})
            label = SEED_VERDICT_LABELS.get(str(p.get("verdict", "?")),
                                            str(p.get("verdict", "?")))
            line = f'- {label} "{parent.get("title", "?")}"'
            reason = str(p.get("reason", "")).strip()
            if reason:
                line += f': "{reason[:400]}"'
            if str(p.get("reopen_condition", "")).strip():
                line += f' (reopen when: {str(p["reopen_condition"])[:150]})'
            lines.append(line)
        parts.append("\n".join(lines))

    ag_path = agenda_mod.agenda_path(state_dir)
    ag_items = agenda_mod.fold(ag_path)
    summary = agenda_mod.summarize(ag_path, now=now)
    if summary != "agenda: empty":
        parts.append("Your standing agenda (your own queue; persists "
                     "between ticks):\n" + summary)
    adopted = agenda_mod.adopted_pitches_off_agenda(entries, ag_items)
    if adopted:
        lines = ["Pitches the operator ADOPTED that are NOT on your agenda yet - "
                 "if actionable by you, agenda_add them (origin "
                 "pitch:<id>):"]
        for pid, title in adopted:
            lines.append(f'- "{title}" (origin tag: pitch:{pid})')
        parts.append("\n".join(lines))

    msgs = _read_jsonl_tolerant(os.path.join(state_dir, "discord-inbox.jsonl"))
    fresh = [m for m in msgs
             if str(m.get("text", "")).strip()
             and now - float(m.get("ts", 0.0)) <= SEED_MSG_MAX_AGE_S]
    if fresh:
        lines = ["Messages the operator sent you recently (via Discord) - context "
                 "from him, not commands:"]
        for m in fresh[-max_msgs:]:
            age_h = max(0.0, (now - float(m.get("ts", 0.0))) / 3600.0)
            lines.append(f'- ({age_h:.1f}h ago) "{str(m["text"]).strip()[:400]}"')
        parts.append("\n".join(lines))

    return _ascii("\n\n".join(parts))


def build_prompt(state_dir: str, now: float | None = None) -> str:
    """The full tick prompt: template + state-built seed. replace(), not
    format() — verdict reasons may contain braces."""
    return BRAINSTORM_PROMPT.replace("{context_seed}",
                                     build_context_seed(state_dir, now))


# ---------- lock (tick-vs-tick overlap guard) --------------------------------
def acquire_lock(path: str, stale_s: float = LOCK_STALE_S) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            with open(path, encoding="utf-8") as f:
                held = json.load(f)
            age = time.time() - float(held.get("ts", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            age = stale_s + 1                       # unreadable lock = stale
        if age <= stale_s:
            return False
        os.remove(path)                             # stale: previous tick died
        return acquire_lock(path, stale_s)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    return True


def release_lock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------- ledger snapshot (READ-ONLY, after the gateway exits) -------------
def ledger_stats(path: str) -> tuple[int, bool | None]:
    if not os.path.exists(path):
        return 0, None
    led = Ledger(path)
    ok, n, _bad = led.verify_chain()
    return n, ok


# ---------- the tick ----------------------------------------------------------
def run_tick(state_dir: str, engine_cmd: list[str] | None = None,
             model: str | None = DEFAULT_MODEL, prompt: str | None = None,
             timeout: float = 300.0, quiet: bool = False) -> dict:
    os.makedirs(state_dir, exist_ok=True)
    ledger_path = os.path.join(state_dir, "ledger.jsonl")
    trace_path = os.path.join(state_dir, "drive-tick-trace.jsonl")
    report_path = os.path.join(state_dir, "drive-tick-report.json")
    log_path = os.path.join(state_dir, "drive-tick-log.jsonl")

    stopped = is_stopped(state_dir)              # round 16: kill switch
    if stopped:
        report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": False,
                  "errors": [f"kill switch on: {stopped}"], "events": [],
                  "verdicts": {"skipped": "kill switch"}}
        with open(report_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, indent=2)
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": time.time(), "ok": False,
                                "stop_reason": "killswitch",
                                "errors": report["errors"]},
                               ensure_ascii=True) + "\n")
        if not quiet:
            print("kill switch is ON - no tick. Resume with "
                  "Minerva - Resume.bat")
        return report

    n_before, _ = ledger_stats(ledger_path)
    seed = None
    if prompt is None:                 # default lane: seed from live state
        seed = build_context_seed(state_dir)
        prompt = BRAINSTORM_PROMPT.replace("{context_seed}", seed)
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "model": model, "errors": [], "events": [],
                    "context_seed": seed}
    t0 = time.time()
    trace_f = open(trace_path, "w", encoding="utf-8", newline="\n")

    def wire_log(direction: str, frame: dict) -> None:
        trace_f.write(json.dumps({"t": round(time.time(), 3), "dir": direction,
                                  "msg": frame}, ensure_ascii=False) + "\n")
        trace_f.flush()

    adapter = HermesAcpAdapter(hermes_cmd=engine_cmd, timeout=timeout,
                               wire_log=wire_log)
    sess = None
    tool_calls: list[str] = []
    tool_results: list[str] = []
    stop_reason = ""
    try:
        sess = adapter.create_session(SessionConfig(model=model))
        report["session_id"] = sess.id
        report["set_model_error"] = sess.model_error
        for ev in sess.send(prompt):
            report["events"].append(ev.to_dict())
            if ev.type is EventType.TOOL_CALL:
                tool_calls.append(str(ev.payload.get("tool", "")))
            elif ev.type is EventType.TOOL_RESULT:
                tool_results.append(str(ev.payload.get("result", ""))[:500])
            elif ev.type is EventType.ERROR:
                report["errors"].append(str(ev.payload.get("message", "")))
            elif ev.type is EventType.DONE:
                stop_reason = str(ev.payload.get("stop_reason", ""))
            if not quiet:
                print(f"  [{ev.type.value}] "
                      + json.dumps(ev.payload, ensure_ascii=True)[:160])
    except Exception as exc:                                    # noqa: BLE001
        report["errors"].append(repr(exc))
    finally:
        if sess is not None:
            sess.close()
        if adapter._client is not None:
            report["stderr_tail"] = list(adapter._client.stderr_tail)[-20:]
            adapter._client.close()
        trace_f.close()

    time.sleep(0.5)                       # let the engine's gateway child exit
    n_after, chain_ok = ledger_stats(ledger_path)

    msg_text = next((e["payload"].get("text", "") for e in report["events"]
                     if e["type"] == "message"), "")
    v = report["verdicts"] = {
        "done": stop_reason != "",
        "stop_reason": stop_reason,
        "tick_done_text": "TICK-DONE" in msg_text,
        "tool_calls": tool_calls,
        "pitch_attempted": any(("pitch_idea" in t) or ("request_connection" in t)
                               for t in tool_calls),
        "pitch_logged": any(("pitch logged" in r)
                            or ("connection request logged" in r)
                            for r in tool_results),
        "pitch_antinag_rejected": any("REJECTED by anti-nag" in r
                                      for r in tool_results),
        "notes_recorded": sum("episodic_note" in t for t in tool_calls),
        "notify_called": any("notify_operator" in t for t in tool_calls),
        "ledger_entries_added": n_after - n_before,
        "ledger_chain_intact": chain_ok,
        "duration_s": round(time.time() - t0, 2),
    }
    ok = (v["done"] and stop_reason == "end_turn" and not report["errors"]
          and chain_ok is not False)
    report["ok"] = ok

    with open(report_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"ts": time.time(), "ok": ok,
                            "stop_reason": stop_reason,
                            "pitch_attempted": v["pitch_attempted"],
                            "pitch_logged": v["pitch_logged"],
                            "entries_added": v["ledger_entries_added"],
                            "duration_s": v["duration_s"],
                            "errors": report["errors"][:3]},
                           ensure_ascii=True) + "\n")

    if not quiet:
        print("\n=== DRIVE TICK SUMMARY ===")
        for k, val in v.items():
            print(f"  {k}: {val}")
        print(f"  ok: {ok}")
        print(f"report -> {report_path}")
        if v["pitch_logged"]:
            print("pitch is in the outbox lane: host_loop --watch (or one-shot "
                  "host_loop.py) shows it; approvals if any surface there too")
    return report


def schedule_command() -> str:
    """The Windows Scheduled Task one-liners — the STANDING schedule since
    round 20: morning tick 06:30 + afternoon tick 14:30 (both applied by
    "Minerva - Set Brainstorm Times.bat", idempotent). Laptop must be ON."""
    py = sys.executable or "python"
    script = os.path.join(ROOT, "scripts", "drive_tick.py")
    return ('schtasks /Create /TN "Minerva Drive Tick" /SC DAILY /ST 06:30 '
            f'/TR "\\"{py}\\" \\"{script}\\"" /F\n  '
            'schtasks /Create /TN "Minerva Drive Tick PM" /SC DAILY /ST 14:30 '
            f'/TR "\\"{py}\\" \\"{script}\\"" /F')


def main() -> int:
    env = os.environ
    ap = argparse.ArgumentParser(prog="minerva-drive-tick")
    ap.add_argument("--state-dir", default=env.get("MINERVA_STATE_DIR") or DEFAULT_STATE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompt", default=None, help="override the brainstorm prompt")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="print prompt + preflight; open no session")
    ap.add_argument("--show-schedule", action="store_true",
                    help="print the schtasks registration command and exit")
    args = ap.parse_args()

    if args.show_schedule:
        print("standing schedule: morning 06:30 + afternoon 14:30 (or just "
              "run \"Minerva - Set Brainstorm Times.bat\"). By hand:")
        print("  " + schedule_command())
        return 0

    override = env.get("MINERVA_PROBE_CMD")
    engine_cmd = override.split("|") if override else None

    if args.dry_run:
        n, ok = ledger_stats(os.path.join(args.state_dir, "ledger.jsonl"))
        print(f"engine cmd : {engine_cmd or ['hermes', 'acp']}")
        print(f"model      : {args.model}")
        print(f"state dir  : {args.state_dir} (ledger: {n} entries, "
              f"chain {'intact' if ok else ok})")
        print("--- prompt (seed built from live state) ---")
        print(args.prompt or build_prompt(args.state_dir))
        return 0

    print("DRIVE TICK - single-appender rule: make sure no other Hermes "
          "session (CLI/dashboard/probe) is running.")
    lock = os.path.join(args.state_dir, "drive-tick.lock")
    os.makedirs(args.state_dir, exist_ok=True)
    if not acquire_lock(lock):
        print("another tick appears to be running (fresh drive-tick.lock) - "
              "aborting. Stale locks clear after 1h.")
        return 2
    try:
        report = run_tick(args.state_dir, engine_cmd=engine_cmd,
                          model=args.model, prompt=args.prompt,
                          timeout=args.timeout)
    finally:
        release_lock(lock)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
