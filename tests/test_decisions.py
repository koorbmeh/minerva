"""Chat decision tools (round 27): Minerva records the operator's verdicts/approvals
when he states them in conversation. Store helpers, the three governed tools
(record + fold immediately, resolve by id/title/ref, provenance), a real-gateway
round trip (pitch_idea -> decisions_pending -> record_verdict -> resolved, chain
intact), and the chat-lane wiring.   python tests/test_decisions.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from minerva_core.ledger import Ledger, ProposalIndex, PITCH      # noqa: E402
from minerva_core.gateway.registry import ToolRegistry            # noqa: E402
from minerva_core.gateway import tools_decisions as td            # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def _seed(tmp: str):
    led = Ledger(os.path.join(tmp, "ledger.jsonl"))
    prop = ProposalIndex(led)
    p1 = led.append(PITCH, {"title": "calendar connection", "domain": "connection",
                            "slug": "calendar-connection"})
    p2 = led.append(PITCH, {"title": "Gmail receipt organizer",
                            "domain": "autonomous-income",
                            "slug": "gmail-receipt-organizer"})
    vpath = os.path.join(tmp, "verdicts.jsonl")
    apath = os.path.join(tmp, "approvals.jsonl")
    with open(apath, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ref": "19ecd0d6c09-62508b27", "ts": 1.0,
                            "tool": "agenda_add", "args": {"title": "organizer"},
                            "reason": "inline warden veto: mail policy",
                            "status": "pending"}) + "\n")
    return led, prop, vpath, apath, p1["id"], p2["id"]


def test_helpers(tmp: str) -> None:
    led, prop, vpath, apath, p1, p2 = _seed(os.path.join(tmp, "h"))
    op = td.open_pitches(led, prop)
    check("open_pitches finds both open pitches", len(op) == 2)
    pid, title, err = td.resolve_pitch(op, "calendar")
    check("resolve_pitch by title fragment", pid == p1 and title == "calendar connection")
    pid, _t, err = td.resolve_pitch(op, p2[-6:])
    check("resolve_pitch by id fragment", pid == p2)
    _p, _t, err = td.resolve_pitch(op, "zzz")
    check("resolve_pitch miss is explicit", "no OPEN pitch" in err)
    pa = td.pending_approvals(apath)
    check("pending_approvals finds the pending ask", len(pa) == 1
          and pa[0][0] == "19ecd0d6c09-62508b27")
    ref, err = td.resolve_ref(pa, "62508b27")
    check("resolve_ref by ref fragment", ref == "19ecd0d6c09-62508b27")
    ref, err = td.resolve_ref(pa, "agenda_add")
    check("resolve_ref by tool name", ref == "19ecd0d6c09-62508b27")


def test_tools(tmp: str) -> None:
    led, prop, vpath, apath, p1, p2 = _seed(os.path.join(tmp, "t"))
    reg = ToolRegistry()
    td.register_decision_tools(reg, led, prop, vpath, apath)
    check("three decision tools registered",
          {"decisions_pending", "record_verdict", "resolve_approval"} <= set(reg.names()))
    check("write tools carry decision_write class, list carries none",
          reg.get("record_verdict").classes == {"decision_write"}
          and reg.get("resolve_approval").classes == {"decision_write"}
          and reg.get("decisions_pending").classes == set())

    out = reg.get("decisions_pending").handler({})
    check("decisions_pending lists pitches + approvals with ids/refs",
          "calendar connection" in out and "Gmail receipt organizer" in out
          and "62508b27" in out)

    r = reg.get("record_verdict").handler({"pitch": "calendar", "verdict": "park",
                                           "reopen_condition": "if he wants real cal"})
    check("record_verdict by title -> park, confirmed", "park" in r
          and "calendar connection" in r)
    check("record_verdict FOLDS immediately (pitch now parked in the ledger)",
          prop.state(p1) == "parked")
    note = next(json.loads(l) for l in open(vpath) if "park" in l)["note"]
    check("verdict carries the operator attribution + chat provenance",
          "recorded from chat" in note)

    r = reg.get("record_verdict").handler({"pitch": "receipt", "verdict": "yes"})
    check("record_verdict alias yes -> adopt", "adopt" in r and prop.state(p2) == "adopted")

    out = reg.get("decisions_pending").handler({})
    check("after both verdicts no pitches await", "awaiting your verdict" not in out)

    check("unknown verdict refused",
          reg.get("record_verdict").handler({"pitch": "x", "verdict": "maybe"})
          .startswith("refused"))

    r = reg.get("resolve_approval").handler({"ref": "62508b27", "decision": "approve"})
    check("resolve_approval approve by ref fragment", "approved" in r)
    latest = {}
    for l in open(apath):
        rr = json.loads(l)
        latest[rr["ref"]] = rr.get("status")
    check("approvals.jsonl now shows approved", latest["19ecd0d6c09-62508b27"] == "approved")
    check("resolve_approval bad decision refused",
          reg.get("resolve_approval").handler({"ref": "62508b27", "decision": "maybe"})
          .startswith("refused"))
    check("resolve_approval no-match refused",
          reg.get("resolve_approval").handler({"ref": "nope", "decision": "approve"})
          .startswith("refused"))

    # ambiguity
    led2, prop2, v2, a2, _, _ = _seed(os.path.join(tmp, "amb"))
    led2.append(PITCH, {"title": "calendar reminder thing", "domain": "x",
                        "slug": "calendar-reminder-thing"})
    reg2 = ToolRegistry()
    td.register_decision_tools(reg2, led2, prop2, v2, a2)
    check("ambiguous title refused, asks for more",
          "ambiguous" in reg2.get("record_verdict").handler(
              {"pitch": "calendar", "verdict": "park"}))


def test_gateway_roundtrip(tmp: str) -> None:
    state = os.path.join(tmp, "gw")
    env = dict(os.environ)
    src = os.path.abspath(os.path.join(_HERE, "..", "src"))
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["MINERVA_GMAIL_ENV"] = os.path.join(state, "no.env")
    env["MINERVA_CALENDAR_ENV"] = os.path.join(state, "no2.env")
    p = subprocess.Popen([sys.executable, "-m", "minerva_core.gateway",
                          "--state-dir", state],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    rid = [0]

    def req(method, params=None):
        rid[0] += 1
        msg = {"jsonrpc": "2.0", "id": rid[0], "method": method}
        if params is not None:
            msg["params"] = params
        p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()
        return json.loads(p.stdout.readline())

    def call(tool, args):
        r = req("tools/call", {"name": tool, "arguments": args})
        return r["result"]["content"][0]["text"], r["result"]["isError"]

    try:
        req("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test"}})
        p.stdin.write(json.dumps({"jsonrpc": "2.0",
                                  "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        names = {t["name"] for t in req("tools/list")["result"]["tools"]}
        check("gateway lists decision tools",
              {"decisions_pending", "record_verdict", "resolve_approval"} <= names)
        text, err = call("pitch_idea", {"domain": "work-automation",
                                        "title": "weekly inbox digest"})
        check("seed a pitch via pitch_idea", "pitch logged" in text and not err)
        text, err = call("decisions_pending", {})
        check("decisions_pending shows the open pitch",
              "weekly inbox digest" in text and not err)
        text, err = call("record_verdict", {"pitch": "weekly inbox",
                                            "verdict": "adopt"})
        check("record_verdict via MCP resolves + folds",
              "adopt" in text and not err)
        text, err = call("decisions_pending", {})
        check("pitch no longer awaiting after the verdict",
              "weekly inbox digest" not in text)
    finally:
        p.stdin.close()
        code = p.wait(timeout=10)
    check("gateway exits clean", code == 0)
    led = Ledger(os.path.join(state, "ledger.jsonl"))
    ok, n, _b = led.verify_chain()
    pv = [e for e in led.entries() if e["type"] == "pitch_verdict"]
    check("chain intact + the verdict folded into the ledger",
          ok and len(pv) == 1 and pv[0]["payload"]["verdict"] == "adopt")


def test_wiring(tmp: str) -> None:
    import minerva_chat
    for t in ("decisions_pending", "record_verdict", "resolve_approval"):
        check(f"CHAT_TOOLS carries {t}", t in minerva_chat.CHAT_TOOLS)
    check("persona teaches recording explicit decisions",
          "record_verdict" in minerva_chat.PERSONA
          and "EXPLICITLY" in minerva_chat.PERSONA)
    import drive_tick
    check("the autonomous TICK prompt does NOT invite recording decisions",
          "record_verdict" not in drive_tick.BRAINSTORM_PROMPT)
    from minerva_core.gateway.tools_builtin import build_default_registry
    state = os.path.join(tmp, "w"); os.makedirs(state)
    led = Ledger(os.path.join(state, "ledger.jsonl"))
    dreg = build_default_registry(led, ProposalIndex(led),
                                  outbox_path=os.path.join(state, "outbox.jsonl"),
                                  brain_dir=None)
    check("default registry carries the decision tools",
          {"decisions_pending", "record_verdict", "resolve_approval"} <= set(dreg.names()))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_helpers(tmp)
        test_tools(tmp)
        test_gateway_roundtrip(tmp)
        test_wiring(tmp)
    print(f"\nall {PASSED} checks passed")
