"""Supervisor (round 26.1) - ONE window runs ALL of Minerva's daemons.

Before this, "Start Minerva" ran only the delivery daemon (host_loop --watch)
and the orchestrator (run_orchestrator --watch) was a SECOND window you had to
remember to open. Now a single supervisor starts BOTH (and any future daemon),
shows their output in the one console, RESTARTS a daemon that crashes (backoff +
a sane cap), and - the whole point - TAKES THEM ALL DOWN when you close the
window or press Ctrl+C. One start, one stop.

NOT managed here (on purpose):
  * the scheduled brainstorm ticks / 06:50 morning brief / 15-min watchdog are
    Windows scheduled TASKS (schtasks) - they fire on their own, no window;
  * the kill switch (state/STOP via the STOP/Resume launchers) is the SOFTER
    control - it PAUSES her actions (every daemon no-ops, no spend) WITHOUT
    closing anything. Close-the-window is the hard stop; STOP is the pause.

How "everything dies together" is guaranteed:
  * Windows: the children are placed in a JOB OBJECT with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. When this supervisor exits - even a hard
    window-close that kills it WITHOUT a clean shutdown - the OS closes the job
    handle and reaps every child. No orphaned python.exe lingering with the mic.
  * POSIX (tests/dev): children run in a NEW process group and are terminated
    (SIGTERM -> SIGKILL) on shutdown.
Either way a clean Ctrl+C also terminates them directly.

Stdlib only. ASCII console (cp1252 rule).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

DEFAULT_MAX_RESTARTS = 20          # within the rolling window, then give up loudly
DEFAULT_RESTART_WINDOW_S = 120.0
DEFAULT_BACKOFF_S = 3.0


def default_daemons() -> list[tuple[str, list[str]]]:
    """(label, argv) for every continuous daemon Start Minerva should run.
    Add a future daemon here and it joins the single start/stop automatically."""
    py = sys.executable or "python"
    return [
        ("delivery (host_loop)",
         [py, os.path.join(SCRIPTS, "host_loop.py"), "--watch"]),
        ("orchestrator",
         [py, os.path.join(SCRIPTS, "run_orchestrator.py"), "--watch"]),
    ]


# ---------- Windows job object (kill-on-close) -------------------------------
def _win_kill_on_close_job():
    """A job handle whose closure (incl. our death) kills every assigned child.
    Returns the handle, or None if anything goes wrong (we degrade to
    terminate-on-clean-exit only)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC),
                        ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32.CreateJobObjectW.restype = wt.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9=ExtendedLimit
            return None
        return job
    except Exception:                                          # noqa: BLE001
        return None


def _win_assign(job, proc) -> None:
    if job is None or sys.platform != "win32":
        return
    try:
        import ctypes
        handle = int(getattr(proc, "_handle", 0))
        if handle:
            ctypes.WinDLL("kernel32").AssignProcessToJobObject(job, handle)
    except Exception:                                          # noqa: BLE001
        pass


class _Child:
    def __init__(self, name: str, argv: list[str]) -> None:
        self.name = name
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.restarts: list[float] = []     # ts of each RE-start (not the first)
        self.last_attempt = 0.0
        self.gave_up = False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Supervisor:
    def __init__(self, daemons: list[tuple[str, list[str]]] | None = None,
                 max_restarts: int = DEFAULT_MAX_RESTARTS,
                 restart_window_s: float = DEFAULT_RESTART_WINDOW_S,
                 backoff_s: float = DEFAULT_BACKOFF_S, quiet: bool = False) -> None:
        src = daemons if daemons is not None else default_daemons()
        self.children = [_Child(n, list(a)) for n, a in src]
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.backoff_s = backoff_s
        self.quiet = quiet
        self._job = None
        self._shutting_down = False

    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(msg)
            sys.stdout.flush()

    def _spawn(self, c: _Child, now: float) -> None:
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True       # own process group for clean kill
        try:
            c.proc = subprocess.Popen(c.argv, **kw)
            c.last_attempt = now
            _win_assign(self._job, c.proc)
        except Exception as exc:                                # noqa: BLE001
            c.proc = None
            self._say(f"  ! could not start {c.name}: {exc!r}")

    def start(self) -> None:
        self._job = _win_kill_on_close_job()
        now = time.time()
        for c in self.children:
            self._spawn(c, now)
            if c.running():
                self._say(f"  started: {c.name} (pid {c.proc.pid})")

    def poll_once(self, now: float | None = None) -> dict:
        """Check every child; restart a crashed one (backoff + cap). Returns a
        per-child status dict - the unit the watch loop and the tests drive."""
        now = time.time() if now is None else now
        status: dict[str, str] = {}
        for c in self.children:
            if self._shutting_down:
                status[c.name] = "stopping"
                continue
            if c.gave_up:
                status[c.name] = "gave_up"
                continue
            if c.running():
                status[c.name] = "running"
                continue
            # not running -> it exited (crash or never started)
            recent = [t for t in c.restarts if now - t < self.restart_window_s]
            if len(recent) >= self.max_restarts:
                c.gave_up = True
                self._say(f"  !! {c.name} crashed {len(recent)}x in "
                          f"{int(self.restart_window_s)}s - giving up on it. "
                          "Fix it, then restart this window.")
                status[c.name] = "gave_up"
                continue
            if now - c.last_attempt < self.backoff_s:
                status[c.name] = "backoff"          # wait before respawning
                continue
            rc = c.proc.poll() if c.proc is not None else None
            self._say(f"  ~ {c.name} exited (rc={rc}); restarting")
            self._spawn(c, now)
            c.restarts.append(now)
            status[c.name] = "restarted"
        return status

    def shutdown(self, grace_s: float = 6.0) -> None:
        """Terminate every child (idempotent). The Windows job object reaps any
        survivor on our exit regardless."""
        self._shutting_down = True
        for c in self.children:
            p = c.proc
            if p is None or p.poll() is not None:
                continue
            try:
                if sys.platform == "win32":
                    p.terminate()
                else:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:                                   # noqa: BLE001
                try:
                    p.terminate()
                except Exception:                               # noqa: BLE001
                    pass
        deadline = time.time() + grace_s
        for c in self.children:
            p = c.proc
            if p is None:
                continue
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:                                   # noqa: BLE001
                try:
                    p.kill()
                except Exception:                               # noqa: BLE001
                    pass

    def run(self, stop_check=None, poll_s: float = 1.0) -> int:
        """Start all daemons, monitor + restart until Ctrl+C / window close /
        stop_check, then take them all down together."""
        self.start()
        try:
            while True:
                if stop_check is not None and stop_check():
                    break
                self.poll_once()
                time.sleep(poll_s)
        except KeyboardInterrupt:
            self._say("\nstopping everything...")
        finally:
            self.shutdown()
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="minerva-supervisor")
    ap.add_argument("--poll", type=float, default=2.0, help="monitor poll seconds")
    ap.add_argument("--only", default="", help="comma labels to run a subset "
                    "(delivery,orchestrator); default = all")
    args = ap.parse_args()

    daemons = default_daemons()
    if args.only.strip():
        want = {s.strip().lower() for s in args.only.split(",") if s.strip()}
        daemons = [(n, a) for n, a in daemons
                   if any(w in n.lower() for w in want)]
        if not daemons:
            print("no daemons matched --only; nothing to run", file=sys.stderr)
            return 2

    print("=" * 54)
    print(" MINERVA IS ON  (everything runs in THIS one window)")
    print()
    print("  Running together:")
    for n, _ in daemons:
        print(f"   - {n}")
    print()
    print("  - she texts you (Discord) + shows it here; pitches, mail")
    print("    alerts, approvals, chat, and background-job results all flow.")
    print("  - if a piece crashes it is restarted automatically.")
    print()
    print("  CLOSE THIS WINDOW (or press Ctrl+C) to turn EVERYTHING off.")
    print("  (To just PAUSE her without closing: Minerva - STOP.)")
    print("=" * 54)
    sys.stdout.flush()
    return Supervisor(daemons, quiet=False).run(poll_s=args.poll)


if __name__ == "__main__":
    sys.exit(main())
