#!/usr/bin/env python3
"""
Safety tests for push_all.py, the daily unattended run: what happens when something goes wrong.

  * the stock sync fails: nothing is pushed from out-of-date stock (a sync that still wrote a fresh
    books_synced.json is fine)
  * a typo in the config, or a crash in one pusher, must not stop the run or the report
  * a marketplace that is switched on but has lost its credentials shows up in the subject and triggers an email
  * a very big log must not stop the report email
  * a wrong products path is reported, not discovered 26 times
  * the stock sync runs again between pushers in a long run
  * dry runs, and .env lines with comments

Everything runs in temporary folders with fake pushers and a fake mail server. Nothing is pushed or emailed.

    python tests/test_push_all_safety.py
"""
import email
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from email import policy
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
RESULTS = []
MAILS = []
SMTP_LIMIT = [10 ** 9]


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


class FakeSMTP(socketserver.StreamRequestHandler):
    def handle(self):
        send = lambda s: self.wfile.write((s + "\r\n").encode())
        send("220 fake")
        while True:
            line = self.rfile.readline().decode(errors="replace").rstrip("\r\n")
            if not line:
                return
            cmd = line.upper()
            if cmd.startswith(("EHLO", "HELO")):
                send("250-fake"); send("250 AUTH PLAIN")
            elif cmd.startswith("AUTH PLAIN"):
                send("235 ok")
            elif cmd == "DATA":
                send("354 go")
                data = b""
                while True:
                    l = self.rfile.readline()
                    if l in (b".\r\n", b""):
                        break
                    data += l
                if len(data) > SMTP_LIMIT[0]:
                    send("552 message size exceeds the limit")
                else:
                    MAILS.append(email.message_from_bytes(data, policy=policy.default))
                    send("250 queued")
            elif cmd == "QUIT":
                send("221 bye"); return
            else:
                send("250 ok")


server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSMTP)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]

ENV_TEXT = (f"GMAIL_ADDRESS=sender@gmail.com\nGMAIL_APP_PASSWORD=abcdabcdabcdabcd\nSMTP_HOST=127.0.0.1\nSMTP_PORT={PORT}\n"
            "VINTED_PRO_TOKEN=a,b\nAMAZON_LWA_CLIENT_ID=x\nAMAZON_LWA_CLIENT_SECRET=x\nAMAZON_REFRESH_TOKEN=x\nAMAZON_SELLER_ID=x\n"
            "GALAXUS_SFTP_USER=x\nGALAXUS_SFTP_PASSWORD=x\nGALAXUS_PROVIDER=Shop\n")
# a fake pusher: prints the products file it was given, and leaves a marker file so we can tell it ran
MARKER = ("import sys\nfrom pathlib import Path\nprint('ARGV', ' '.join(sys.argv[1:]))\n"
          "Path('ran_' + Path(sys.argv[0]).stem).write_text('1')\n")
SYNC_OK = "print('0 new sale(s), 0 cancelled')\n"
SYNC_BAD = "import sys\nprint('boom')\nsys.exit(1)\n"
SYNC_BAD_BUT_FRESH = ("import sys, json\nfrom pathlib import Path\nPath('books_synced.json').write_text(json.dumps([{'id': 'B1', 'stock': 0}]))\n"
                      "print('0 new sale(s), 0 cancelled')\nprint('! 1 pusher(s) failed')\nsys.exit(1)\n")
COUNTING_SYNC = "from pathlib import Path\np = Path('sync_calls'); p.write_text(p.read_text() + 'x' if p.exists() else 'x')\nprint('0 new sale(s)')\n"


def make(pushers, cfg=None, pusher_cfg=None, env_text=ENV_TEXT, books=True):
    tmp = Path(tempfile.mkdtemp(prefix="pa_safety_"))
    shutil.copy(ROOT / "push_all.py", tmp / "push_all.py")
    for name, code in pushers.items():
        (tmp / name).write_text(code, encoding="utf-8")
    if books:
        (tmp / "books.json").write_text(json.dumps([{"id": "B1", "title": "Uno", "price_eur": 10}]), encoding="utf-8")
    config = {"json_file": str(tmp / "books.json"), "timeout_minutes": 5, "email": {"to": ["boss@example.com"]},
              "pushers": pusher_cfg or {}}
    config.update(cfg or {})
    (tmp / "push_all_config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp / ".env").write_text(env_text, encoding="utf-8")
    return tmp


def run(tmp, *args, timeout=120):
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("GMAIL_", "SMTP_", "REPORT_TO", "VINTED_", "AMAZON_", "GALAXUS_", "CARREFOUR_", "PUSHERS_", "STOCK_SYNC", "PUSH_ALL"))}
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "push_all.py", *args], cwd=tmp, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def subject():
    return MAILS[-1]["Subject"] if MAILS else None


def products_given(out):
    return [Path(l.split("ARGV ")[1].split()[1]).name for l in out.splitlines() if l.startswith("ARGV")]


def on(*names):
    return {n: {"enabled": True, "args": []} for n in names}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    two = {"vinted_api_push.py": MARKER, "galaxus_push.py": MARKER}

    print("\n=== the stock sync fails ===")
    tmp = make({**two, "stock_sync.py": SYNC_OK}, {"stock_sync": {"enabled": True}}, on("vinted", "galaxus"))
    (tmp / "books_synced.json").write_text(json.dumps([{"id": "B1", "stock": 0, "sold_out": True}]), encoding="utf-8")
    rc, out = run(tmp, "--no-email")
    check("sync fine: pushers are given books_synced.json", products_given(out) == ["books_synced.json"] * 2, out[-300:])
    (tmp / "stock_sync.py").write_text(SYNC_BAD, encoding="utf-8")
    yesterday = __import__("time").time() - 24 * 3600
    os.utime(tmp / "books_synced.json", (yesterday, yesterday))      # left over from an earlier day
    for m in tmp.glob("ran_*"):
        m.unlink()
    MAILS.clear()
    rc, out = run(tmp)
    check("sync failed and left only an old books_synced.json: no pusher runs (not even the Galaxus feed)",
          not list(tmp.glob("ran_*")) and "not run: the stock sync failed" in out, out[-400:])
    check("... the report says so, the subject names the pushers, the email is sent, exit code 1",
          rc == 1 and len(MAILS) == 1 and "failed:" in subject() and "Vinted" in subject() and "Galaxus" in subject(), str(subject()))
    cfg = json.loads((tmp / "push_all_config.json").read_text(encoding="utf-8"))
    cfg["stock_sync"]["on_failure"] = "continue"
    (tmp / "push_all_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    rc, out = run(tmp, "--no-email")
    check('stock_sync.on_failure = "continue": pushers run from the raw books.json, with a warning',
          products_given(out) == ["books.json"] * 2 and "UNSYNCED" in out, out[-400:])
    (tmp / "stock_sync.py").write_text(SYNC_BAD_BUT_FRESH, encoding="utf-8")
    cfg["stock_sync"]["on_failure"] = "skip"
    (tmp / "push_all_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    rc, out = run(tmp, "--no-email")
    check("sync exits with an error (a pusher failed inside it) but wrote a fresh books_synced.json: pushers use it",
          products_given(out) == ["books_synced.json"] * 2 and "not run" not in out, out[-400:])

    print("\n=== the stock sync runs again between pushers in a long run ===")
    slow = "import time\nprint('working', flush=True)\ntime.sleep(2.5)\n"
    tmp = make({"vinted_api_push.py": slow, "amazon_push.py": MARKER, "stock_sync.py": COUNTING_SYNC},
               {"stock_sync": {"enabled": True, "every_minutes": 0.02}}, on("vinted", "amazon"))
    rc, out = run(tmp, "--no-email")
    calls = (tmp / "sync_calls").read_text() if (tmp / "sync_calls").exists() else ""
    check("a pusher that takes longer than every_minutes: orders are read again before the next one", len(calls) == 2, f"{len(calls)} syncs\n{out[-300:]}")
    tmp = make({"vinted_api_push.py": slow, "amazon_push.py": MARKER, "stock_sync.py": COUNTING_SYNC},
               {"stock_sync": {"enabled": True}}, on("vinted", "amazon"))
    rc, out = run(tmp, "--no-email")
    check("default every 15 minutes: a short run syncs once", (tmp / "sync_calls").read_text() == "x", out[-300:])
    tmp = make({"vinted_api_push.py": slow, "amazon_push.py": MARKER, "stock_sync.py": SYNC_OK},
               {"stock_sync": {"enabled": True, "every_minutes": 0.02}}, on("vinted", "amazon"))
    MAILS.clear()
    rc, out = run(tmp)
    check("a re-sync that works adds no row to the report", "Stock sync (again)" not in MAILS[-1].get_body(("plain",)).get_content(), out[-300:])

    print("\n=== config typos and crashes ===")
    tmp = make({"vinted_api_push.py": MARKER, "amazon_push.py": MARKER}, pusher_cfg={
        "vinted": {"enabled": True, "args": [], "timeout_minutes": "abc"}, "amazon": {"enabled": "yes", "args": "x"}})
    MAILS.clear()
    rc, out = run(tmp)
    check("a typo in the config: clear message naming the settings, nothing runs, nothing half-done",
          rc != 0 and "Settings problems" in out and "pushers.vinted.timeout_minutes" in out and "pushers.amazon.enabled" in out
          and "pushers.amazon.args" in out and not list(tmp.glob("ran_*")) and "Traceback" not in out, out[-500:])
    import push_all
    example = json.loads((ROOT / "push_all_config.example.json").read_text(encoding="utf-8"))
    check("the shipped push_all_config.example.json passes the check", push_all.config_problems(example) == [], str(push_all.config_problems(example)))
    check("other mistakes are caught too",
          len(push_all.config_problems({"email": {"when": "sometimes"}, "stock_sync": {"on_failure": "x", "every_minutes": 0},
                                        "log_keep_days": -1, "pushers": {"vinted": 5}})) == 5)

    tmp = make({"vinted_api_push.py": MARKER, "amazon_push.py": MARKER}, pusher_cfg=on("vinted", "amazon"))
    (tmp / "posted_api.json").write_text("[]", encoding="utf-8")            # a state file that is not an object
    MAILS.clear()
    rc, out = run(tmp)
    check("a state file holding a list instead of an object no longer crashes the run",
          "Traceback" not in out and (tmp / "ran_amazon_push").exists() and len(MAILS) == 1, out[-400:])

    # an unexpected exception inside one pusher's run: the others and the report must still happen
    tmp = make({"vinted_api_push.py": MARKER, "amazon_push.py": MARKER}, pusher_cfg=on("vinted", "amazon"))
    saved = (push_all.BASE, push_all.CONFIG_FILE, push_all.ENV_FILE, push_all.LOG_DIR, push_all.LOCK_FILE, push_all.run_pusher)
    push_all.BASE, push_all.CONFIG_FILE, push_all.ENV_FILE = tmp, tmp / "push_all_config.json", tmp / ".env"
    push_all.LOG_DIR, push_all.LOCK_FILE = tmp / "logs", tmp / "push_all.lock"
    real_run_pusher = push_all.run_pusher

    def exploding(name, *a, **k):
        if name == "vinted":
            raise RuntimeError("disk exploded")
        return real_run_pusher(name, *a, **k)

    push_all.run_pusher = exploding
    old_cwd, old_argv = os.getcwd(), sys.argv
    sys.argv = ["push_all.py"]
    os.chdir(tmp)
    MAILS.clear()
    buf = io.StringIO()
    code = None
    from contextlib import redirect_stdout
    with redirect_stdout(buf):
        try:
            push_all.main()
        except SystemExit as e:
            code = e.code
    os.chdir(old_cwd)
    sys.argv = old_argv
    push_all.BASE, push_all.CONFIG_FILE, push_all.ENV_FILE, push_all.LOG_DIR, push_all.LOCK_FILE, push_all.run_pusher = saved
    out = buf.getvalue()
    check("a pusher that crashes inside push_all is reported as FAILED; the next pusher still runs",
          "internal error: RuntimeError: disk exploded" in out and (tmp / "ran_amazon_push").exists(), out[-500:])
    check("... and the report email still goes out, exit code 1", code == 1 and len(MAILS) == 1 and "Vinted" in subject(), f"{code} {subject()}")

    print("\n=== a marketplace that lost its credentials ===")
    no_token = ENV_TEXT.replace("VINTED_PRO_TOKEN=a,b\n", "")
    for when in ("problems", "always"):
        tmp = make({"vinted_api_push.py": MARKER, "amazon_push.py": MARKER}, {"email": {"to": ["boss@example.com"], "when": when}},
                   on("vinted", "amazon"), env_text=no_token)
        MAILS.clear()
        rc, out = run(tmp)
        check(f'when = "{when}": email sent, subject says what did not run, exit code 0',
              len(MAILS) == 1 and subject() == "[Product push] PROBLEMS - 0 pushed, not configured: Vinted" and rc == 0, f"{rc} {subject()}")
    tmp = make({"vinted_api_push.py": MARKER, "amazon_push.py": MARKER}, {"email": {"to": ["boss@example.com"], "when": "problems"}},
               on("vinted", "amazon"))
    MAILS.clear()
    rc, out = run(tmp)
    check('when = "problems" and everything fine: still no email', not MAILS and "(no email sent)" in out, out[-200:])

    print("\n=== the report email and big logs ===")
    big = "for i in range(30000): print('   + book %d pushed, with a long line of text to make this log big ......' % i)\n"
    tmp = make({"vinted_api_push.py": big}, pusher_cfg=on("vinted"))
    SMTP_LIMIT[0] = 600_000
    MAILS.clear()
    rc, out = run(tmp)
    log_size = (list((tmp / "logs").glob("*/vinted.log")) or [Path("x")])[0].stat().st_size
    att = [p for p in MAILS[-1].iter_attachments()] if MAILS else []
    att_text = att[0].get_content() if att else ""
    check("a 3 MB log is trimmed in the attachment (first part and last part) and the email goes out",
          log_size > 2_000_000 and len(MAILS) == 1 and "characters cut" in att_text and len(att_text) < 400_000, f"log {log_size}, att {len(att_text)}\n{out[-300:]}")
    check("... the full log stays on disk", log_size > 2_000_000)
    SMTP_LIMIT[0] = 20_000
    MAILS.clear()
    rc, out = run(tmp)
    body = MAILS[-1].get_body(("plain",)).get_content() if MAILS else ""
    check("a mail server that refuses the email with the log: the report is sent again without the attachment, with a note",
          len(MAILS) == 1 and not list(MAILS[-1].iter_attachments()) and "attachment was left out" in body and rc == 0, f"{rc}\n{out[-300:]}")
    SMTP_LIMIT[0] = 10
    MAILS.clear()
    rc, out = run(tmp)
    check("a mail server that refuses everything: exit code 2, message, nothing hangs", rc == 2 and "Could not send" in out, out[-200:])
    SMTP_LIMIT[0] = 10 ** 9

    print("\n=== wrong products path ===")
    tmp = make({"vinted_api_push.py": MARKER, "amazon_push.py": MARKER}, {"json_file": "C:/no/such/books.json"}, on("vinted", "amazon"))
    MAILS.clear()
    rc, out = run(tmp)
    check("the report names the missing file once; no pusher is started; email sent; exit code 1",
          rc == 1 and not list(tmp.glob("ran_*")) and "Products file" in out and "not found" in out and len(MAILS) == 1
          and out.count("not run: products file not found") == 2, out[-400:])

    print("\n=== dry runs ===")
    tmp = make({"carrefour_push.py": MARKER, "vinted_api_push.py": MARKER}, pusher_cfg=on("carrefour", "vinted"))
    rc, out = run(tmp, "--dry-run", "--no-email")
    check("--dry-run: a pusher whose safe mode only writes files (here Carrefour) runs without credentials",
          (tmp / "ran_carrefour_push").exists() and "Carrefour" in out and "--dry-run" in out, out[-400:])
    mk = make({"carrefour_push.py": MARKER}, pusher_cfg=on("carrefour"))
    rc, out = run(mk, "--no-email")
    check("a real run without credentials does not start the pusher", not (mk / "ran_carrefour_push").exists() and "NOT CONFIGURED" in out, out[-300:])
    mv = make({"vinted_api_push.py": MARKER}, pusher_cfg=on("vinted"), env_text=no_token)
    rc, out = run(mv, "--dry-run", "--no-email")
    check("--dry-run of Vinted without its token: NOT CONFIGURED (--validate-only needs the API)",
          not (mv / "ran_vinted_api_push").exists() and "NOT CONFIGURED" in out, out[-300:])

    print("\n=== .env lines ===")
    e = Path(tempfile.mkdtemp()) / ".env"
    e.write_text("A1=abc123   # my token\nA2='abc 123'  # note\nA3=\"a # b\" # note\nA4=pass#word\nA5=plain\nA6='it'\n"
                 "# a whole comment line\nexport A7=seven # c\n", encoding="utf-8")
    for k in ("A1", "A2", "A3", "A4", "A5", "A6", "A7"):
        os.environ.pop(k, None)
    push_all.load_env_file(e)
    got = {k: os.environ.get(k) for k in ("A1", "A2", "A3", "A4", "A5", "A6", "A7")}
    check("trailing '# comment' dropped from unquoted values", got["A1"] == "abc123" and got["A7"] == "seven", str(got))
    check("quoted values keep spaces and '#', and a comment after the closing quote is dropped",
          got["A2"] == "abc 123" and got["A3"] == "a # b" and got["A6"] == "it", str(got))
    check("a '#' inside a value (no space before it) is kept", got["A4"] == "pass#word" and got["A5"] == "plain", str(got))

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
