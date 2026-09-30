"""Multi-agent orchestration (round 26): the JobQueue store, the agent runner
(governed single-job session with budget/kill-switch/timeout rails), the
orchestrator thread pool (concurrency, decomposition, MAIN-ledger NOTE under the
shared lock, DM delivery), the three gateway tools, the real-gateway round trip,
and the chat/tick/daemon wiring.   python tests/test_jobs.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from minerva_core import jobs                                    # noqa: E402
from minerva_core import agent_runner                            # noqa: E402
from minerva_core.events import EngineEvent, EventType           # noqa: E402
from minerva_core.jobs import JobQueue, short_id                 # noqa: E402
from minerva_core.killswitch import stop as ks_stop             # noqa: E402
from minerva_core.ledger import Ledger                           # noqa: E402
from minerva_core.orchestrator import (Orchestrator, _acquire_lock,  # noqa: E402
                                       _release_lock)
from minerva_core.spend import SpendMeter, record_spend, spend_path  # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


class EnvPin:
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


# ---- a scripted fake engine (the morning_brief/agenda injection convention) --
def ev(t, **p):
    return EngineEvent(t, "s", p)


class FakeSession:
    def __init__(self, events, sleep=0.0, subjob=None, queue=None):
        self.events = events
        self.sleep = sleep
        self.subjob = subjob
        self.queue = queue
        self.cancelled = False
        self.closed = False

    def send(self, prompt):
        if self.subjob and self.queue:           # simulate a job_submit tool call
            self.queue.submit(self.subjob, origin="job")
        if self.sleep:
            time.sleep(self.sleep)
        for e in self.events:
            if self.cancelled:
                return
            yield e

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class FakeAdapter:
    def __init__(self, **kw):
        self.kw = kw
        self._client = None

    def create_session(self, cfg):
        return FakeSession(**self.kw)


def factory(**kw):
    return lambda sdir: (FakeAdapter(**kw), object(), "byok-fake")


def done_events(result="done", usd=0.003):
    return [ev(EventType.TOOL_CALL, tool="web_fetch", args={}, call_id="1"),
            ev(EventType.COST, usd=usd),
            ev(EventType.MESSAGE, text="JOB-DONE " + result),
            ev(EventType.DONE, stop_reason="end_turn")]


# ============================== STORE ========================================
def test_store(tmp: str) -> None:
    path = os.path.join(tmp, "s1", "jobs.jsonl")
    q = JobQueue(path)

    ev1, msg = q.submit("  Research  quiet mini PCs for a NAS ", context="budget $500",
                        priority="high", origin="operator")
    check("submit returns event + plain text", ev1 is not None and
          msg.startswith("job submitted (") and "Research quiet mini PCs" in msg)
    jid = ev1["id"]
    j = q.fold()[jid]
    check("fold: pending, fields, whitespace-collapsed goal",
          j["status"] == "pending" and j["priority"] == "high"
          and j["goal"] == "Research quiet mini PCs for a NAS"
          and j["context"] == "budget $500" and j["origin"] == "operator")

    check("empty goal refused", q.submit("   ")[0] is None)
    long = q.submit("y" * 1500)
    check("goal clamped to cap",
          long[0] is not None and len(q.fold()[long[0]["id"]]["goal"]) == jobs.GOAL_CAP)

    _, dup = q.submit("research QUIET mini pcs for a nas!!")
    check("near-duplicate of active job refused, names it",
          dup.startswith("refused: near-duplicate") and short_id(jid) in dup)

    with EnvPin(MINERVA_JOBS_MAX_PENDING="2"):
        q.submit("Draft a dentist reminder email to send tomorrow")
        _, full = q.submit("Summarize this week's local news headlines briefly")
        check("pending cap honored with plain refusal",
              full.startswith("refused: job queue full"))

    # lifecycle: pending -> running -> done
    job, m = q.mark_start(jid)
    check("mark_start claims a pending job", job is not None and "started" in m
          and q.fold()[jid]["status"] == "running" and q.fold()[jid]["started_ts"])
    j2, m2 = q.mark_start(jid)
    check("double-claim is a no-op (already running)",
          j2 is None and "already running" in m2)
    q.mark_complete(jid, "Top pick: Beelink SER5; runner-up Minisforum.", usd=0.0123)
    jd = q.fold()[jid]
    check("complete: done + result + usd + ended_ts",
          jd["status"] == "done" and "Beelink" in jd["result"]
          and abs(jd["usd"] - 0.0123) < 1e-9 and jd["ended_ts"])
    check("complete on an already-terminal job is ignored",
          (q.mark_complete(jid, "second result"),
           q.fold()[jid]["result"].startswith("Top pick"))[1])

    # fail + cancel paths
    ef, _ = q.submit("A job destined to fail with a timeout")
    q.mark_start(ef["id"])
    q.mark_fail(ef["id"], "boom: timeout after 300s")
    check("fail path -> failed + error", q.fold()[ef["id"]]["status"] == "failed"
          and "timeout" in q.fold()[ef["id"]]["error"])
    ec, _ = q.submit("A pending job that gets cancelled by the operator")
    check("cancel pending -> cancelled",
          "cancelled" in q.cancel(short_id(ec["id"]), note="changed plan")
          and q.fold()[ec["id"]]["status"] == "cancelled")
    check("cancel a terminal job refused",
          "already" in q.cancel(jid))

    # resolve_id: tail handle, ambiguity, miss
    check("short tail handle resolves",
          q.result_text(short_id(jid)).startswith(f"job {short_id(jid)}"))
    check("unknown id reported plainly", "no such job" in q.result_text("zzzz9999"))

    # torn line + ASCII
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"op": "subm')
        f.write("\nnot json\n")
    check("torn/garbled lines tolerated", q.fold()[jid]["status"] == "done")
    eu, _ = q.submit("Unicode goal cafe -- stays readable", origin="operator")
    with open(path, "rb") as f:
        raw = f.read()
    check("jobs file is pure ASCII (cp1252 rule)", all(b < 128 for b in raw))

    # priority + age ordering of the pending set
    p2 = os.path.join(tmp, "s2", "jobs.jsonl")
    q2 = JobQueue(p2)
    a, _ = q2.submit("Normal priority older task here", priority="normal", now=1000.0)
    b, _ = q2.submit("High priority newer task here", priority="high", now=2000.0)
    c, _ = q2.submit("Low priority middle task here", priority="low", now=1500.0)
    order = [short_id(j["id"]) for j in q2.pending()]
    check("pending ordered high-first then oldest",
          order == [short_id(b["id"]), short_id(a["id"]), short_id(c["id"])])

    # summarize
    s = q2.summarize("active")
    check("summarize active lists pending with ids/priority",
          s.startswith("jobs (active):") and short_id(b["id"]) in s)
    check("summarize a status with none reports totals",
          "none done" in q2.summarize("done"))


# ============================== AGENT RUNNER =================================
def test_agent_runner(tmp: str) -> None:
    main = os.path.join(tmp, "ar"); os.makedirs(main)
    q = JobQueue(jobs.jobs_path(main))

    j, _ = q.submit("Find three good NAS cases for a quiet build", origin="operator")
    q.mark_start(j["id"])
    rep = agent_runner.run_job(q.fold()[j["id"]], q, main,
                               engine_factory=factory(events=done_events("Jonsbo N2, Node 304, Sliger CX.")))
    check("happy path: ok + done + result captured",
          rep["ok"] and rep["status"] == "done"
          and "Jonsbo" in q.fold()[j["id"]]["result"])
    check("JOB-DONE prefix stripped from result",
          not q.fold()[j["id"]]["result"].startswith("JOB-DONE"))
    check("spend metered to the SHARED main meter",
          abs(SpendMeter(spend_path(main)).spent_24h() - 0.003) < 1e-9)
    check("job ran in its OWN isolated state dir",
          os.path.isdir(agent_runner.job_state_dir(main, j["id"])))

    # kill switch pre-flight
    jk, _ = q.submit("Should not start while stopped"); q.mark_start(jk["id"])
    ks_stop(main, reason="test halt")
    rk = agent_runner.run_job(q.fold()[jk["id"]], q, main, engine_factory=factory(events=done_events()))
    check("kill switch refuses before any session",
          not rk["ok"] and "kill switch" in rk["errors"][0]
          and q.fold()[jk["id"]]["status"] == "failed")
    os.remove(os.path.join(main, "STOP"))

    # budget pre-check against shared meter
    jb, _ = q.submit("Blocked by the daily budget cap"); q.mark_start(jb["id"])
    record_spend(spend_path(main), 9.99, "padding")
    with EnvPin(MINERVA_DAILY_BUDGET_USD="5"):
        rb = agent_runner.run_job(q.fold()[jb["id"]], q, main, engine_factory=factory(events=done_events()))
    check("over-budget job refused before spend",
          not rb["ok"] and "over daily budget" in rb["errors"][-1])

    # timeout: a hung session is cancelled + closed, spend still metered
    main2 = os.path.join(tmp, "ar2"); os.makedirs(main2)
    q2 = JobQueue(jobs.jobs_path(main2))
    jt, _ = q2.submit("A job that hangs past its deadline"); q2.mark_start(jt["id"])
    t0 = time.time()
    sess_holder = {}

    class HangAdapter(FakeAdapter):
        def create_session(self, cfg):
            s = FakeSession(events=[ev(EventType.COST, usd=0.001)], sleep=30.0)
            sess_holder["s"] = s
            return s
    rt = agent_runner.run_job(q2.fold()[jt["id"]], q2, main2,
                              engine_factory=lambda sdir: (HangAdapter(), object(), "byok-fake"),
                              timeout_s=1.5)
    el = time.time() - t0
    check("timeout: failed with a timeout reason", not rt["ok"]
          and "timeout" in rt["errors"][-1] and q2.fold()[jt["id"]]["status"] == "failed")
    check("timeout: bounded near the deadline (not the 30s sleep)", 1.5 <= el < 8.0)
    check("timeout: the session was cancelled + closed",
          sess_holder["s"].cancelled and sess_holder["s"].closed)

    # no-key (default_engine raises RuntimeError) -> honest failure
    main3 = os.path.join(tmp, "ar3"); os.makedirs(main3)
    q3 = JobQueue(jobs.jobs_path(main3))
    jn, _ = q3.submit("Needs the paid lane that is not configured"); q3.mark_start(jn["id"])

    def raising(sdir):
        raise RuntimeError("no BYOK key (config/anthropic.env)")
    rn = agent_runner.run_job(q3.fold()[jn["id"]], q3, main3, engine_factory=raising)
    check("no-key: honest failure, never a silent free fallback",
          not rn["ok"] and "no BYOK key" in rn["errors"][-1])

    # ASK_USER -> result records the blocker (a job never asks inline)
    main4 = os.path.join(tmp, "ar4"); os.makedirs(main4)
    q4 = JobQueue(jobs.jobs_path(main4))
    ja, _ = q4.submit("A job that hits an approval wall"); q4.mark_start(ja["id"])
    ask = [ev(EventType.ASK_USER, question="approve $20 buy?", ref="r1"),
           ev(EventType.MESSAGE, text="JOB-DONE Could not complete without approval."),
           ev(EventType.DONE, stop_reason="end_turn")]
    ra = agent_runner.run_job(q4.fold()[ja["id"]], q4, main4, engine_factory=factory(events=ask))
    check("escalation recorded as a blocker in the result",
          ra["ok"] and "blocked - needs the operator" in q4.fold()[ja["id"]]["result"])

    # empty result -> fail
    je, _ = q4.submit("A job that produces no text at all"); q4.mark_start(je["id"])
    re_ = agent_runner.run_job(q4.fold()[je["id"]], q4, main4,
                               engine_factory=factory(events=[ev(EventType.DONE, stop_reason="end_turn")]))
    check("no result text -> failed", not re_["ok"]
          and "no result" in re_["errors"][-1])


# ============================== ORCHESTRATOR ================================
def test_orchestrator(tmp: str) -> None:
    main = os.path.join(tmp, "orc"); os.makedirs(main)
    q = JobQueue(jobs.jobs_path(main))
    goals = ["Research quiet mini PC cases for a home NAS build",
             "Draft a friendly dentist appointment reminder email",
             "Summarize this month's local newspaper headlines",
             "Compare three budget mechanical keyboards under fifty dollars"]
    for g in goals:
        e, m = q.submit(g, origin="operator")
        assert e is not None, m
    orc = Orchestrator(main, q, engine_factory=factory(events=done_events("found it")),
                       notify=True, quiet=True)
    s = orc.drain()
    check("drain dispatched and finished every pending job",
          s["ok"] and s["dispatched"] == 4 and s["done"] == 4)
    check("all jobs now done in the queue",
          all(j["status"] == "done" for j in q.fold().values()))
    led = Ledger(os.path.join(main, "ledger.jsonl"))
    ok, n, _ = led.verify_chain()
    notes = [e for e in led.entries()
             if e["type"] == "note" and e["payload"].get("about") == "job"]
    check("a NOTE per completion lands in the MAIN ledger, chain intact",
          ok and len(notes) == 4)
    check("completion NOTE carries job id + status + usd",
          all(e["payload"].get("job_id") and e["payload"].get("status") == "done"
              for e in notes))
    ob = [json.loads(l) for l in open(os.path.join(main, "outbox.jsonl"))]
    check("a result DM per job queues to the outbox",
          len(ob) == 4 and all(m["message"].startswith("[job] Done:") for m in ob))
    check("note_written reported true under no contention",
          all(r["note_written"] for r in s["results"]))

    # decomposition: a job submits a sub-job, the drainer picks it up
    main2 = os.path.join(tmp, "orc2"); os.makedirs(main2)
    q2 = JobQueue(jobs.jobs_path(main2))
    q2.submit("Parent goal that spawns one child task", origin="operator")
    orc2 = Orchestrator(main2, q2, notify=False, quiet=True, engine_factory=factory(
        events=done_events("spawned"), subjob="A separate child errand about groceries", queue=q2))
    s2 = orc2.drain()
    check("decomposition: parent's sub-job is drained too (recursion via queue)",
          s2["dispatched"] == 2 and len(q2.fold()) == 2
          and all(j["status"] == "done" for j in q2.fold().values()))

    # concurrency: 6 jobs x 0.4s on 3 workers finishes well under serial 2.4s
    main3 = os.path.join(tmp, "orc3"); os.makedirs(main3)
    q3 = JobQueue(jobs.jobs_path(main3))
    for g in ["Sleepy astronomy telescope lookup", "Sleepy bicycle tire repair guide",
              "Sleepy espresso descaler review", "Sleepy houseplant watering schedule",
              "Sleepy Chicago train timetable check", "Sleepy guitar string replacement note"]:
        q3.submit(g)
    t0 = time.time()
    s3 = Orchestrator(main3, q3, n_workers=3, notify=False, quiet=True,
                      engine_factory=factory(events=done_events("slept"), sleep=0.4)).drain()
    el = time.time() - t0
    check("concurrency: 6 jobs all done", s3["done"] == 6)
    check("concurrency: ran in parallel (well under serial time)", el < 1.8)

    # kill switch: drain is a no-op, jobs untouched
    main4 = os.path.join(tmp, "orc4"); os.makedirs(main4)
    q4 = JobQueue(jobs.jobs_path(main4))
    q4.submit("Should not run while stopped", origin="operator")
    ks_stop(main4, reason="halt")
    s4 = Orchestrator(main4, q4, engine_factory=factory(events=done_events()), quiet=True).drain()
    check("kill switch: drain dispatches nothing",
          s4["dispatched"] == 0 and s4.get("skipped") == "killswitch")
    check("kill switch: pending job left untouched",
          list(q4.fold().values())[0]["status"] == "pending")

    # MAIN-ledger single-appender: NOTE skipped (not forked) when lock held
    main5 = os.path.join(tmp, "orc5"); os.makedirs(main5)
    q5 = JobQueue(jobs.jobs_path(main5))
    q5.submit("Runs while a tick holds the engine lock", origin="operator")
    lock = os.path.join(main5, "drive-tick.lock")
    assert _acquire_lock(lock)
    with EnvPin():
        import minerva_core.orchestrator as O
        old_tries, old_wait = O.NOTE_LOCK_TRIES, O.NOTE_LOCK_WAIT_S
        O.NOTE_LOCK_TRIES, O.NOTE_LOCK_WAIT_S = 2, 0.05
        try:
            s5 = Orchestrator(main5, q5, notify=True, quiet=True,
                              engine_factory=factory(events=done_events("ok"))).drain()
        finally:
            O.NOTE_LOCK_TRIES, O.NOTE_LOCK_WAIT_S = old_tries, old_wait
    _release_lock(lock)
    led5 = Ledger(os.path.join(main5, "ledger.jsonl"))
    ok5, _, _ = led5.verify_chain()
    jn5 = [e for e in led5.entries()
           if e["type"] == "note" and e["payload"].get("about") == "job"]
    ob5 = [json.loads(l) for l in open(os.path.join(main5, "outbox.jsonl"))]
    check("lock held: job still completes + is DM'd",
          s5["done"] == 1 and len(ob5) == 1)
    check("lock held: MAIN-ledger NOTE skipped, chain stays intact (no fork)",
          ok5 and len(jn5) == 0 and s5["results"][0]["note_written"] is False)

    # max-drain cap bounds a runaway
    main6 = os.path.join(tmp, "orc6"); os.makedirs(main6)
    q6 = JobQueue(jobs.jobs_path(main6))
    for g in ["Investigate volcano photography spots", "Catalog origami folding techniques",
              "Outline submarine warfare history", "Transcribe a jazz saxophone solo",
              "Schedule desert cactus watering reminders"]:
        q6.submit(g)
    s6 = Orchestrator(main6, q6, n_workers=2, notify=False, quiet=True,
                      engine_factory=factory(events=done_events())).drain(max_jobs=3)
    check("max_jobs caps dispatch in a single drain", s6["dispatched"] == 3
          and s6["hit_cap"] is True)


# ============================== GATEWAY TOOLS ===============================
def test_tools(tmp: str) -> None:
    from minerva_core.gateway.registry import ToolRegistry
    from minerva_core.gateway.tools_jobs import register_job_tools
    path = os.path.join(tmp, "t1", "jobs.jsonl")
    reg = ToolRegistry()
    register_job_tools(reg, path)
    check("three job tools registered",
          {"job_submit", "job_list", "job_result"} <= set(reg.names()))
    check("submit carries job_write class; list/result carry none",
          reg.get("job_submit").classes == {"job_write"}
          and reg.get("job_list").classes == set()
          and reg.get("job_result").classes == set())

    out = reg.get("job_submit").handler({"goal": "Plan a weekend trip to the lake",
                                         "priority": "high", "origin": "operator"})
    check("submit via tool", out.startswith("job submitted ("))
    iid = out.split("(", 1)[1].split(")", 1)[0]
    check("list via tool shows it", "the lake" in reg.get("job_list").handler({}))
    check("result via tool before done says pending",
          "still pending" in reg.get("job_result").handler({"id": iid}))

    with EnvPin(MINERVA_JOBS_SUBMITS_PER_SESSION="2"):
        reg.get("job_submit").handler({"goal": "Second distinct job about tax documents"})
        bounced = reg.get("job_submit").handler({"goal": "Third job re car maintenance schedule"})
        check("per-session submit cap refuses plainly",
              bounced.startswith("refused: you already submitted 2"))
    reg2 = ToolRegistry(); register_job_tools(reg2, path)
    with EnvPin(MINERVA_JOBS_SUBMITS_PER_SESSION="2"):
        check("new session = fresh submit counter",
              reg2.get("job_submit").handler({"goal": "Fresh session fresh cap job"}).startswith("job submitted"))


# ============================== REAL GATEWAY ================================
def test_gateway_roundtrip(tmp: str) -> None:
    state = os.path.join(tmp, "gw")
    env = dict(os.environ)
    src = os.path.abspath(os.path.join(_HERE, "..", "src"))
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["MINERVA_GMAIL_ENV"] = os.path.join(state, "no-gmail.env")
    env["MINERVA_CALENDAR_ENV"] = os.path.join(state, "no-cal.env")
    for k in ("MINERVA_JOBS_MAX_PENDING", "MINERVA_JOBS_SUBMITS_PER_SESSION"):
        env.pop(k, None)
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
        check("gateway lists job tools",
              {"job_submit", "job_list", "job_result"} <= names)
        text, err = call("job_submit", {"goal": "Round-trip background job",
                                        "origin": "operator"})
        check("MCP job_submit", text.startswith("job submitted") and not err)
        text, err = call("job_list", {})
        check("MCP job_list", "Round-trip background job" in text and not err)
    finally:
        p.stdin.close()
        code = p.wait(timeout=10)
    check("gateway exits clean", code == 0)
    ok, n, _ = Ledger(os.path.join(state, "ledger.jsonl")).verify_chain()
    check("ledger chain intact, job calls audited", ok and n >= 2)
    check("jobs file lands in the state dir",
          os.path.exists(os.path.join(state, "jobs.jsonl")))


# ============================== WIRING =====================================
def test_wiring(tmp: str) -> None:
    import drive_tick
    import minerva_chat
    import run_orchestrator

    for t in ("job_submit", "job_list", "job_result"):
        check(f"CHAT_TOOLS carries {t}", t in minerva_chat.CHAT_TOOLS)
    check("chat persona teaches delegating big work via job_submit",
          "job_submit" in minerva_chat.PERSONA and "background" in minerva_chat.PERSONA)

    pr = drive_tick.BRAINSTORM_PROMPT
    check("tick prompt offers job_submit for bounded work",
          "mcp_minerva_job_submit" in pr)
    check("tick prompt: a job does not count as the one pitch",
          "do NOT count as your one pitch" in pr)
    check("tick prompt still ends with TICK-DONE rule",
          "9. Finish by replying exactly: TICK-DONE" in pr)
    check("tick prompt stays ASCII", pr.encode("ascii", "strict") is not None)

    # default registry carries job tools, file lands next to the outbox
    from minerva_core.gateway.tools_builtin import build_default_registry
    from minerva_core.ledger import Ledger as L, ProposalIndex
    state = os.path.join(tmp, "w1"); os.makedirs(state)
    led = L(os.path.join(state, "ledger.jsonl"))
    dreg = build_default_registry(led, ProposalIndex(led),
                                  outbox_path=os.path.join(state, "outbox.jsonl"),
                                  brain_dir=None)
    check("default registry carries job tools (no brain, no workspace)",
          {"job_submit", "job_list", "job_result"} <= set(dreg.names()))
    dreg.get("job_submit").handler({"goal": "Lands next to the outbox in state dir"})
    check("jobs file lands in the state dir from the default registry",
          os.path.exists(os.path.join(state, "jobs.jsonl")))

    # the daemon: dry-run reports the queue without running anything
    s = run_orchestrator.drain_once  # callable exists
    check("run_orchestrator exposes drain_once + watch + main",
          callable(s) and callable(run_orchestrator.watch)
          and callable(run_orchestrator.main))
    check("orchestrator launcher .bat exists",
          os.path.exists(os.path.join(ROOT, "Minerva - Start Orchestrator.bat")))


def test_orchestrator_singleton(tmp: str) -> None:
    import run_orchestrator as R
    d = os.path.join(tmp, "single"); os.makedirs(d)
    check("first orchestrator claims the singleton lock",
          R.acquire_singleton(d) is True)
    check("_pid_alive true for our own live pid", R._pid_alive(os.getpid()))
    check("_pid_alive false for an impossible pid", not R._pid_alive(2147480000))
    import json as _json
    # A live pid that is NOT ours, on any platform.  This used to hardcode pid 1
    # (init on POSIX) - which does not exist on Windows, so the lock read as
    # stale, the second acquire succeeded, and the check failed on the laptop.
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with open(os.path.join(d, R.LOCK_NAME), "w") as f:
            _json.dump({"pid": live.pid, "ts": time.time()}, f)
        check("a second orchestrator is refused while one is LIVE",
              R.acquire_singleton(d) is False)
    finally:
        live.terminate()
        live.wait()
    with open(os.path.join(d, R.LOCK_NAME), "w") as f:
        _json.dump({"pid": 2147480000, "ts": time.time()}, f)   # dead pid
    check("a crashed orchestrator's lock is reclaimed (supervisor can restart)",
          R.acquire_singleton(d) is True)
    R.release_singleton(d)
    check("release clears the lock file",
          not os.path.exists(os.path.join(d, R.LOCK_NAME)))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_store(tmp)
        test_agent_runner(tmp)
        test_orchestrator(tmp)
        test_tools(tmp)
        test_gateway_roundtrip(tmp)
        test_orchestrator_singleton(tmp)
        test_wiring(tmp)
    print(f"\nall {PASSED} checks passed")
