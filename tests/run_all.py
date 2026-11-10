#!/usr/bin/env python3
"""
Run every test suite (tests/test_*.py) one after another and print one summary.
Exit code 0 only if every suite ran and every check passed. Used by the
GitHub Actions workflow (.github/workflows/tests.yml) and handy locally:

    python tests/run_all.py            # all suites
    python tests/run_all.py ebay cdon  # only suites whose name contains these words
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TIMEOUT = 15 * 60          # per suite
SUMMARY = re.compile(r"(\d+) passed, (\d+) failed")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    words = [w.lower() for w in sys.argv[1:]]
    suites = sorted(p for p in HERE.glob("test_*.py") if not words or any(w in p.stem for w in words))
    if not suites:
        sys.exit("No test suites found.")
    path_env = os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH", "")) if p)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=path_env)
    total_ok = total_bad = 0
    broken = []
    start = time.time()
    for path in suites:
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
            out, code = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            out, code = "", "timeout"
        found = SUMMARY.findall(out)
        passed, failed = (int(found[-1][0]), int(found[-1][1])) if found else (0, 0)
        total_ok, total_bad = total_ok + passed, total_bad + failed
        bad = code != 0 or failed or not found
        print(f"{'FAIL' if bad else 'ok  '}  {path.stem:<24} {passed:>4} passed, {failed} failed"
              f"  ({time.time() - t0:.0f}s)" + (f"  exit {code}" if code != 0 else ""), flush=True)
        if bad:
            broken.append(path.stem)
            lines = [l for l in out.splitlines() if l.strip()]
            fails = [l for l in lines if "FAIL" in l or "Error" in l or "Traceback" in l]
            for l in (fails or lines)[-25:]:
                print(f"        {l}")
    print(f"\n{total_ok} passed, {total_bad} failed in {len(suites)} suites ({time.time() - start:.0f}s)")
    if broken:
        print("Failing suites: " + ", ".join(broken))
        sys.exit(1)


if __name__ == "__main__":
    main()
