#!/usr/bin/env python3
"""
Test for the lock file that keeps push_all.py and stock_sync.py from running twice at once.

A run that is killed (a systemd timeout, a reboot) never removes its lock file. Before, every later run
(stock sync runs every 15 minutes) was refused for 6 hours. Now a lock whose run is no longer running is taken
over at once, while a lock held by a live process still blocks. Real child processes are used, including a
hard kill. Nothing outside a temporary folder is touched.

    python tests/test_lock.py
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import push_all  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])


def try_lock():
    """(took the lock?, text printed or the refusal message)"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            push_all.take_lock()
            return True, buf.getvalue()
        except SystemExit as e:
            return False, str(e)


def age(path, hours):
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n=== lock file (push_all.py / stock_sync.py) ===")
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "push_all.lock"
        push_all.LOCK_FILE = lock

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        check("pid_alive: this process is alive, a finished process and bad ids are not",
              push_all.pid_alive(os.getpid()) and not push_all.pid_alive(dead.pid)
              and not push_all.pid_alive(0) and not push_all.pid_alive(-5))

        took, msg = try_lock()
        check("no lock: taken, holds our PID", took and lock.read_text() == str(os.getpid()), msg)
        lock.unlink()

        live = sleeper()
        lock.write_text(str(live.pid))
        took, msg = try_lock()
        check("lock held by a live process: refused", not took and "in progress" in msg, msg)
        check("... and left in place", lock.read_text() == str(live.pid))

        age(lock, 7)
        took, msg = try_lock()
        check("lock older than LOCK_MAX_AGE (6 h) is taken over even if the process lives (a hung run)",
              took and lock.read_text() == str(os.getpid()), msg)
        lock.unlink()
        live.kill()
        live.wait()

        # a run that takes the lock and is then killed hard: no 'finally', like systemd's timeout kill
        code = ("import sys; from pathlib import Path; sys.path.insert(0, %r); import push_all, time; "
                "push_all.LOCK_FILE = Path(%r); push_all.take_lock(); print('locked', flush=True); time.sleep(300)"
                % (str(ROOT), str(lock)))
        victim = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        victim.stdout.readline()
        check("a real run holds the lock with its PID", lock.exists() and lock.read_text() == str(victim.pid))
        took, msg = try_lock()
        check("... and blocks while it is running", not took and "in progress" in msg, msg)
        victim.kill()
        victim.wait()
        check("hard-killed run leaves its lock behind", lock.exists())
        took, msg = try_lock()
        check("lock of a killed run: taken over at once, with a note", took and f"left by run {victim.pid}" in msg
              and lock.read_text() == str(os.getpid()), msg)
        lock.unlink()

        lock.write_text("not a number")
        took, msg = try_lock()
        check("unreadable lock content, fresh: refused (cannot tell if the run is alive)", not took, msg)
        age(lock, 7)
        took, msg = try_lock()
        check("unreadable lock content, older than 6 h: taken over", took, msg)
        lock.unlink()

        lock.write_text("")
        took, msg = try_lock()
        check("empty lock (run died between creating and writing it), fresh: refused", not took, msg)
        lock.unlink()

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
