"""Standing agenda (round 23): the event-sourced store, the three gateway
tools, caps + dedupe + done-needs-evidence rails, the real-gateway round
trip, and the tick/chat wiring.  python tests/test_agenda.py
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

from minerva_core import agenda                                   # noqa: E402
from minerva_core.gateway.registry import ToolRegistry            # noqa: E402
from minerva_core.gateway.tools_agenda import register_agenda_tools  # noqa: E402
from minerva_core.ledger import Ledger, PITCH, PITCH_VERDICT      # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class EnvPin:
    """Set env vars for a block, restore exactly after (suite isolation)."""

    def __init__(self, **kv) -> None:
        self.kv = kv
        self.old: dict = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def test_store(tmp: str) -> None:
    path = os.path.join(tmp, "s1", "agenda.jsonl")

    ev, msg = agenda.add(path, "  Watch   the Venice catalog for new video models ")
    check("add returns event + plain text", ev is not None and
          msg.startswith("added to agenda (") and "Watch the Venice" in msg)
    items = agenda.fold(path)
    it = items[ev["id"]]
    check("fold shows the open item, title whitespace-collapsed",
          it["status"] == "open" and
          it["title"] == "Watch the Venice catalog for new video models")

    ev2, msg2 = agenda.add(path, "x" * 250)
    check("title clamped to cap", ev2 is not None and
          len(agenda.fold(path)[ev2["id"]]["title"]) == agenda.TITLE_CAP)
    check("empty title refused", agenda.add(path, "   ")[0] is None)

    _, dup = agenda.add(path, "watch the VENICE catalog for new video models!!")
    check("near-duplicate of an open item refused, names it",
          dup.startswith("refused: near-duplicate")
          and agenda.short_id(ev["id"]) in dup)
    ev3, _ = agenda.add(path, "Summarize the week's verdicts for the self-model")
    check("distinct title accepted", ev3 is not None)

    with EnvPin(MINERVA_AGENDA_MAX_OPEN="4"):       # ev, ev2, ev3 are open
        ev4, _ = agenda.add(path, "Fourth slot filler item")
        _, full = agenda.add(path, "Fifth item should bounce")
        check("open cap honored with plain refusal", ev4 is not None and
              full.startswith("refused: agenda full (4 open, cap 4)"))

    out = agenda.update(path, ev["id"], note="checked catalog, nothing new yet")
    check("progress note: status unchanged, note folded",
          "progress noted" in out and
          agenda.fold(path)[ev["id"]]["status"] == "open" and
          agenda.fold(path)[ev["id"]]["notes"][-1]["note"]
          == "checked catalog, nothing new yet")

    check("done WITHOUT note refused (evidence rule)",
          agenda.update(path, ev["id"], status="done").startswith(
              "refused: marking an item done needs a note"))
    out = agenda.update(path, ev["id"], status="done",
                        note="catalog watched 3 weeks, no change - calling it")
    check("done WITH note lands", "status -> done" in out and
          agenda.fold(path)[ev["id"]]["status"] == "done" and
          agenda.fold(path)[ev["id"]].get("done_ts"))
    check("dropped needs a note too",
          agenda.update(path, ev3["id"], status="dropped").startswith("refused:")
          and "status -> dropped" in agenda.update(
              path, ev3["id"], status="dropped", note="superseded by r23 build"))
    check("reopen works", "status -> open" in
          agenda.update(path, ev["id"], status="open", note="new models rumored")
          and agenda.fold(path)[ev["id"]]["status"] == "open")

    check("unknown id refused plainly",
          "no agenda item matches" in agenda.update(path, "zzzz9999", note="x"))
    cp = os.path.commonprefix([ev["id"], ev2["id"]])
    check("ambiguous prefix refused", len(cp) >= 1 and
          "ambiguous" in agenda.update(path, cp, note="x"))
    others = [i for i in agenda.fold(path) if i != ev3["id"]]
    k = max(len(os.path.commonprefix([ev3["id"], o])) for o in others) + 1
    uniq = ev3["id"][:k]                      # shortest provably-unique prefix
    check("unique prefix resolves",
          f'agenda {agenda.short_id(ev3["id"])}'
          in agenda.update(path, uniq, note="touch"))
    check("short tail handle resolves (the handle add/list print)",
          f'agenda {agenda.short_id(ev2["id"])}'
          in agenda.update(path, agenda.short_id(ev2["id"]), note="tail works"))

    with open(path, "a", encoding="utf-8") as f:
        f.write('{"op": "upd')                     # torn tail
        f.write("\nnot json at all\n")
    check("torn/garbled lines tolerated",
          agenda.fold(path)[ev["id"]]["status"] == "open")

    out = agenda.update(path, ev["id"], note="n" * 600)
    check("note clamped",
          len(agenda.fold(path)[ev["id"]]["notes"][-1]["note"]) == agenda.NOTE_CAP)
    check("update on missing file refused", "no agenda item matches" in
          agenda.update(os.path.join(tmp, "nowhere.jsonl"), "abc", note="x"))

    ev_u, _ = agenda.add(path, "Unicode title café — stays readable",
                         origin="operator")
    with open(path, "rb") as f:
        raw = f.read()
    check("agenda file is pure ASCII (cp1252 rule)",
          all(b < 128 for b in raw))
    check("unicode title round-trips through the fold",
          "caf" in agenda.fold(path)[ev_u["id"]]["title"])


def test_summarize_and_adopted(tmp: str) -> None:
    path = os.path.join(tmp, "s2", "agenda.jsonl")
    check("summarize on empty store", agenda.summarize(path) == "agenda: empty")

    ev1, _ = agenda.add(path, "Oldest open item", origin="operator", now=1000.0)
    fillers = ["Restock alpha widgets", "Audit beta gizmos",
               "Refresh gamma listings", "Compare delta vendors",
               "Renew epsilon licenses", "Archive zeta records"]
    for i, title in enumerate(fillers):
        got, msg = agenda.add(path, title, now=2000.0 + i)
        assert got is not None, f"filler add failed: {msg}"
    s = agenda.summarize(path, max_items=5, now=1000.0 + 86400.0)
    check("summary: oldest first with id, age, origin",
          s.splitlines()[1].startswith(
              f"- [{agenda.short_id(ev1['id'])}] (1.0d) Oldest open")
          and "(origin: operator)" in s.splitlines()[1])
    check("summary caps and counts the rest", "(+2 more open)" in s)

    state = os.path.join(tmp, "s2led")
    os.makedirs(state)
    led = Ledger(os.path.join(state, "ledger.jsonl"))
    p1 = led.append(PITCH, {"title": "Standing email digest", "domain": "work"})
    led.append(PITCH_VERDICT, {"verdict": "adopt", "reason": "yes do it"},
               parent_id=p1["id"], actor="operator")
    p2 = led.append(PITCH, {"title": "Mined then unloved", "domain": "x"})
    led.append(PITCH_VERDICT, {"verdict": "adopt"}, parent_id=p2["id"],
               actor="operator")
    led.append(PITCH_VERDICT, {"verdict": "park", "reason": "changed my mind"},
               parent_id=p2["id"], actor="operator")
    entries = led.entries()
    hits = agenda.adopted_pitches_off_agenda(entries, agenda.fold(path))
    check("adopted+unclaimed pitch surfaces",
          (p1["id"], "Standing email digest") in hits)
    check("latest verdict wins (adopt then park = not adopted)",
          all(pid != p2["id"] for pid, _ in hits))
    agenda.add(path, "Build the standing email digest",
               origin=f"pitch:{p1['id']}")
    hits = agenda.adopted_pitches_off_agenda(entries, agenda.fold(path))
    check("claimed pitch (origin pitch:<id>) stops surfacing",
          all(pid != p1["id"] for pid, _ in hits))


def test_tools(tmp: str) -> None:
    path = os.path.join(tmp, "t1", "agenda.jsonl")
    reg = ToolRegistry()
    register_agenda_tools(reg, path)
    check("three agenda tools registered",
          {"agenda_list", "agenda_add", "agenda_update"} <= set(reg.names()))
    check("mutations carry agenda_write class, list carries none",
          reg.get("agenda_add").classes == {"agenda_write"} and
          reg.get("agenda_update").classes == {"agenda_write"} and
          reg.get("agenda_list").classes == set())

    check("list on empty queue", reg.get("agenda_list").handler({})
          == "agenda: empty")
    out = reg.get("agenda_add").handler({"title": "Track grocery prices",
                                         "origin": "operator"})
    check("add via tool", out.startswith("added to agenda ("))
    iid = out.split("(", 1)[1].split(")", 1)[0]
    check("list shows it with origin",
          "Track grocery prices" in reg.get("agenda_list").handler({})
          and "(origin: operator)" in reg.get("agenda_list").handler({}))

    with EnvPin(MINERVA_AGENDA_ADDS_PER_SESSION="2"):
        reg.get("agenda_add").handler({"title": "Second add this session"})
        out = reg.get("agenda_add").handler({"title": "Third should bounce"})
        check("per-session add cap refuses with plain text",
              out.startswith("refused: you already added 2"))
    reg2 = ToolRegistry()
    register_agenda_tools(reg2, path)
    with EnvPin(MINERVA_AGENDA_ADDS_PER_SESSION="2"):
        out = reg2.get("agenda_add").handler({"title": "Fresh session fresh cap"})
        check("new session = fresh add counter", out.startswith("added"))

    out = reg.get("agenda_update").handler(
        {"id": iid, "status": "done", "note": "prices logged for 4 weeks"})
    check("update via tool", "status -> done" in out)
    done_list = reg.get("agenda_list").handler({"status": "done"})
    check("status filter shows done items",
          "Track grocery prices" in done_list and "(done)" in done_list)
    check("bogus status filter answers plainly",
          reg.get("agenda_list").handler({"status": "someday"})
          == "status must be open, done, dropped, or all")
    all_list = reg.get("agenda_list").handler({"status": "all"})
    check("all filter mixes statuses",
          "(done)" in all_list and "(open)" in all_list)

    # default registry: agenda rides unconditionally, file next to the outbox
    state = os.path.join(tmp, "t2")
    os.makedirs(state)
    from minerva_core.gateway.tools_builtin import build_default_registry
    from minerva_core.ledger import ProposalIndex
    led = Ledger(os.path.join(state, "ledger.jsonl"))
    dreg = build_default_registry(led, ProposalIndex(led),
                                  outbox_path=os.path.join(state, "outbox.jsonl"),
                                  brain_dir=None)
    check("default registry carries agenda tools (no brain, no workspace)",
          {"agenda_list", "agenda_add", "agenda_update"} <= set(dreg.names()))
    dreg.get("agenda_add").handler({"title": "Lands next to the outbox"})
    check("agenda file lands in the state dir",
          os.path.exists(os.path.join(state, "agenda.jsonl")))


def test_gateway_roundtrip(tmp: str) -> None:
    state = os.path.join(tmp, "gw")
    env = dict(os.environ)
    src = os.path.abspath(os.path.join(_HERE, "..", "src"))
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["MINERVA_GMAIL_ENV"] = os.path.join(state, "no-gmail.env")
    env["MINERVA_CALENDAR_ENV"] = os.path.join(state, "no-cal.env")
    for k in ("MINERVA_AGENDA_MAX_OPEN", "MINERVA_AGENDA_ADDS_PER_SESSION"):
        env.pop(k, None)
    p = subprocess.Popen([sys.executable, "-m", "minerva_core.gateway",
                          "--state-dir", state],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    rid = [0]

    def req(method: str, params: dict | None = None) -> dict:
        rid[0] += 1
        msg: dict = {"jsonrpc": "2.0", "id": rid[0], "method": method}
        if params is not None:
            msg["params"] = params
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    def call(tool: str, args: dict) -> tuple[str, bool]:
        r = req("tools/call", {"name": tool, "arguments": args})
        return (r["result"]["content"][0]["text"], r["result"]["isError"])

    try:
        req("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test"}})
        p.stdin.write(json.dumps({"jsonrpc": "2.0",
                                  "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        names = {t["name"] for t in req("tools/list")["result"]["tools"]}
        check("gateway lists agenda tools",
              {"agenda_list", "agenda_add", "agenda_update"} <= names)
        text, err = call("agenda_add", {"title": "Round-trip item",
                                        "origin": "operator"})
        check("MCP agenda_add", text.startswith("added to agenda") and not err)
        iid = text.split("(", 1)[1].split(")", 1)[0]
        text, err = call("agenda_list", {})
        check("MCP agenda_list", "Round-trip item" in text and not err)
        text, err = call("agenda_update", {"id": iid, "status": "done",
                                           "note": "round-trip proven"})
        check("MCP agenda_update", "status -> done" in text and not err)
    finally:
        p.stdin.close()
        code = p.wait(timeout=10)
    check("gateway exits clean", code == 0)
    ok, n, _bad = Ledger(os.path.join(state, "ledger.jsonl")).verify_chain()
    check("ledger chain intact, calls audited", ok and n >= 3)
    check("agenda file in state dir",
          os.path.exists(os.path.join(state, "agenda.jsonl")))


def test_wiring(tmp: str) -> None:
    import drive_tick
    import minerva_chat

    for t in ("agenda_list", "agenda_add", "agenda_update"):
        check(f"CHAT_TOOLS carries {t}", t in minerva_chat.CHAT_TOOLS)
    check("persona teaches the standing agenda",
          "STANDING AGENDA" in minerva_chat.PERSONA
          and "agenda_update" in minerva_chat.PERSONA)

    pr = drive_tick.BRAINSTORM_PROMPT
    check("tick prompt teaches agenda movement",
          "mcp_minerva_agenda_update" in pr and "STANDING AGENDA" in pr)
    check("tick prompt: agenda actions exempt from the one-pitch rule",
          "do NOT count as your one pitch" in pr)
    check("tick prompt still ends with TICK-DONE rule",
          "9. Finish by replying exactly: TICK-DONE" in pr)
    check("tick prompt stays ASCII",
          pr.encode("ascii", "strict") is not None)

    state = os.path.join(tmp, "w1")
    os.makedirs(state)
    led = Ledger(os.path.join(state, "ledger.jsonl"))
    pe = led.append(PITCH, {"title": "Adopted but unqueued", "domain": "work"})
    led.append(PITCH_VERDICT, {"verdict": "adopt", "reason": "go"},
               parent_id=pe["id"], actor="operator")
    seed = drive_tick.build_context_seed(state)
    check("seed surfaces the adopted-but-unqueued pitch",
          "NOT on your agenda yet" in seed and "Adopted but unqueued" in seed
          and f"pitch:{pe['id']}" in seed)
    agenda.add(agenda.agenda_path(state), "Do the adopted thing",
               origin=f"pitch:{pe['id']}")
    seed = drive_tick.build_context_seed(state)
    check("seed shows the agenda once items exist",
          "Your standing agenda" in seed and "Do the adopted thing" in seed)
    check("claimed pitch no longer flagged", "NOT on your agenda yet" not in seed)
    check("seed stays ASCII", seed.encode("ascii", "strict") is not None)

    ps1 = open(os.path.join(ROOT, "scripts", "enable_agenda.ps1"),
               encoding="utf-8").read()
    check("enable click unions all three tools into MINERVA_TOOLS",
          all(t in ps1 for t in ("agenda_list", "agenda_add", "agenda_update"))
          and "MINERVA_TOOLS" in ps1)
    st = open(os.path.join(ROOT, "scripts", "minerva_status.py"),
              encoding="utf-8").read()
    check("status line reports her agenda", "her agenda" in st)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_store(tmp)
        test_summarize_and_adopted(tmp)
        test_tools(tmp)
        test_gateway_roundtrip(tmp)
        test_wiring(tmp)
    print(f"\nall {PASSED} checks passed")
