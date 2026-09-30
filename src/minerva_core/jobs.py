r"""Job queue (round 26) - the multi-agent orchestration backbone.

The vision's "barely finite tasks" + income/value-agents tier needs real work
to run ASYNCHRONOUSLY: The operator (or Minerva herself, via the job_submit tool)
drops a high-level GOAL on the queue; the orchestrator daemon drains it into a
bounded thread pool; each job runs as its OWN governed BYOK agent session in an
ISOLATED state dir (its own ledger - no concurrent-append races against the
main hash chain) and reports a result back here. The agenda is her standing
INTENTIONS; the job queue is work IN FLIGHT.

Mechanics mirror agenda.py exactly (same trust tier as outbox.jsonl): event-
sourced JSONL at state/jobs.jsonl, one event per line {ts, op, id, ...}, the
current view is a fold, append-only, torn-line tolerant. The hash-chained
ledger stays the audit trail - every job_* TOOL CALL lands there, and the
orchestrator writes a NOTE per completion to the MAIN ledger.

Lifecycle:  submit -> (start) -> complete | fail | cancel
   pending  --start-->  running  --complete-->  done
                                 \--fail------>  failed
   pending/running  --cancel-->  cancelled

Honesty rails (the anti-nag lessons, applied to work): MAX_PENDING cap (a queue
that only grows is a problem, not a plan - the refusal says drain or cancel
something first); near-duplicate GOALS refused against pending/running jobs
(slug Jaccard); goal/context/result/error clamps. Claiming is serialized by the
orchestrator (one daemon process, one in-process lock) so two workers never
grab the same pending job.

Stdlib only. ASCII out (cp1252 rule): events are written ensure_ascii.
"""
from __future__ import annotations

import json
import os
import time

from .ledger import new_id, similar, slugify

JOBS_NAME = "jobs.jsonl"
STATUSES = {"pending", "running", "done", "failed", "cancelled"}
TERMINAL = {"done", "failed", "cancelled"}
ACTIVE = {"pending", "running"}
PRIORITIES = ("high", "normal", "low")
_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}

GOAL_CAP = 1000
CONTEXT_CAP = 4000
RESULT_CAP = 8000
ERROR_CAP = 1000
NOTE_CAP = 500
ORIGIN_CAP = 80
DEFAULT_MAX_PENDING = 25


def max_pending() -> int:
    try:
        return int(os.environ.get("MINERVA_JOBS_MAX_PENDING", "").strip()
                   or DEFAULT_MAX_PENDING)
    except ValueError:
        return DEFAULT_MAX_PENDING


def jobs_path(state_dir: str) -> str:
    return os.path.join(state_dir, JOBS_NAME)


def short_id(iid: str) -> str:
    """Display handle: the RANDOM tail of a ledger-style id (the millisecond
    time-prefix collides for anything created in the same instant)."""
    return str(iid).rsplit("-", 1)[-1]


def _norm_priority(p) -> str:
    p = str(p if p is not None else "normal").strip().lower()
    return p if p in _PRIORITY_RANK else "normal"


def _read_events(path: str) -> list[dict]:
    """READ-ONLY, torn/garbled lines skipped (operational state, not audit)."""
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except OSError:
        return out
    return out


def _append(path: str, ev: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=True, sort_keys=True) + "\n")


def fold(path: str) -> dict[str, dict]:
    """id -> current job view. Duplicate submits keep the first; transition
    events on an unknown/terminal id are skipped (tolerance for damage and for
    replays, not business logic)."""
    jobs: dict[str, dict] = {}
    for ev in _read_events(path):
        op = ev.get("op")
        iid = str(ev.get("id", "") or "")
        if not iid:
            continue
        ts = float(ev.get("ts", 0.0) or 0.0)
        if op == "submit":
            if iid in jobs:
                continue
            jobs[iid] = {"id": iid,
                         "goal": str(ev.get("goal", ""))[:GOAL_CAP],
                         "context": str(ev.get("context", ""))[:CONTEXT_CAP],
                         "priority": _norm_priority(ev.get("priority")),
                         "origin": str(ev.get("origin", "engine"))[:ORIGIN_CAP],
                         "status": "pending", "ts": ts, "updated_ts": ts,
                         "started_ts": None, "ended_ts": None,
                         "result": "", "error": "", "usd": 0.0}
            continue
        job = jobs.get(iid)
        if job is None:
            continue
        if op == "start":
            if job["status"] == "pending":
                job["status"] = "running"
                job["started_ts"] = ts
        elif op == "complete":
            if job["status"] not in TERMINAL:
                job["status"] = "done"
                job["ended_ts"] = ts
                job["result"] = str(ev.get("result", ""))[:RESULT_CAP]
                job["usd"] = float(ev.get("usd", 0.0) or 0.0)
        elif op == "fail":
            if job["status"] not in TERMINAL:
                job["status"] = "failed"
                job["ended_ts"] = ts
                job["error"] = str(ev.get("error", ""))[:ERROR_CAP]
                job["usd"] = float(ev.get("usd", 0.0) or 0.0)
        elif op == "cancel":
            if job["status"] not in TERMINAL:
                job["status"] = "cancelled"
                job["ended_ts"] = ts
                job["error"] = str(ev.get("note", "") or "cancelled")[:ERROR_CAP]
        job["updated_ts"] = ts
    return jobs


def _by_priority_then_age(job: dict) -> tuple:
    return (_PRIORITY_RANK.get(job["priority"], 1), job["ts"])


def pending_jobs(jobs: dict[str, dict]) -> list[dict]:
    """Pending jobs, highest priority first, then oldest first (FIFO)."""
    return sorted((j for j in jobs.values() if j["status"] == "pending"),
                  key=_by_priority_then_age)


def active_jobs(jobs: dict[str, dict]) -> list[dict]:
    return sorted((j for j in jobs.values() if j["status"] in ACTIVE),
                  key=_by_priority_then_age)


def resolve_id(jobs: dict[str, dict], piece: str) -> tuple[dict | None, str]:
    """Exact id, else unique prefix, else unique CONTAINS - so the short tail
    handle that submit/list print resolves (the agenda convention)."""
    piece = str(piece or "").strip().lower()
    if not piece:
        return None, "no id given"
    if piece in jobs:
        return jobs[piece], ""
    hits = [j for k, j in jobs.items() if k.startswith(piece)]
    if not hits:
        hits = [j for k, j in jobs.items() if piece in k]
    if not hits:
        return None, f"no job matches id '{piece}' (job_list shows ids)"
    if len(hits) > 1:
        ids = ", ".join(sorted(short_id(j["id"]) for j in hits))
        return None, f"id '{piece}' is ambiguous ({ids}) - use more characters"
    return hits[0], ""


def counts(jobs: dict[str, dict]) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for j in jobs.values():
        out[j["status"]] = out.get(j["status"], 0) + 1
    return out


class JobQueue:
    """Thin wrapper over the event log at `path`. Every method folds fresh
    (append-only, torn-tolerant); no in-memory cache to drift."""

    def __init__(self, path: str) -> None:
        self.path = path

    # ---- reads ----------------------------------------------------------
    def fold(self) -> dict[str, dict]:
        return fold(self.path)

    def get(self, id_piece: str) -> tuple[dict | None, str]:
        return resolve_id(self.fold(), id_piece)

    def pending(self) -> list[dict]:
        return pending_jobs(self.fold())

    def active(self) -> list[dict]:
        return active_jobs(self.fold())

    def counts(self) -> dict[str, int]:
        return counts(self.fold())

    # ---- writes ---------------------------------------------------------
    def submit(self, goal: str, context: str = "", priority: str = "normal",
               origin: str = "engine",
               now: float | None = None) -> tuple[dict | None, str]:
        """Returns (event-or-None, plain-language result text)."""
        goal = " ".join(str(goal or "").split())[:GOAL_CAP]
        if not goal:
            return None, "refused: empty goal"
        jobs = self.fold()
        slug = slugify(goal)
        for j in jobs.values():
            if j["status"] in ACTIVE and similar(slug, slugify(j["goal"])):
                return None, (f"refused: near-duplicate of {j['status']} job "
                              f"{short_id(j['id'])} \"{j['goal'][:80]}\" - "
                              "let it finish or cancel it instead")
        pend = [j for j in jobs.values() if j["status"] == "pending"]
        cap = max_pending()
        if len(pend) >= cap:
            return None, (f"refused: job queue full ({len(pend)} pending, cap "
                          f"{cap}) - let some drain or cancel one first")
        ev = {"ts": now if now is not None else time.time(), "op": "submit",
              "id": new_id(), "goal": goal,
              "context": str(context or "")[:CONTEXT_CAP],
              "priority": _norm_priority(priority),
              "origin": str(origin or "engine")[:ORIGIN_CAP]}
        _append(self.path, ev)
        return ev, f"job submitted ({short_id(ev['id'])}): {goal}"

    def mark_start(self, id_piece: str,
                   now: float | None = None) -> tuple[dict | None, str]:
        """Claim a PENDING job -> running. Returns (job-or-None, msg). Only the
        pending->running transition is honored, so a double-claim is a no-op
        (the orchestrator still serializes claims under its own lock)."""
        job, err = resolve_id(self.fold(), id_piece)
        if job is None:
            return None, "refused: " + err
        if job["status"] != "pending":
            return None, f"job {short_id(job['id'])} is already {job['status']}"
        ev = {"ts": now if now is not None else time.time(), "op": "start",
              "id": job["id"]}
        _append(self.path, ev)
        return job, f"job {short_id(job['id'])} started"

    def mark_complete(self, job_id: str, result: str, usd: float = 0.0,
                      now: float | None = None) -> str:
        _append(self.path, {"ts": now if now is not None else time.time(),
                            "op": "complete", "id": job_id,
                            "result": str(result or "")[:RESULT_CAP],
                            "usd": round(float(usd or 0.0), 6)})
        return f"job {short_id(job_id)} complete"

    def mark_fail(self, job_id: str, error: str, usd: float = 0.0,
                  now: float | None = None) -> str:
        _append(self.path, {"ts": now if now is not None else time.time(),
                            "op": "fail", "id": job_id,
                            "error": str(error or "")[:ERROR_CAP],
                            "usd": round(float(usd or 0.0), 6)})
        return f"job {short_id(job_id)} failed"

    def cancel(self, id_piece: str, note: str = "",
               now: float | None = None) -> str:
        job, err = resolve_id(self.fold(), id_piece)
        if job is None:
            return "refused: " + err
        if job["status"] in TERMINAL:
            return f"job {short_id(job['id'])} is already {job['status']} - nothing to cancel"
        _append(self.path, {"ts": now if now is not None else time.time(),
                            "op": "cancel", "id": job["id"],
                            "note": " ".join(str(note or "").split())[:NOTE_CAP]})
        return f"job {short_id(job['id'])} cancelled"

    # ---- summaries ------------------------------------------------------
    def summarize(self, status: str = "active", max_items: int = 12,
                  now: float | None = None) -> str:
        """Plain one-screen view. status: active|pending|running|done|failed|
        cancelled|all. Default 'active' = pending + running, the working set."""
        now = now if now is not None else time.time()
        jobs = self.fold()
        which = str(status or "active").strip().lower()
        if which == "active":
            chosen = active_jobs(jobs)
            label = "active"
        elif which in STATUSES:
            chosen = sorted((j for j in jobs.values() if j["status"] == which),
                            key=lambda j: j["updated_ts"], reverse=True)
            label = which
        else:
            chosen = sorted(jobs.values(), key=lambda j: j["updated_ts"],
                            reverse=True)
            label = "all"
        if not chosen:
            c = counts(jobs)
            return (f"jobs: none {label} "
                    f"(totals: {c['pending']} pending, {c['running']} running, "
                    f"{c['done']} done, {c['failed']} failed)")
        lines = [f"jobs ({label}): {len(chosen)} shown"]
        for j in chosen[:max_items]:
            age_h = max(0.0, (now - j["ts"]) / 3600.0)
            line = (f"- [{short_id(j['id'])}] ({j['status']}, {j['priority']}, "
                    f"{age_h:.1f}h) {j['goal'][:90]}")
            if j["status"] == "failed" and j["error"]:
                line += f" | error: {j['error'][:100]}"
            lines.append(line)
        if len(chosen) > max_items:
            lines.append(f"  (+{len(chosen) - max_items} more)")
        return "\n".join(lines)

    def result_text(self, id_piece: str) -> str:
        job, err = resolve_id(self.fold(), id_piece)
        if job is None:
            return "no such job: " + err
        head = (f"job {short_id(job['id'])} [{job['status']}] {job['goal']}")
        if job["status"] == "done":
            return head + "\n\nresult:\n" + (job["result"] or "(empty result)")
        if job["status"] == "failed":
            return head + "\n\nfailed: " + (job["error"] or "(no error text)")
        if job["status"] == "cancelled":
            return head + "\n\ncancelled: " + (job["error"] or "")
        return head + f"\n\n(still {job['status']} - no result yet)"
