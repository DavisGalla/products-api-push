#!/usr/bin/env python3
"""
Test for push_all.py. It runs in a temporary folder with FAKE pushers (small
scripts with the real names that print typical output and write state files)
and a FAKE mail server on 127.0.0.1:8766. Nothing is pushed or emailed.

    python tests/test_push_all.py
"""
import base64
import email
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

ROOT = Path(__file__).resolve().parent.parent
SMTP_PORT = 8766
RESULTS = []
MAILS = []   # (auth, envelope rcpts, parsed message)


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n        {detail}"))


# ------------------------------------------------------------ fake SMTP

class FakeSMTP(socketserver.StreamRequestHandler):
    def handle(self):
        send = lambda s: self.wfile.write((s + "\r\n").encode())
        send("220 fake")
        auth, rcpts = None, []
        while True:
            line = self.rfile.readline().decode(errors="replace").rstrip("\r\n")
            if not line:
                return
            cmd = line.upper()
            if cmd.startswith(("EHLO", "HELO")):
                send("250-fake")
                send("250 AUTH PLAIN")
            elif cmd.startswith("AUTH PLAIN"):
                auth = base64.b64decode(line.split()[2]).split(b"\0")
                if auth[2] != b"abcdabcdabcdabcd":
                    send("535 bad credentials")
                else:
                    send("235 ok")
            elif cmd.startswith("MAIL FROM"):
                send("250 ok")
            elif cmd.startswith("RCPT TO"):
                rcpts.append(line.split(":", 1)[1].strip(" <>"))
                send("250 ok")
            elif cmd == "DATA":
                send("354 go")
                data = b""
                while True:
                    l = self.rfile.readline()
                    if l in (b".\r\n", b""):
                        break
                    data += l
                MAILS.append((auth, rcpts, email.message_from_bytes(data, policy=policy.default)))
                send("250 queued")
            elif cmd == "QUIT":
                send("221 bye")
                return
            else:
                send("250 ok")


# -------------------------------------------------------- fake pushers

FAKES = {
    # 2 new items pushed, 1 skipped, 1 already there
    "vinted_api_push.py": r'''
import json, sys
from pathlib import Path
assert sys.argv[1] == "push", sys.argv
s = Path("posted_api.json"); st = json.loads(s.read_text()) if s.exists() else {}
print("[1] already pushed: Old book")
print("[2] SKIP 'No price': no price")
if "--validate-only" in sys.argv:
    print("   2/2 valid"); sys.exit()
st["A"] = {"item_id": 1}; st["B"] = {"item_id": 2}
s.write_text(json.dumps(st))
print("   + A -> 1"); print("   + B -> 2")
''',
    # 1 pushed, 1 rejected, 1 not in catalog
    "amazon_push.py": r'''
import json, sys
from pathlib import Path
assert "--yes" in sys.argv and "--markets" in sys.argv, sys.argv
s = Path("amazon_posted.json"); st = json.loads(s.read_text()) if s.exists() else {}
print("[1] (1/2) Book one")
print("   ES: SKIP - ISBN 978 is not in the Amazon.es catalog")
print("   DE: ! 400 INVALID: price too high")
print("   FR: ACCEPTED  B000  Book one")
st["FR:T-1"] = {"status": "ACCEPTED"}
s.write_text(json.dumps(st))
''',
    # bad credentials -> sys.exit
    "fnac_push.py": r'''
import sys
sys.exit("Fnac login failed (ERROR): bad key")
''',
    # hangs -> timeout
    "abebooks_push.py": r'''
import time
print("sending...", flush=True)
time.sleep(30)
''',
    # never run: not configured
    "casadellibro_push.py": "raise SystemExit('should not run')\n",
}


def setup(tmp):
    shutil.copy(ROOT / "push_all.py", tmp / "push_all.py")
    for name, code in FAKES.items():
        (tmp / name).write_text(code, encoding="utf-8")
    (tmp / "books.json").write_text("[]", encoding="utf-8")
    (tmp / "push_all_config.json").write_text(json.dumps({
        "json_file": str(tmp / "books.json"),
        "timeout_minutes": 60,
        "email": {"to": ["boss@example.com", "me@example.com"]},
        "pushers": {
            "vinted": {"enabled": True, "args": ["--catalog-id", "1", "--package-size-id", "2"]},
            "amazon": {"enabled": True, "args": ["--markets", "ES,DE,FR"]},
            "fnac": {"enabled": True},
            "abebooks": {"enabled": True, "timeout_minutes": 0.03},
            "casadellibro": {"enabled": True},
            "ebay": {"enabled": False},
        }}), encoding="utf-8")
    (tmp / ".env").write_text(
        "# test credentials\n"
        "GMAIL_ADDRESS=sender@gmail.com\n"
        "GMAIL_APP_PASSWORD='abcd abcd abcd abcd'\n"
        f"SMTP_HOST=127.0.0.1\nSMTP_PORT={SMTP_PORT}\n"
        "VINTED_PRO_TOKEN=a,b\n"
        "AMAZON_LWA_CLIENT_ID=x\nAMAZON_LWA_CLIENT_SECRET=x\nAMAZON_REFRESH_TOKEN=x\nAMAZON_SELLER_ID=x\n"
        "FNAC_API_URL=x\nFNAC_PARTNER_ID=x\nFNAC_SHOP_ID=x\nFNAC_KEY=x\n"
        "ABEBOOKS_USERNAME=x\nABEBOOKS_API_KEY=x\n"
        "CASADELLIBRO_API_URL=\n", encoding="utf-8")


def run(tmp, *args):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GMAIL_", "SMTP_", "REPORT_TO", "VINTED_", "AMAZON_", "FNAC_",
                                "ABEBOOKS_", "CASADELLIBRO_", "EBAY_", "ELCORTEINGLES_", "PUSHERS_"))}
    p = subprocess.run([sys.executable, "push_all.py", *args], cwd=tmp, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    return p.returncode, p.stdout + p.stderr


def body_of(msg, kind):
    part = msg.get_body(preferencelist=(kind,))
    return part.get_content() if part else ""


def test_push_all():
    print("\n=== push_all.py (fake pushers + fake Gmail) ===")
    server = socketserver.ThreadingTCPServer(("127.0.0.1", SMTP_PORT), FakeSMTP)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="push_all_test_"))
    try:
        setup(tmp)

        rc, out = run(tmp, "--test-email")
        check("--test-email sends through the mail server", rc == 0 and len(MAILS) == 1, out[-500:])
        if MAILS:
            auth, rcpts, msg = MAILS[-1]
            check("logs in with the address and the app password (spaces removed)",
                  auth[1] == b"sender@gmail.com" and auth[2] == b"abcdabcdabcdabcd", str(auth))
            check("test email goes to the configured recipient (GMAIL_ADDRESS)", rcpts == ["sender@gmail.com"], str(rcpts))

        (tmp / "push_all_config.json").rename(tmp / "cfg.bak")
        rc, out = run(tmp, "--no-email")
        check("missing config file is explained", rc != 0 and "push_all_config.example.json" in out, out[-300:])
        (tmp / "cfg.bak").rename(tmp / "push_all_config.json")

        rc, out = run(tmp)
        print("    | " + "\n    | ".join(out.splitlines()[-16:]))
        check("exit code 1 because a pusher failed", rc == 1, f"rc={rc}")
        check("one report email sent", len(MAILS) == 2, f"{len(MAILS)} mails")
        auth, rcpts, msg = MAILS[-1]
        subject = msg["Subject"]
        text, htm = body_of(msg, "plain"), body_of(msg, "html")
        check("report goes to every address in config email.to",
              rcpts == ["boss@example.com", "me@example.com"], str(rcpts))
        check("subject sums it up", subject == "[Product push] FAILED - 3 pushed, 1 rejected, failed: Fnac, AbeBooks / IberLibro",
              subject)
        check("Vinted: 2 pushed, 1 skipped, 1 already listed",
              "Vinted" in text and "2 pushed, 1 skipped, 1 already listed" in text, text)
        check("Amazon: 1 pushed, rejection shown with its reason",
              "1 pushed, 1 rejected/errors, 1 skipped" in text and "DE: ! 400 INVALID: price too high" in text, text)
        check("Fnac: FAILED with the error message", "FAILED" in text and "Fnac login failed (ERROR): bad key" in text, text)
        check("hanging pusher is killed and reported", "timed out after" in text, text)
        check("pusher with missing credentials is NOT CONFIGURED, not run",
              "NOT CONFIGURED" in text and "missing CASADELLIBRO_API_URL, CASADELLIBRO_API_KEY" in text
              and "should not run" not in out, text)
        check("disabled pusher (eBay) not in the report", "eBay" not in text)
        check("HTML version has the table", "<table" in htm and "Amazon EU" in htm)
        att = [p for p in msg.iter_attachments() if p.get_filename() == "push_log.txt"]
        check("full log attached", att and "price too high" in att[0].get_content(), str(att))
        logs = list((tmp / "logs").glob("*/report.txt"))
        check("report and logs saved under logs/", len(logs) == 1 and (logs[0].parent / "amazon.log").exists())
        check("lock file removed after the run", not (tmp / "push_all.lock").exists())

        rc, out = run(tmp, "--only", "vinted", "--no-email")
        check("re-run pushes nothing new (state unchanged)", "0 pushed" in out and rc == 0, out[-400:])
        check("--no-email sends nothing", len(MAILS) == 2)

        rc, out = run(tmp, "--only", "vinted", "--dry-run", "--no-email")
        check("--dry-run passes the safe-mode flag", "--validate-only" in out and "DRY RUN" in out, out[-400:])

        (tmp / "push_all.lock").write_text("123")
        rc, out = run(tmp, "--only", "vinted", "--no-email")
        check("a second run at the same time is refused", rc != 0 and "in progress" in out, out[-300:])
        (tmp / "push_all.lock").unlink()

        rc, out = run(tmp, "--only", "nosuch")
        check("unknown pusher name is rejected", rc != 0 and "Unknown pusher" in out, out[-300:])

        env_text = (tmp / ".env").read_text().replace("abcd abcd abcd abcd", "wrong")
        (tmp / ".env").write_text(env_text)
        rc, out = run(tmp, "--only", "vinted")
        check("email failure is reported with exit code 2", rc == 2 and "Could not send the report email" in out,
              out[-300:])
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_push_all()
    print(f"\n{sum(RESULTS)} passed, {len(RESULTS) - sum(RESULTS)} failed")
    sys.exit(0 if all(RESULTS) else 1)
