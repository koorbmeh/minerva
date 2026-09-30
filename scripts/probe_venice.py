"""Guided live probe for the Venice.ai lane (adapter #3) - round 12.

Run on the Minerva host ("Minerva - Test Venice.bat"). Two stages:
  1. List the models the key can use  -> state/venice-models.txt
     (id + whether it supports tool calling, when Venice says)
  2. One tiny governed round-trip: a Venice model calls the governed `ping`
     through OUR gateway in its own sandbox state dir (state/venice-probe/),
     then replies VENICE-OK. Verifies the probe ledger hash chain.
Never touches the main ledger (own state dir; single-appender rule safe).
The key stays host-side: loaded from config/venice.env, never printed,
never written to the trace (bodies only, never headers).

Round-12 live lesson (first run, the operator at keyboard): the old pick grabbed an
e2ee-* model alphabetically and Venice answered 400 "tools is not supported
by this model". The governed lane IS tool calls, so the pick now prefers
TOOL-CAPABLE models and skips the e2ee-* lane (encrypted variants); on a
tools-rejected 400 it retries once WITHOUT tools so you learn whether the
lane works at all vs. just that model. Pin a choice with VENICE_MODEL in
config/venice.env once you know what you like (see venice-models.txt).

Plain ASCII output (cp1252 rule). Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from minerva_core.adapters.venice import (VeniceAdapter, load_venice_key,   # noqa: E402
                                          load_venice_var, default_model,
                                          API_BASE)
from minerva_core.engine import SessionConfig                               # noqa: E402
from minerva_core.events import EventType                                   # noqa: E402
from minerva_core.ledger import Ledger                                      # noqa: E402

STATE = os.path.join(ROOT, "state")
PROBE_STATE = os.path.join(STATE, "venice-probe")
MODELS_TXT = os.path.join(STATE, "venice-models.txt")
REPORT = os.path.join(STATE, "venice-report.json")
TRACE = os.path.join(STATE, "venice-trace.jsonl")

PROMPT = ("Call the ping tool once with echo='venice-probe'. After you get "
          "the result, reply with exactly: VENICE-OK")
PROMPT_NO_TOOLS = "Reply with exactly: VENICE-OK"

# liberal capability sniffing: Venice's /models metadata shape has shifted;
# any '...function...': true or '...tool...': true inside the entry counts.
_CAP_TRUE = re.compile(r'"[^"]*(function|tool)[^"]*"\s*:\s*true', re.I)
_CAP_FALSE = re.compile(r'"[^"]*(function|tool)[^"]*"\s*:\s*false', re.I)


def say(s: str) -> None:
    print(str(s).encode("ascii", "backslashreplace").decode("ascii"))


def tools_flag(entry: dict) -> bool | None:
    """True/False when the model entry declares tool support; None = unknown."""
    blob = json.dumps(entry)
    if _CAP_TRUE.search(blob):
        return True
    if _CAP_FALSE.search(blob):
        return False
    return None


def list_models(key: str) -> list[tuple[str, bool | None]]:
    req = urllib.request.Request(API_BASE + "/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    out = sorted((str(m.get("id", "")), tools_flag(m))
                 for m in data.get("data", []) if m.get("id"))
    os.makedirs(STATE, exist_ok=True)
    label = {True: "tools=yes", False: "tools=no", None: "tools=?"}
    with open(MODELS_TXT, "w", encoding="utf-8", newline="\n") as f:
        f.write("# id <TAB> tool-calling support (the governed lane needs tools)\n")
        for mid, cap in out:
            f.write(f"{mid}\t{label[cap]}\n")
    return out


def pick_model(models: list[tuple[str, bool | None]]) -> tuple[str, str]:
    """(model_id, why). Pin via VENICE_MODEL always wins. Otherwise prefer
    tool-capable, prefer 'uncensored' in the id, and SKIP e2ee-* (encrypted
    lane: different client requirements; it also rejected tools live)."""
    pinned = default_model()
    ids = [m for m, _c in models]
    if load_venice_var("VENICE_MODEL"):
        return pinned, "your VENICE_MODEL pin (config/venice.env or env)"
    if pinned in ids:
        return pinned, "your pinned/default choice"
    usable = [(m, c) for m, c in models if not m.startswith("e2ee-")]
    for m, c in usable:
        if c is True and "uncensored" in m:
            return m, "tool-capable + uncensored"
    for m, c in usable:
        if c is True:
            return m, "tool-capable (no uncensored one declared tools)"
    for m, c in usable:
        if "uncensored" in m and c is None:
            return m, "uncensored, tool support unknown - we'll find out"
    if usable:
        return usable[0][0], "first non-e2ee model (nothing declared tools)"
    return pinned, "nothing usable listed; trying the default anyway"


def run_session(adapter: VeniceAdapter, model: str, with_tools: bool) -> dict:
    cfg = SessionConfig(persona="You are Minerva. Be brief.", model=model,
                        tools=("ping",))
    events = []
    t0 = time.time()
    sess = adapter.create_session(cfg)
    if not with_tools:
        sess.tools = []                      # body omits tools entirely
    try:
        for ev in sess.send(PROMPT if with_tools else PROMPT_NO_TOOLS):
            events.append(ev)
    finally:
        sess.close()
    final = next((str(e.payload.get("text", "")) for e in reversed(events)
                  if e.type is EventType.MESSAGE), "")
    return {
        "duration_s": round(time.time() - t0, 2),
        "tool_called": any(e.type is EventType.TOOL_CALL
                           and e.payload.get("tool") == "ping" for e in events),
        "pong": any(e.type is EventType.TOOL_RESULT
                    and "pong" in str(e.payload.get("result", "")) for e in events),
        "final_text": final,
        "errors": [str(e.payload.get("message", "")) for e in events
                   if e.type is EventType.ERROR],
    }


def main() -> int:
    key = load_venice_key()
    if not key:
        say("No Venice key yet.")
        say("Open config\\venice.env in Notepad and paste your key after")
        say("VENICE_API_KEY=   (get it from Venice.ai -> Settings -> API keys),")
        say("save, then run this again. Don't paste the key anywhere else.")
        return 0

    say("Step 1/2: asking Venice which models your key can use...")
    try:
        models = list_models(key)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError) as e:
        say(f"  Could not list models: {e}")
        say("  (Check the key and your internet connection, then retry.)")
        return 1
    n_tools = sum(1 for _m, c in models if c is True)
    say(f"  {len(models)} models available ({n_tools} declare tool-calling) - "
        "full list: state\\venice-models.txt")
    model, why = pick_model(models)
    say(f"  Using '{model}' ({why}).")
    say("  To pick a different one later: set VENICE_MODEL in config\\venice.env.")

    say(f"Step 2/2: one tiny governed test chat on '{model}'...")
    os.makedirs(PROBE_STATE, exist_ok=True)
    frames: list[dict] = []

    def wire(direction: str, frame: dict) -> None:
        frames.append({"dir": direction, "frame": frame})

    adapter = VeniceAdapter(state_dir=PROBE_STATE, model=model, wire_log=wire)
    r = run_session(adapter, model, with_tools=True)
    no_tools_note = ""
    if r["errors"] and any("tool" in e.lower() and "400" not in e
                           or "400" in e for e in r["errors"]):
        # 400 with tools attached: is it the LANE or just this model?
        say("  That model rejected the request - checking whether the lane "
            "itself works (plain chat, no tools)...")
        r2 = run_session(adapter, model, with_tools=False)
        if not r2["errors"] and "VENICE-OK" in r2["final_text"]:
            no_tools_note = (f"model '{model}' chats fine but does NOT support "
                             "tool calling - the governed lane needs a "
                             "tools=yes model (see venice-models.txt), set it "
                             "as VENICE_MODEL in config\\venice.env")
        else:
            no_tools_note = "plain chat failed too - see errors"
            r["errors"] += [e for e in r2["errors"] if e not in r["errors"]]
    elif not r["tool_called"] and not r["errors"]:
        # the round-13 live case: gemma-4-uncensored declared tools, answered
        # VENICE-OK, and never called the governed ping - a quiet failure.
        no_tools_note = (f"model '{model}' answered but NEVER CALLED the "
                         "governed ping tool - it ignores tools instead of "
                         "using them. Pick a tools=yes model from "
                         "venice-models.txt and pin it as VENICE_MODEL in "
                         "config\\venice.env")

    ok_chain, n, _ = Ledger(os.path.join(PROBE_STATE, "ledger.jsonl")).verify_chain()
    with open(TRACE, "w", encoding="utf-8", newline="\n") as f:
        for fr in frames:
            f.write(json.dumps(fr, ensure_ascii=True) + "\n")
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model,
              "model_pick_reason": why, "duration_s": r["duration_s"],
              "tool_called": r["tool_called"], "pong": r["pong"],
              "final_text": r["final_text"][:200], "errors": r["errors"][:3],
              "no_tools_diagnosis": no_tools_note,
              "probe_ledger_entries": n, "probe_chain_intact": ok_chain,
              "ok": bool(r["tool_called"] and r["pong"] and ok_chain
                         and not r["errors"])}
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2)

    say("")
    if report["ok"]:
        say("VENICE LANE WORKS:")
        say("  - the model called the governed ping tool (gateway answered 'pong')")
        say(f"  - probe ledger: {n} entries, hash chain INTACT")
        say(f"  - her reply: {r['final_text'][:120]}")
        say(f"  - took {r['duration_s']}s. To make this the standing pick: set")
        say(f"    VENICE_MODEL={model} in config\\venice.env")
        return 0
    say("Summary:")
    say(f"  tool called: {r['tool_called']} / pong seen: {r['pong']} / "
        f"chain intact: {ok_chain}")
    if no_tools_note:
        say(f"  diagnosis: {no_tools_note}")
    for e in r["errors"][:3]:
        say(f"  error: {e[:160]}")
    say("  Full detail: state\\venice-report.json + state\\venice-trace.jsonl")
    return 1


if __name__ == "__main__":
    sys.exit(main())
