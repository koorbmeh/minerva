"""Run Minerva's test suite: every tests/test_*.py in its own process.

Each test file is a plain script that prints "all N checks passed" as its last word when it
succeeds. A file counts as passed only when it exits 0 AND reports its checks, so a file that
stops early without failing (a truncated test, say) still fails the run.

Usage:  python run_tests.py            exit 0 when every file passes, 1 otherwise
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PASSED = re.compile(r"all (\d+) checks passed")
TIMEOUT_S = 600


def main() -> int:
    files = sorted((ROOT / "tests").glob("test_*.py"))
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    failed, checks = [], 0
    for f in files:
        try:
            p = subprocess.run([sys.executable, str(f)], cwd=ROOT, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
            out, code = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired:
            out, code = "timed out after %d s" % TIMEOUT_S, "timeout"
        m = PASSED.search(out)
        if code == 0 and m:
            checks += int(m.group(1))
            print("ok    %-34s %s checks" % (f.name, m.group(1)))
        else:
            failed.append(f.name)
            why = "exit %s" % code if code != 0 else "exited 0 without reporting its checks"
            print("FAIL  %-34s %s" % (f.name, why))
            for line in out.strip().splitlines()[-8:]:
                print("      " + line)
    if not files:
        print("no test files found under tests/")
    ok = len(files) - len(failed)
    print("run_tests: %d/%d files passed, %d checks, %d failed" % (ok, len(files), checks, len(failed)))
    return 0 if files and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
