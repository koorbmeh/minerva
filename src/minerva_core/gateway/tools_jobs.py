"""Gateway tools for the JOB QUEUE (round 26) - delegate real async work.

Three tools, governed like everything else (every call rides GovernanceGate and
lands in the hash-chained ledger):
  * job_submit  - drop a high-level GOAL on the queue (class job_write -
                  flippable into governance.ASK_CLASSES / MINERVA_INLINE_CLASSES
                  the day the operator wants new jobs approval-gated or warden-
                  reviewed; today it is an allow-class - the orchestrator runs
                  each job under its OWN governed gateway + budget + kill
                  switch, so the work itself is already fully governed).
  * job_list    - see the queue (read; always on).
  * job_result  - read a finished job's result (read; always on).

The orchestrator daemon (scripts/run_orchestrator.py) drains pending jobs into
a bounded thread pool. These tools only TOUCH the queue file - they never run a
job inline (a gateway tool call must return fast; jobs take seconds-to-minutes).

Anti-flood rail (the anti-nag lesson, applied to work): job_submit is capped per
GATEWAY PROCESS (one process == one engine session), so a looping engine cannot
stuff the queue in a single session; the queue's own MAX_PENDING + near-dup
refusal bound the total regardless. This is what makes engine-driven
decomposition (a job submitting sub-jobs) safe.

Stdlib only. Plain-language results (the model relays them to the operator).
"""
from __future__ import annotations

import os

from ..jobs import JobQueue
from .registry import ToolRegistry, ToolSpec

DEFAULT_SUBMITS_PER_SESSION = 3
LIST_CAP = 20


def submits_per_session() -> int:
    try:
        return int(os.environ.get("MINERVA_JOBS_SUBMITS_PER_SESSION", "").strip()
                   or DEFAULT_SUBMITS_PER_SESSION)
    except ValueError:
        return DEFAULT_SUBMITS_PER_SESSION


def register_job_tools(reg: ToolRegistry, jobs_path: str) -> None:
    queue = JobQueue(jobs_path)
    session = {"submits": 0}          # per gateway process == per engine session

    def _submit(a: dict) -> str:
        cap = submits_per_session()
        if session["submits"] >= cap:
            return (f"refused: you already submitted {cap} job(s) this session "
                    "- let the orchestrator work them before adding more")
        ev, msg = queue.submit(str(a.get("goal", "")),
                               context=str(a.get("context", "") or ""),
                               priority=str(a.get("priority", "") or "normal"),
                               origin=str(a.get("origin", "") or "engine"))
        if ev is not None:
            session["submits"] += 1
        return msg

    def _list(a: dict) -> str:
        which = str(a.get("status", "") or "active").strip().lower()
        try:
            n = int(a.get("max_items", LIST_CAP) or LIST_CAP)
        except (TypeError, ValueError):
            n = LIST_CAP
        return queue.summarize(status=which, max_items=max(1, min(n, LIST_CAP)))

    def _result(a: dict) -> str:
        return queue.result_text(str(a.get("id", "")))

    reg.register(ToolSpec(
        name="job_submit",
        description=("Delegate a larger task to your background workers. Drop a "
                     "concrete high-level GOAL here and the orchestrator runs it "
                     "as its own agent session (research, files, recall - it can "
                     "even split into sub-jobs), then reports the result to "
                     "the operator. Use this when a request is too big to finish in one "
                     "reply, or is ongoing background work - NOT for a quick "
                     "answer you can give now, an idea for the operator (pitch_idea), "
                     "or a standing reminder (agenda_add). priority: "
                     "high|normal|low. Near-duplicate goals and a full queue are "
                     "refused; submits are capped per session."),
        input_schema={"type": "object",
                      "properties": {"goal": {"type": "string"},
                                     "context": {"type": "string"},
                                     "priority": {"type": "string",
                                                  "enum": ["high", "normal",
                                                           "low"]},
                                     "origin": {"type": "string"}},
                      "required": ["goal"]},
        classes={"job_write"},
        handler=_submit))

    reg.register(ToolSpec(
        name="job_list",
        description=("List background jobs and their status. Default shows the "
                     "ACTIVE set (pending + running) with ids, priority, age, "
                     "and the goal. status: active|pending|running|done|failed|"
                     "cancelled|all."),
        input_schema={"type": "object",
                      "properties": {"status": {"type": "string",
                                                "enum": ["active", "pending",
                                                         "running", "done",
                                                         "failed", "cancelled",
                                                         "all"]},
                                     "max_items": {"type": "integer"}}},
        handler=_list))

    reg.register(ToolSpec(
        name="job_result",
        description=("Read the result (or failure reason) of a job by id (a "
                     "unique id prefix or its short tail handle works). Use it "
                     "after job_list shows a job is done, to relay what the "
                     "background worker found or did."),
        input_schema={"type": "object",
                      "properties": {"id": {"type": "string"}},
                      "required": ["id"]},
        handler=_result))
