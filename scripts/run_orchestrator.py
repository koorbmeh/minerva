"""Orchestrator daemon (round 26) - drains Minerva's job queue continuously.

A STANDALONE process, separate from host_loop and the drive tick (composable by
design): every MINERVA_ORCHESTRATOR_POLL_S (default 30s) it asks the Orchestrator
to drain all PENDING jobs through a bounded thread pool. Each job runs as its own
governed BYOK agent session in its own isolated state dir (own ledger); on
completion the orchestrator writes a NOTE to the MAIN ledger (under the shared
engine lock) and queues a result DM to the operator's outbox - host_loop --watch
delivers it to Discord, exactly like pitches and the morning brief.

Jobs reach the queue via the job_submit gateway tool (chat lane, or a tick, or a
job decomposing itself), or the CLI here (--submit). Jobs run on the BYOK lane
ONLY (privacy + capability); this daemon warns loudly at startup if no key is
configured, but still runs - queued jobs simply fail with an honest "no key"
result until config/anthropic.env is filled.

Rails: the kill switch (state/STOP) makes every drain a no-op; per-job budget
pre-check + timeout live in agent_runner; MINERVA_ORCHESTRATOR_WORKERS bounds
concurrency. Never dies on a drain exception (logs and continues, like the
other daemons).

Usage:
  python scripts\\run_orchestrator.py --watch        the daemon (default)
  python scripts\\run_orchestrator.py --once          one drain pass, then exit
  python scripts\\run_orchestrator.py --dry-run        show queue + config, run nothing
  python scripts\\run_orchestrator.py --submit "goal"  enqueue a job from the CLI

Artifacts: state/orchestrator-log.jsonl (one line per drain pass that did work)
+ state/orchestrator-status.json (last heartbeat). Stdlib only. ASCII console.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from minerva_core import jobs as jobs_mod                  # noqa: E402
from minerva_core.killswitch import is_stopped             # noqa: E402
from minerva_core.orchestrator import Orchestrator, max_drain, workers  # noqa: E402

STATE = os.path.join(ROOT, "state")
LOG_NAME = "orchestrator-log.jsonl"
STATUS_NAME = "orchestrator-status.json"
DEFAULT_POLL_S = 30.0


def poll_s() -> float:
    try:
        return float(os.environ.get("MINERVA_ORCHESTRATOR_POLL_S", "").strip()
                     or DEFAULT_POLL_S)
    except ValueError:
        return DEFAULT_POLL_S


# ---- singleton guard: only ONE orchestrator daemon may drain at a time -------
LOCK_NAME = "orchestrator.lock"
LOCK_MAX_AGE_S = 86400.0          # a lock older than this is reclaimed regardless


def _pid_alive(pid: int) -> bool:
    """Is process `pid` running? PID-liveness (not a timeout) so a crashed
    orchestrator's lock is reclaimable immediately - the supervisor restarts
    fast and must not be blocked by a dead instance's leftover lock."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong(0)
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259   # STILL_ACTIVE
        except Exception:                                       # noqa: BLE001
            return True                             # uncertain -> don't double-run
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_singleton(state_dir: str) -> bool:
    """Atomically claim the orchestrator singleton lock. Returns False if a
    DIFFERENT, LIVE orchestrator already holds it. Reclaims a dead/stale lock."""
    path = os.path.join(state_dir, LOCK_NAME)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            with open(path, encoding="utf-8") as f:
                held = json.load(f)
            pid = int(held.get("pid", 0))
            ts = float(held.get("ts", 0.0))
        except (OSError, json.JSONDecodeError, ValueError):
            pid, ts = 0, 0.0
        if (pid and pid != os.getpid() and _pid_alive(pid)
                and (time.time() - ts) < LOCK_MAX_AGE_S):
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        return acquire_singleton(state_dir)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    return True


def release_singleton(state_dir: str) -> None:
    try:
        os.remove(os.path.join(state_dir, LOCK_NAME))
    except OSError:
        pass


def _ascii(s: str) -> str:
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


def _write_status(state_dir: str, **extra) -> None:
    """The daemon's pulse (never raises)."""
    rec = {"ts": time.time(), "pid": os.getpid()}
    rec.update(extra)
    try:
        with open(os.path.join(state_dir, STATUS_NAME), "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump(rec, f)
    except OSError:
        pass


def _log(state_dir: str, rec: dict) -> None:
    try:
        with open(os.path.join(state_dir, LOG_NAME), "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=True, sort_keys=True) + "\n")
    except OSError:
        pass


def drain_once(state_dir: str, n_workers: int | None = None,
               quiet: bool = False) -> dict:
    orc = Orchestrator(state_dir, n_workers=n_workers, quiet=quiet)
    summary = orc.drain()
    _write_status(state_dir, last_drain=summary.get("dispatched", 0),
                  last_done=summary.get("done", 0),
                  skipped=summary.get("skipped", ""))
    if summary.get("dispatched") or summary.get("skipped") == "killswitch":
        _log(state_dir, {"ts": time.time(),
                         "dispatched": summary.get("dispatched", 0),
                         "done": summary.get("done", 0),
                         "failed": summary.get("failed", 0),
                         "usd": summary.get("total_usd", 0.0),
                         "skipped": summary.get("skipped", ""),
                         "duration_s": summary.get("duration_s", 0.0)})
    return summary


def watch(state_dir: str, interval: float, n_workers: int | None,
          quiet: bool = False) -> int:
    if not acquire_singleton(state_dir):
        print("another orchestrator is already running (Start Minerva includes "
              "it) - this window is not needed.", file=sys.stderr)
        return 0
    nw = n_workers if n_workers is not None else workers()
    print("=" * 52)
    print(" MINERVA ORCHESTRATOR IS ON")
    print()
    print("  Background jobs you (or Minerva) delegate get run here")
    print(f"  - up to {nw} at a time, results texted to you when done.")
    print(f"  - polling the queue every {int(interval)}s.")
    print("  Close this window (or Ctrl+C) to stop running jobs.")
    print("=" * 52)
    sys.stdout.flush()
    try:
        while True:
            try:
                if is_stopped(state_dir):
                    _write_status(state_dir, stopped=True)
                else:
                    s = drain_once(state_dir, n_workers=nw, quiet=quiet)
                    if s.get("dispatched"):
                        print(f"[{time.strftime('%H:%M:%S')}] drained "
                              f"{s.get('done')}/{s.get('dispatched')} "
                              f"(${s.get('total_usd', 0)}, {s.get('duration_s')}s)")
                        sys.stdout.flush()
            except Exception as exc:                            # noqa: BLE001
                _log(state_dir, {"ts": time.time(),
                                 "error": _ascii(repr(exc))[:300]})
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\norchestrator off.")
    finally:
        release_singleton(state_dir)
    return 0


def main() -> int:
    env = os.environ
    ap = argparse.ArgumentParser(prog="minerva-orchestrator")
    ap.add_argument("--state-dir", default=env.get("MINERVA_STATE_DIR") or STATE)
    ap.add_argument("--once", action="store_true",
                    help="one drain pass, then exit")
    ap.add_argument("--watch", action="store_true",
                    help="daemon: poll + drain forever (default)")
    ap.add_argument("--poll", type=float, default=None,
                    help="watch poll seconds (default MINERVA_ORCHESTRATOR_POLL_S/30)")
    ap.add_argument("--workers", type=int, default=None,
                    help="max concurrent jobs (default MINERVA_ORCHESTRATOR_WORKERS/3)")
    ap.add_argument("--submit", default=None,
                    help="enqueue one job goal from the CLI and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the queue + config; run nothing")
    args = ap.parse_args()
    state_dir = args.state_dir
    os.makedirs(state_dir, exist_ok=True)
    queue = jobs_mod.JobQueue(jobs_mod.jobs_path(state_dir))

    try:
        from minerva_core.adapters.anthropic_byok import load_api_key
        has_key = bool(load_api_key(env.get("MINERVA_ANTHROPIC_ENV") or None))
    except Exception:                                           # noqa: BLE001
        has_key = False

    if args.submit:
        ev, msg = queue.submit(args.submit, origin="operator")
        print(msg)
        return 0 if ev is not None else 1

    if args.dry_run:
        c = queue.counts()
        print(f"orchestrator dry-run (state: {state_dir})")
        print(f"  workers: {args.workers or workers()}  poll: "
              f"{args.poll or poll_s()}s  max/drain: {max_drain()}")
        print(f"  BYOK key: {'present' if has_key else 'MISSING (jobs will fail)'}")
        print(f"  kill switch: {'ON' if is_stopped(state_dir) else 'off'}")
        print(f"  queue: {c['pending']} pending, {c['running']} running, "
              f"{c['done']} done, {c['failed']} failed")
        print(queue.summarize("active"))
        return 0

    if not has_key:
        print("WARNING: no BYOK key (config/anthropic.env). Jobs run on the "
              "paid lane only - queued jobs will FAIL with an honest 'no key' "
              "result until you fill it.", file=sys.stderr)

    if args.once:
        s = drain_once(state_dir, n_workers=args.workers, quiet=False)
        print(f"drain: dispatched {s.get('dispatched', 0)}, done "
              f"{s.get('done', 0)}, failed {s.get('failed', 0)}, "
              f"${s.get('total_usd', 0)} ({s.get('skipped', '') or 'ok'})")
        return 0

    return watch(state_dir, args.poll if args.poll is not None else poll_s(),
                 args.workers)


if __name__ == "__main__":
    sys.exit(main())
