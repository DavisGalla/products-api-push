#!/usr/bin/env python3
"""
Run every enabled marketplace pusher one after another and email one report,
for example: "Vinted 5 pushed - Amazon 3 pushed, 1 rejected - Fnac FAILED".

The pushers themselves are not changed. Each runs as its own process with
--yes; the runner works out what happened from:
  * its state file (new or changed entries = pushed),
  * its output ("SKIP" lines = skipped, "!" lines = rejected/errors),
  * its exit code (non-zero = failed, e.g. bad credentials).

SETUP (see docs/push_all.md)
  1. Copy push_all_config.example.json to push_all_config.json, set the
     products file, the email recipients, and enable the pushers you use.
  2. Credentials come from environment variables. Recommended: store them in
     Bitwarden Secrets Manager and start runs with `bws run --`, which injects
     them. Alternative: copy .env.example to .env and fill it in (never
     committed). Real environment variables take precedence over .env.

USAGE (drop "bws run --" when using a .env file)
    bws run -- python push_all.py --test-email          # check the Gmail setup only
    bws run -- python push_all.py --dry-run --no-email  # safe modes of every pusher, print report
    bws run -- python push_all.py                       # real run, email the report
    bws run -- python push_all.py --only vinted,amazon  # just these pushers
"""
import argparse
import html
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "push_all_config.json"
ENV_FILE = Path(os.environ.get("PUSHERS_ENV_FILE") or BASE / ".env")
LOG_DIR = BASE / "logs"
LOCK_FILE = BASE / "push_all.lock"
LOCK_MAX_AGE = 6 * 3600  # an older lock is treated as left over from a crash

# name -> how to run it. "sub": the script uses a `push` subcommand.
# "dry": the flags for its safe mode (used with --dry-run).
PUSHERS = {
    "vinted": dict(label="Vinted", script="vinted_api_push.py", sub=True, yes=False,
                   state="posted_api.json", dry=["--validate-only"],
                   env=["VINTED_PRO_TOKEN"]),
    "ebay": dict(label="eBay", script="ebay_push.py", sub=False, yes=True,
                 state="ebay_posted.json", dry=["--dry-run"],
                 env=["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN"]),
    "amazon": dict(label="Amazon EU", script="amazon_push.py", sub=True, yes=True,
                   state="amazon_posted.json", dry=["--validate-only"],
                   env=["AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
                        "AMAZON_REFRESH_TOKEN", "AMAZON_SELLER_ID"]),
    "casadellibro": dict(label="Casa del Libro", script="casadellibro_push.py", sub=True, yes=True,
                         state="casadellibro_posted.json", dry=["--dry-run"],
                         env=["CASADELLIBRO_API_URL", "CASADELLIBRO_API_KEY"]),
    "fnac": dict(label="Fnac", script="fnac_push.py", sub=True, yes=True,
                 state="fnac_posted.json", dry=["--dry-run"],
                 env=["FNAC_API_URL", "FNAC_PARTNER_ID", "FNAC_SHOP_ID", "FNAC_KEY"]),
    "abebooks": dict(label="AbeBooks / IberLibro", script="abebooks_push.py", sub=True, yes=True,
                     state="abebooks_posted.json", dry=["--dry-run"],
                     env=["ABEBOOKS_USERNAME", "ABEBOOKS_API_KEY"]),
    "booklooker": dict(label="booklooker", script="booklooker_push.py", sub=True, yes=True,
                       state="booklooker_posted.json", dry=["--dry-run"],
                       env=["BOOKLOOKER_API_KEY"]),
    "elcorteingles": dict(label="El Corte Inglés", script="elcorteingles_push.py", sub=True, yes=True,
                          state="elcorteingles_posted.json", dry=["--dry-run"],
                          env=["ELCORTEINGLES_API_URL", "ELCORTEINGLES_API_KEY"]),
    "carrefour": dict(label="Carrefour", script="carrefour_push.py", sub=True, yes=True,
                      state="carrefour_posted.json", dry=["--dry-run"],
                      env=["CARREFOUR_API_URL", "CARREFOUR_API_KEY"]),
    "worten": dict(label="Worten", script="worten_push.py", sub=True, yes=True,
                   state="worten_posted.json", dry=["--dry-run"],
                   env=["WORTEN_API_URL", "WORTEN_API_KEY"]),
    "ibs": dict(label="IBS.it", script="ibs_push.py", sub=True, yes=True,
                state="ibs_posted.json", dry=["--dry-run"],
                env=["IBS_API_URL", "IBS_API_KEY"]),
    "empik": dict(label="Empik", script="empik_push.py", sub=True, yes=True,
                  state="empik_posted.json", dry=["--dry-run"],
                  env=["EMPIK_API_URL", "EMPIK_API_KEY"]),
    "leclerc": dict(label="E.Leclerc", script="leclerc_push.py", sub=True, yes=True,
                    state="leclerc_posted.json", dry=["--dry-run"],
                    env=["LECLERC_API_URL", "LECLERC_API_KEY"]),
    "kaufland": dict(label="Kaufland", script="kaufland_push.py", sub=True, yes=True,
                     state="kaufland_posted.json", dry=["--dry-run"],
                     env=["KAUFLAND_CLIENT_KEY", "KAUFLAND_SECRET_KEY"]),
    "bol": dict(label="bol.com", script="bol_push.py", sub=True, yes=True,
                state="bol_posted.json", dry=["--dry-run"],
                env=["BOL_CLIENT_ID", "BOL_CLIENT_SECRET"]),
    "cdiscount": dict(label="Cdiscount", script="cdiscount_push.py", sub=True, yes=True,
                      state="cdiscount_posted.json", dry=["--dry-run"],
                      env=["OCTOPIA_CLIENT_ID", "OCTOPIA_CLIENT_SECRET", "OCTOPIA_SELLER_ID"]),
    "allegro": dict(label="Allegro", script="allegro_push.py", sub=True, yes=True,
                    state="allegro_posted.json", dry=["--dry-run"],
                    env=["ALLEGRO_CLIENT_ID", "ALLEGRO_CLIENT_SECRET"]),
    "aliexpress": dict(label="AliExpress", script="aliexpress_push.py", sub=True, yes=True,
                       state="aliexpress_posted.json", dry=["--dry-run"],
                       env=["ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET"]),
    "temu": dict(label="Temu", script="temu_push.py", sub=True, yes=True,
                 state="temu_posted.json", dry=["--dry-run"],
                 env=["TEMU_APP_KEY", "TEMU_APP_SECRET", "TEMU_ACCESS_TOKEN"]),
    "tiktok": dict(label="TikTok Shop", script="tiktok_push.py", sub=True, yes=True,
                   state="tiktok_posted.json", dry=["--dry-run"],
                   env=["TIKTOK_APP_KEY", "TIKTOK_APP_SECRET"]),
    "galaxus": dict(label="Galaxus", script="galaxus_push.py", sub=True, yes=True,
                    state="galaxus_posted.json", dry=["--dry-run"],
                    env=["GALAXUS_SFTP_USER", "GALAXUS_SFTP_PASSWORD", "GALAXUS_PROVIDER"]),
    "otto": dict(label="OTTO Market", script="otto_push.py", sub=True, yes=True,
                 state="otto_posted.json", dry=["--dry-run"],
                 env=["OTTO_CLIENT_ID", "OTTO_CLIENT_SECRET"]),
    "emag": dict(label="eMAG", script="emag_push.py", sub=True, yes=True,
                 state="emag_posted.json", dry=["--dry-run"],
                 env=["EMAG_USERNAME", "EMAG_PASSWORD"]),
}

RE_SKIP = re.compile(r"\bSKIP\b")
RE_ALREADY = re.compile(r"already (pushed|done)")
RE_PROBLEM = re.compile(r"^\s*(?:[A-Z]{2}: )?! "                       # per-item errors: "   ! ..." / "   ES: ! ..."
                        r"|request failed \(|Slot limit|offer import rejected"
                        r"|Still \w+ after \d+s|^\s+reason: |gave up after retries")


# ----------------------------------------------------------------- setup

def load_env_file(path):
    """Read KEY=VALUE lines into os.environ. Existing variables win."""
    if not path.exists():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(f"{CONFIG_FILE.name} not found. Copy push_all_config.example.json to "
                 f"{CONFIG_FILE.name} and edit it.")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    unknown = set(cfg.get("pushers", {})) - set(PUSHERS)
    if unknown:
        sys.exit(f"Unknown pusher(s) in {CONFIG_FILE.name}: {', '.join(sorted(unknown))}. "
                 f"Known: {', '.join(PUSHERS)}")
    return cfg


def take_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - LOCK_FILE.stat().st_mtime < LOCK_MAX_AGE:
            sys.exit(f"Another push_all run is in progress ({LOCK_FILE.name} exists). "
                     f"Delete it if that run crashed.")
        LOCK_FILE.unlink()
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


# ------------------------------------------------------------------- run

def read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_command(spec, settings, json_file, dry_run):
    cmd = [sys.executable, str(BASE / spec["script"])]
    if spec["sub"]:
        cmd.append("push")
    if json_file:
        cmd.append(str(json_file))
    cmd += [str(x) for x in settings.get("args", [])]
    if dry_run:
        cmd += [f for f in spec["dry"] if f not in cmd]
    if spec["yes"]:
        cmd.append("--yes")
    return cmd


def required_env(name, spec, settings):
    names = list(spec["env"])
    if name == "fnac" and "--sandbox" in settings.get("args", []):
        names.remove("FNAC_API_URL")
    return names


def run_pusher(name, spec, settings, cfg, a, log_dir):
    res = {"name": name, "label": spec["label"], "pushed": 0, "skipped": 0, "already": 0,
           "problems": [], "tail": [], "exit_code": None, "seconds": 0, "log": None}
    missing = [k for k in required_env(name, spec, settings) if not os.environ.get(k)]
    # dry runs of these pushers build their files without credentials
    if missing and not (a.dry_run and name in ("fnac", "abebooks", "ebay", "kaufland", "galaxus", "otto", "emag")):
        res.update(status="NOT CONFIGURED", note="missing " + ", ".join(missing))
        return res

    state_path = BASE / spec["state"]
    before = read_state(state_path)
    cmd = build_command(spec, settings, cfg.get("json_file"), a.dry_run)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    timeout = float(settings.get("timeout_minutes", cfg.get("timeout_minutes", 60))) * 60
    start = time.time()
    print(f"\n=== {spec['label']}: {' '.join(cmd[1:])}", flush=True)
    try:
        p = subprocess.run(cmd, cwd=BASE, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, encoding="utf-8", errors="replace")
        output, res["exit_code"] = p.stdout, p.returncode
        timed_out = False
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        output = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        timed_out = True
    res["seconds"] = round(time.time() - start)
    print(output, end="" if output.endswith("\n") else "\n", flush=True)

    log = log_dir / f"{name}.log"
    log.write_text(f"$ {' '.join(cmd)}\n\n{output}", encoding="utf-8")
    res["log"] = log

    after = read_state(state_path)
    res["pushed"] = sum(1 for k, v in after.items() if before.get(k) != v)
    lines = output.splitlines()
    res["skipped"] = sum(1 for l in lines if RE_SKIP.search(l))
    res["already"] = sum(1 for l in lines if RE_ALREADY.search(l))
    res["problems"] = [l.strip() for l in lines if RE_PROBLEM.search(l)]
    res["tail"] = [l for l in lines if l.strip()][-4:]

    if timed_out:
        res.update(status="FAILED", note=f"timed out after {timeout / 60:g} min (killed)")
    elif res["exit_code"] != 0:
        last = res["tail"][-1] if res["tail"] else ""
        res.update(status="FAILED", note=f"exit code {res['exit_code']}: {last}")
    elif res["problems"]:
        res.update(status="PROBLEMS", note="")
    else:
        res.update(status="OK", note="")
    return res


# ---------------------------------------------------------------- report

def summary_line(r):
    if r["status"] in ("NOT CONFIGURED", "DISABLED"):
        return r["note"]
    parts = [f"{r['pushed']} pushed"]
    if r["problems"]:
        parts.append(f"{len(r['problems'])} rejected/errors")
    if r["skipped"]:
        parts.append(f"{r['skipped']} skipped")
    if r["already"]:
        parts.append(f"{r['already']} already listed")
    text = ", ".join(parts)
    return f"{text} - {r['note']}" if r["note"] else text


def subject_of(results, dry_run):
    ran = [r for r in results if r["status"] not in ("NOT CONFIGURED", "DISABLED")]
    pushed = sum(r["pushed"] for r in ran)
    problems = sum(len(r["problems"]) for r in ran)
    failed = [r["label"] for r in ran if r["status"] == "FAILED"]
    worst = "FAILED" if failed else "PROBLEMS" if problems else "OK"
    bits = [f"{pushed} pushed"]
    if problems:
        bits.append(f"{problems} rejected")
    if failed:
        bits.append("failed: " + ", ".join(failed))
    prefix = "[Product push DRY RUN]" if dry_run else "[Product push]"
    return f"{prefix} {worst} - " + ", ".join(bits)


def text_report(results, started, dry_run):
    out = [f"Product push {'(DRY RUN) ' if dry_run else ''}started {started}", ""]
    width = max(len(r["label"]) for r in results)
    for r in results:
        out.append(f"{r['label']:<{width}}  {r['status']:<14}  {summary_line(r)}")
        for line in r["problems"][:20]:
            out.append(f"{'':<{width}}    {line}")
        if len(r["problems"]) > 20:
            out.append(f"{'':<{width}}    ... and {len(r['problems']) - 20} more (see attached log)")
        if r["status"] == "FAILED" or (dry_run and r["tail"]):
            for line in r["tail"]:
                out.append(f"{'':<{width}}    > {line.strip()}")
    out += ["", "Full output of every pusher is in the attached log."]
    return "\n".join(out)


COLORS = {"OK": "#1e7e34", "PROBLEMS": "#b8860b", "FAILED": "#c0392b",
          "NOT CONFIGURED": "#777777", "DISABLED": "#777777"}


def html_report(results, started, dry_run):
    rows = []
    for r in results:
        details = "".join(f"<div style='font-family:monospace;font-size:12px'>{html.escape(l)}</div>"
                          for l in r["problems"][:20])
        if r["status"] == "FAILED" or (dry_run and r["tail"]):
            details += "".join(f"<div style='font-family:monospace;font-size:12px;color:#555'>&gt; "
                               f"{html.escape(l.strip())}</div>" for l in r["tail"])
        rows.append(
            f"<tr><td style='padding:6px 10px;vertical-align:top'><b>{html.escape(r['label'])}</b></td>"
            f"<td style='padding:6px 10px;vertical-align:top;color:{COLORS[r['status']]}'><b>{r['status']}</b></td>"
            f"<td style='padding:6px 10px;vertical-align:top'>{html.escape(summary_line(r))}{details}</td></tr>")
    return (f"<p>Product push {'<b>(DRY RUN)</b> ' if dry_run else ''}started {html.escape(started)}</p>"
            f"<table style='border-collapse:collapse' border='1' cellspacing='0'>"
            f"<tr><th>Marketplace</th><th>Result</th><th>Details</th></tr>{''.join(rows)}</table>"
            f"<p>Full output of every pusher is in the attached log.</p>")


# ----------------------------------------------------------------- email

def send_email(subject, text, html_body, attachments=()):
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = [x.strip() for x in (os.environ.get("REPORT_TO") or sender or "").split(",") if x.strip()]
    missing = [k for k, v in (("GMAIL_ADDRESS", sender), ("GMAIL_APP_PASSWORD", password)) if not v]
    if missing:
        raise RuntimeError("missing " + ", ".join(missing) + " (set them in .env)")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for name, data in attachments:
        msg.add_attachment(data.encode("utf-8"), maintype="text", subtype="plain", filename=name)

    local = host in ("127.0.0.1", "localhost")
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        elif not local:
            server.quit()
            raise RuntimeError(f"{host}:{port} offers no TLS; refusing to send the password in clear text")
    with server:
        server.login(sender, password.replace(" ", ""))  # Google shows app passwords with spaces
        server.send_message(msg, to_addrs=to)
    return to


# ------------------------------------------------------------------ main

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Run all enabled pushers and email one report")
    ap.add_argument("--only", help="comma-separated pushers to run, e.g. vinted,amazon")
    ap.add_argument("--dry-run", action="store_true",
                    help="run each pusher in its safe mode (--dry-run / --validate-only)")
    ap.add_argument("--no-email", action="store_true", help="print the report, send nothing")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    a = ap.parse_args()

    load_env_file(ENV_FILE)
    if a.test_email:
        try:
            to = send_email("[Product push] test email",
                            "If you can read this, push_all.py can send reports.", None)
        except Exception as e:
            sys.exit(f"Test email failed: {e}")
        print(f"Test email sent to {', '.join(to)}.")
        return

    cfg = load_config()
    if cfg.get("email", {}).get("to") and not os.environ.get("REPORT_TO"):
        to = cfg["email"]["to"]
        os.environ["REPORT_TO"] = ",".join(to) if isinstance(to, list) else str(to)
    only = {x.strip() for x in a.only.split(",")} if a.only else None
    if only and only - set(PUSHERS):
        sys.exit(f"Unknown pusher(s): {', '.join(sorted(only - set(PUSHERS)))}. Known: {', '.join(PUSHERS)}")

    take_lock()
    try:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        log_dir = LOG_DIR / time.strftime("%Y-%m-%d_%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for name, spec in PUSHERS.items():
            settings = cfg.get("pushers", {}).get(name, {})
            if only is not None and name not in only:
                continue
            if only is None and not settings.get("enabled"):
                continue
            results.append(run_pusher(name, spec, settings, cfg, a, log_dir))
    finally:
        LOCK_FILE.unlink(missing_ok=True)

    if not results:
        sys.exit(f"No pushers enabled. Set \"enabled\": true in {CONFIG_FILE.name} or use --only.")
    subject = subject_of(results, a.dry_run)
    text = text_report(results, started, a.dry_run)
    (log_dir / "report.txt").write_text(subject + "\n\n" + text, encoding="utf-8")
    print("\n" + "=" * 70 + "\n" + subject + "\n\n" + text)

    failed = any(r["status"] == "FAILED" for r in results)
    when = cfg.get("email", {}).get("when", "always")
    trouble = failed or any(r["status"] == "PROBLEMS" for r in results)
    if a.no_email or (when == "problems" and not trouble):
        print("\n(no email sent)")
    else:
        combined = "\n\n".join(f"{'#' * 70}\n# {r['label']}\n{'#' * 70}\n" + r["log"].read_text(encoding="utf-8")
                               for r in results if r["log"])
        try:
            to = send_email(subject, text, html_report(results, started, a.dry_run),
                            [("push_log.txt", combined)] if combined else [])
            print(f"\nReport emailed to {', '.join(to)}.")
        except Exception as e:
            print(f"\nCould not send the report email: {e}")
            sys.exit(2)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
