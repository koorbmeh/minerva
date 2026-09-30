"""Orchestrator (round 26) - the bounded thread-pool that drains the job queue.

This is the "spin up many agents on a barely finite number of tasks" mechanism
from the vision, governed: it claims PENDING jobs from the JobQueue (claiming is
serialized in this single dispatcher thread, so two workers never grab the same
job), runs each as its OWN governed BYOK session via agent_runner (isolated
state dir = isolated ledger, real concurrency), and on completion writes a NOTE
to the MAIN ledger and DMs the operator the result.

Composability: this is a SEPARATE process from host_loop and the drive tick
(scripts/run_orchestrator.py is the daemon). Each job gets its own gateway in
its own state dir, so jobs need no shared lock among themselves. The ONLY thing
that must stay single-appender is the MAIN ledger - so the completion NOTE is
written while holding the SAME engine lock (state/drive-tick.lock) the tick,
chat, and morning brief take, mutually excluding it from those main-state-dir
gateways. If the lock is busy, the NOTE is skipped (logged) rather than forking
the chain - the job's own ledger and jobs.jsonl keep the full record either way.

Decomposition rides the same queue: a job's session may job_submit sub-jobs
(capped per session + by MAX_PENDING), which this drainer picks up on its next
claim pass - recursion through the queue, bounded by the same anti-flood rails.

Rails: kill switch (state/STOP) stops claiming and dispatch; the per-job budget
pre-check + timeout live in agent_runner. MINERVA_ORCHESTRATOR_WORKERS bounds
concurrency (default 3); MINERVA_ORCHESTRATOR_MAX_DRAIN bounds jobs per drain
pass (default 50) so a runaway sub-job loop cannot spin forever.

Never raises out of drain(). Stdlib only. ASCII (cp1252 rule).
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import agent_runner
from . import jobs as jobs_mod
from .killswitch import is_stopped
from .ledger import NOTE, Ledger

DEFAULT_WORKERS = 3
DEFAULT_MAX_DRAIN = 50
ENGINE_LOCK_NAME = "drive-tick.lock"     # SHARED main-ledger serialization
LOCK_STALE_S = 3600.0
NOTE_LOCK_TRIES = 6
NOTE_LOCK_WAIT_S = 0.4
DM_CAP = 1700                            # host_loop DM headroom


def workers() -> int:
    try:
        n = int(os.environ.get("MINERVA_ORCHESTRATOR_WORKERS", "").strip()
                or DEFAULT_WORKERS)
    except ValueError:
        n = DEFAULT_WORKERS
    return max(1, min(n, 16))


def max_drain() -> int:
    try:
        return int(os.environ.get("MINERVA_ORCHESTRATOR_MAX_DRAIN", "").strip()
                   or DEFAULT_MAX_DRAIN)
    except ValueError:
        return DEFAULT_MAX_DRAIN


def notify_enabled() -> bool:
    return (os.environ.get("MINERVA_ORCHESTRATOR_NOTIFY", "1").strip().lower()
            not in ("0", "off", "no", "false"))


def _ascii(s: str) -> str:
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


# ---- the SHARED engine lock (same file + protocol as drive_tick) -----------
def _acquire_lock(path: str, stale_s: float = LOCK_STALE_S) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            with open(path, encoding="utf-8") as f:
                held = json.load(f)
            age = time.time() - float(held.get("ts", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            age = stale_s + 1
        if age <= stale_s:
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        return _acquire_lock(path, stale_s)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"pid": os.getpid(), "ts": time.time(), "by": "orchestrator"}, f)
    return True


def _release_lock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class Orchestrator:
    def __init__(self, main_state_dir: str, queue=None, engine_factory=None,
                 n_workers: int | None = None, notify: bool | None = None,
                 quiet: bool = True) -> None:
        self.state_dir = main_state_dir
        self.queue = queue or jobs_mod.JobQueue(jobs_mod.jobs_path(main_state_dir))
        self.engine_factory = engine_factory
        self.n_workers = n_workers if n_workers is not None else workers()
        self.notify = notify_enabled() if notify is None else notify
        self.quiet = quiet
        self.outbox_path = os.path.join(main_state_dir, "outbox.jsonl")
        self.ledger_path = os.path.join(main_state_dir, "ledger.jsonl")
        self.lock_path = os.path.join(main_state_dir, ENGINE_LOCK_NAME)

    # ---- claiming (single dispatcher thread, so no lock needed) ----------
    def _claim_next(self) -> dict | None:
        for job in self.queue.pending():
            claimed, _ = self.queue.mark_start(job["id"])
            if claimed is not None:
                return self.queue.fold().get(job["id"], claimed)
        return None

    def _run(self, job: dict) -> dict:
        return agent_runner.run_job(job, self.queue, self.state_dir,
                                    engine_factory=self.engine_factory,
                                    quiet=self.quiet)

    # ---- completion side-effects -----------------------------------------
    def _note_completion(self, job: dict, report: dict) -> bool:
        """Append a NOTE to the MAIN ledger under the shared engine lock.
        Returns True if written. Skipped (not fatal) if the lock is busy."""
        got = False
        for _ in range(NOTE_LOCK_TRIES):
            if _acquire_lock(self.lock_path):
                got = True
                break
            time.sleep(NOTE_LOCK_WAIT_S)
        if not got:
            return False
        try:
            status = report.get("status", "?")
            body = (report.get("result") or
                    ("; ".join(report.get("errors", [])) if status != "done"
                     else "")) or "(no detail)"
            text = (f"Job {jobs_mod.short_id(job['id'])} [{status}] "
                    f"\"{str(job.get('goal',''))[:120]}\" -> {body[:400]}")
            Ledger(self.ledger_path).append(
                NOTE, {"text": _ascii(text)[:2000], "about": "job",
                       "source": "orchestrator", "job_id": job["id"],
                       "status": status,
                       "usd": round(float(report.get("usd", 0.0) or 0.0), 6)})
            return True
        except Exception:                                       # noqa: BLE001
            return False
        finally:
            _release_lock(self.lock_path)

    def _dm(self, job: dict, report: dict) -> None:
        status = report.get("status", "?")
        goal = str(job.get("goal", ""))[:120]
        if status == "done":
            head = f"[job] Done: {goal}"
            body = report.get("result", "")
        else:
            head = f"[job] {status.upper()}: {goal}"
            body = "; ".join(report.get("errors", [])) or "(no detail)"
        msg = _ascii((head + "\n" + body).strip())[:DM_CAP]
        try:
            with open(self.outbox_path, "a", encoding="utf-8",
                      newline="\n") as f:
                f.write(json.dumps({"ts": time.time(), "message": msg,
                                    "priority": "normal"},
                                   ensure_ascii=True) + "\n")
        except OSError:
            pass

    def _finish(self, fut, job: dict, results: list) -> None:
        try:
            report = fut.result()
        except Exception as exc:                                # noqa: BLE001
            report = {"job_id": job["id"], "ok": False, "status": "failed",
                      "errors": [f"worker crashed: {exc!r}"[:300]], "usd": 0.0}
            try:
                self.queue.mark_fail(job["id"], report["errors"][0])
            except Exception:                                   # noqa: BLE001
                pass
        report["note_written"] = self._note_completion(job, report)
        if self.notify:
            self._dm(job, report)
        results.append(report)
        if not self.quiet:
            print(f"  job {jobs_mod.short_id(job['id'])}: {report.get('status')}"
                  f" (${report.get('usd', 0)})")

    # ---- the drain pass ---------------------------------------------------
    def drain(self, max_jobs: int | None = None) -> dict:
        """Run every currently-pending job (and any sub-jobs they submit)
        through the pool, bounded by n_workers concurrency and max_jobs total.
        Returns a summary. Never raises."""
        t0 = time.time()
        results: list[dict] = []
        cap = max_jobs if max_jobs is not None else max_drain()
        if is_stopped(self.state_dir):
            return {"ok": True, "dispatched": 0, "skipped": "killswitch",
                    "results": results, "duration_s": 0.0}
        dispatched = 0
        try:
            with ThreadPoolExecutor(max_workers=self.n_workers) as ex:
                inflight: dict = {}
                while True:
                    if is_stopped(self.state_dir):
                        break
                    while (len(inflight) < self.n_workers and dispatched < cap):
                        job = self._claim_next()
                        if job is None:
                            break
                        inflight[ex.submit(self._run, job)] = job
                        dispatched += 1
                    if not inflight:
                        break
                    done, _ = wait(inflight, timeout=2.0,
                                   return_when=FIRST_COMPLETED)
                    for fut in done:
                        self._finish(fut, inflight.pop(fut), results)
        except Exception as exc:                                # noqa: BLE001
            return {"ok": False, "dispatched": dispatched,
                    "error": repr(exc)[:300], "results": results,
                    "duration_s": round(time.time() - t0, 2)}
        ndone = sum(1 for r in results if r.get("status") == "done")
        summary = {"ok": True, "dispatched": dispatched, "done": ndone,
                   "failed": dispatched - ndone, "results": results,
                   "total_usd": round(sum(float(r.get("usd", 0.0) or 0.0)
                                          for r in results), 6),
                   "hit_cap": dispatched >= cap,
                   "duration_s": round(time.time() - t0, 2)}
        if not self.quiet:
            print(f"drain: {ndone}/{dispatched} done, "
                  f"${summary['total_usd']}, {summary['duration_s']}s")
        return summary
