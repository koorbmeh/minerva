"""Supervisor (round 26.1): ONE window starts ALL daemons and stops them
together. Process lifecycle - start, monitor, crash-restart with a cap +
backoff, clean shutdown - exercised with dummy children (cross-platform; the
Windows job-object path is guarded and no-ops on POSIX).   python tests/test_supervisor.py
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import supervisor as S                                          # noqa: E402

PASSED = 0
PY_SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]
PY_CRASH = [sys.executable, "-c", "import sys; sys.exit(3)"]


def check(name: str, cond: bool) -> None:
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok - {name}")


def test_default_daemons() -> None:
    d = S.default_daemons()
    check("two daemons by default", len(d) == 2)
    labels = [n for n, _ in d]
    check("labels name delivery + orchestrator",
          any("host_loop" in n or "delivery" in n for n in labels)
          and any("orchestrator" in n for n in labels))
    argvs = " ".join(" ".join(a) for _, a in d)
    check("delivery daemon = host_loop.py --watch",
          "host_loop.py" in argvs and "--watch" in argvs)
    check("orchestrator daemon = run_orchestrator.py --watch",
          "run_orchestrator.py" in argvs)
    if sys.platform == "win32":
        job = S._win_kill_on_close_job()
        check("job-object helper returns a kill-on-close handle on Windows",
              job is not None)
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(job)
    else:
        check("job-object helper returns None off-Windows (POSIX safe)",
              S._win_kill_on_close_job() is None)


def test_start_monitor_shutdown() -> None:
    sup = S.Supervisor([("a", PY_SLEEP), ("b", PY_SLEEP)], quiet=True)
    sup.start()
    time.sleep(0.3)
    check("both children spawned and running",
          all(c.running() for c in sup.children))
    st = sup.poll_once()
    check("poll reports running for both", st == {"a": "running", "b": "running"})
    sup.shutdown(grace_s=4)
    check("shutdown terminates every child",
          all(c.proc is not None and c.proc.poll() is not None
              for c in sup.children))
    check("status after shutdown is 'stopping'",
          sup.poll_once() == {"a": "stopping", "b": "stopping"})


def test_crash_restart_cap() -> None:
    sup = S.Supervisor([("crasher", PY_CRASH)], max_restarts=2,
                       restart_window_s=60.0, backoff_s=0.0, quiet=True)
    sup.start()
    time.sleep(0.2)
    seq = []
    for _ in range(5):
        seq.append(sup.poll_once()["crasher"])
        time.sleep(0.12)
    check("a crashed daemon is restarted", "restarted" in seq)
    check("restarts are capped at max_restarts", seq.count("restarted") == 2)
    check("after the cap it gives up (loudly, no thrash)", seq[-1] == "gave_up"
          and sup.children[0].gave_up is True)
    sup.shutdown()


def test_backoff() -> None:
    sup = S.Supervisor([("c", PY_CRASH)], max_restarts=5, backoff_s=10.0,
                       quiet=True)
    sup.start()
    time.sleep(0.2)
    s1 = sup.poll_once()["c"]
    s2 = sup.poll_once()["c"]
    check("backoff blocks an immediate re-restart", s2 == "backoff")
    check("backoff state is reported plainly", s1 in ("backoff", "restarted"))
    sup.shutdown()


def test_run_stop_check() -> None:
    sup = S.Supervisor([("d", PY_SLEEP)], quiet=True)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] >= 3
    rc = sup.run(stop_check=stop, poll_s=0.05)
    check("run() returns 0 on stop_check", rc == 0)
    check("run() took everything down on exit",
          all(c.proc is not None and c.proc.poll() is not None
              for c in sup.children))
    sup.shutdown()           # idempotent
    check("shutdown is idempotent (no raise on a second call)", True)


def test_launcher_wiring() -> None:
    bat = os.path.join(ROOT, "Start Minerva.bat")
    check("Start Minerva.bat exists", os.path.exists(bat))
    txt = open(bat, encoding="utf-8").read()
    check("Start Minerva launches the supervisor (one window = everything)",
          "supervisor.py" in txt)
    orch = os.path.join(ROOT, "Minerva - Start Orchestrator.bat")
    if os.path.exists(orch):
        otxt = open(orch, encoding="utf-8").read()
        check("standalone orchestrator launcher marked optional",
              "optional" in otxt.lower() or "standalone" in otxt.lower())
    else:
        check("standalone orchestrator launcher marked optional", True)


def test_bad_binary_tolerated() -> None:
    sup = S.Supervisor([("bad", ["/nonexistent/minerva/xyz"])], quiet=True)
    sup.start()                                  # must not raise
    st = sup.poll_once()
    check("a daemon that cannot start never crashes the supervisor",
          "bad" in st)
    check("children keep isolated restart bookkeeping",
          isinstance(sup.children[0].restarts, list))
    sup.shutdown()


if __name__ == "__main__":
    test_default_daemons()
    test_start_monitor_shutdown()
    test_crash_restart_cap()
    test_backoff()
    test_run_stop_check()
    test_launcher_wiring()
    test_bad_binary_tolerated()
    print(f"\nall {PASSED} checks passed")
