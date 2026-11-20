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
LOG_KEEP_DAYS = 60       # logs/<date_time> folders older than this are deleted (config: "log_keep_days")
LOCK_FILE = BASE / "push_all.lock"
LOCK_MAX_AGE = 6 * 3600  # an older lock is treated as left over from a crash

# name -> how to run it. "sub": the script uses a `push` subcommand.
# "dry": the flags for its safe mode (used with --dry-run).
PUSHERS = {
    "vinted": dict(label="Vinted", script="vinted_api_push.py", sub=True, yes=True,
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
    "fruugo": dict(label="Fruugo", script="fruugo_push.py", sub=True, yes=True,
                   state="fruugo_posted.json", dry=["--dry-run"],
                   env=["FRUUGO_FEED_URL"]),
    "cdon": dict(label="CDON", script="cdon_push.py", sub=True, yes=True,
                 state="cdon_posted.json", dry=["--dry-run"],
                 env=["CDON_MERCHANT_ID", "CDON_API_TOKEN"]),
    "onbuy": dict(label="OnBuy", script="onbuy_push.py", sub=True, yes=True,
                  state="onbuy_posted.json", dry=["--dry-run"],
                  env=["ONBUY_CONSUMER_KEY", "ONBUY_SECRET_KEY"]),
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
        if value[:1] in ("'", '"') and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]          # quoted: everything up to the closing quote
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()   # unquoted: a trailing "# comment" is not part of it
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


def config_problems(cfg):
    """Mistakes in push_all_config.json, found before anything runs (a typo must not stop a run half way)."""
    probs = []

    def number(v, zero_ok=False):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and (v >= 0 if zero_ok else v > 0)

    if not isinstance(cfg, dict):
        return ["the file must contain one JSON object"]
    if "json_file" in cfg and not isinstance(cfg["json_file"], str):
        probs.append('"json_file" must be a path in quotes')
    if "timeout_minutes" in cfg and not number(cfg["timeout_minutes"]):
        probs.append('"timeout_minutes" must be a number of minutes above 0')
    if "log_keep_days" in cfg and not number(cfg["log_keep_days"], zero_ok=True):
        probs.append('"log_keep_days" must be a number of days (0 = keep every log)')
    email = cfg.get("email", {})
    if not isinstance(email, dict):
        probs.append('"email" must be an object like {"to": [...], "when": "always"}')
    else:
        if "when" in email and email["when"] not in ("always", "problems"):
            probs.append('"email.when" must be "always" or "problems"')
        if "to" in email and not isinstance(email["to"], (str, list)):
            probs.append('"email.to" must be an address or a list of addresses')
    sync = cfg.get("stock_sync", {})
    if not isinstance(sync, dict):
        probs.append('"stock_sync" must be an object like {"enabled": true}')
    else:
        if "on_failure" in sync and sync["on_failure"] not in ("skip", "continue"):
            probs.append('"stock_sync.on_failure" must be "skip" or "continue"')
        if "every_minutes" in sync and not number(sync["every_minutes"]):
            probs.append('"stock_sync.every_minutes" must be a number of minutes above 0')
    pushers = cfg.get("pushers", {})
    if not isinstance(pushers, dict):
        return probs + ['"pushers" must be an object']
    for name, s in pushers.items():
        if not isinstance(s, dict):
            probs.append(f'pushers.{name} must be an object like {{"enabled": true, "args": []}}')
            continue
        if "enabled" in s and not isinstance(s["enabled"], bool):
            probs.append(f'pushers.{name}.enabled must be true or false (without quotes)')
        if "args" in s and not (isinstance(s["args"], list) and all(isinstance(x, (str, int, float)) for x in s["args"])):
            probs.append(f'pushers.{name}.args must be a list like ["--markets", "all"]')
        if "timeout_minutes" in s and not number(s["timeout_minutes"]):
            probs.append(f'pushers.{name}.timeout_minutes must be a number of minutes above 0')
    return probs


def prune_logs(keep_days):
    """Delete logs/<YYYY-mm-dd_HHMMSS> run folders older than keep_days. Nothing else is touched."""
    import shutil
    if not keep_days or not LOG_DIR.is_dir():
        return 0
    cutoff = time.time() - float(keep_days) * 86400
    removed = 0
    for d in LOG_DIR.iterdir():
        if not d.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", d.name):
            continue
        try:
            started = time.mktime(time.strptime(d.name, "%Y-%m-%d_%H%M%S"))
        except ValueError:
            continue
        if started < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def pid_alive(pid):
    """Is a process with this id running? On Windows os.kill(pid, 0) would END the process, so ask Windows."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5                    # access denied: it exists, it's just not ours
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259                      # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                                                # e.g. permission denied: it exists
    return True


def take_lock():
    """One run at a time. A lock left by a run that was killed (timeout, reboot) is taken over at once; a lock
    held by a live process blocks for at most LOCK_MAX_AGE."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - LOCK_FILE.stat().st_mtime
        try:
            holder = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            holder = None                                          # unreadable: judge by age only
        if age < LOCK_MAX_AGE and (holder is None or pid_alive(holder)):
            sys.exit(f"Another push_all run is in progress ({LOCK_FILE.name} exists). "
                     f"Delete it if that run crashed.")
        if holder is not None and not pid_alive(holder):
            print(f"(removed {LOCK_FILE.name} left by run {holder}, which is no longer running)")
        LOCK_FILE.unlink(missing_ok=True)
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            sys.exit(f"Another push_all run has just started ({LOCK_FILE.name} exists).")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


# ------------------------------------------------------------------- run

def read_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


def failure_result(name, label, note):
    """A FAILED row for something that could not run (or crashed), so it shows in the report and the subject."""
    return {"name": name, "label": label, "pushed": 0, "skipped": 0, "already": 0, "problems": [], "tail": [],
            "exit_code": None, "seconds": 0, "log": None, "status": "FAILED", "note": note}


def run_pusher(name, spec, settings, cfg, a, log_dir):
    res = {"name": name, "label": spec["label"], "pushed": 0, "skipped": 0, "already": 0,
           "problems": [], "tail": [], "exit_code": None, "seconds": 0, "log": None}
    missing = [k for k in required_env(name, spec, settings) if not os.environ.get(k)]
    # A dry run with --dry-run only writes the pusher's preview files and needs no credentials. Vinted and Amazon
    # validate against the API (--validate-only), so they do need them.
    offline_dry = a.dry_run and spec["dry"] == ["--dry-run"]
    if missing and not offline_dry:
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


def run_stock_sync(cfg, a, log_dir, tag="stock_sync", label="Stock sync"):
    """stock_sync.py first (inside our lock): new orders -> books_synced.json + stock 0 everywhere."""
    res = {"name": tag, "label": label, "pushed": 0, "skipped": 0, "already": 0,
           "problems": [], "tail": [], "exit_code": None, "seconds": 0, "log": None}
    cmd = [sys.executable, str(BASE / "stock_sync.py"), "sync"]
    if cfg.get("json_file"):
        cmd += ["--source", str(cfg["json_file"])]
    if a.dry_run:
        cmd.append("--dry-run")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PUSH_ALL_RUNNING="1")
    start = time.time()
    print(f"\n=== {label}: {' '.join(cmd[1:])}", flush=True)
    try:
        r = subprocess.run(cmd, cwd=BASE, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=float(cfg.get("timeout_minutes", 60)) * 60,
                           encoding="utf-8", errors="replace")
        output, res["exit_code"] = r.stdout, r.returncode
    except subprocess.TimeoutExpired:
        output, res["exit_code"] = "", "timeout"
    res["seconds"] = round(time.time() - start)
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    log = log_dir / f"{tag}.log"
    log.write_text(f"$ {' '.join(cmd)}\n\n{output}", encoding="utf-8")
    res["log"] = log
    lines = output.splitlines()
    res["problems"] = [l.strip() for l in lines if RE_PROBLEM.search(l)]
    res["tail"] = [l for l in lines if l.strip()][-4:]
    summary = next((l.strip() for l in lines if "new sale(s)" in l), "")
    if res["exit_code"] != 0:
        res.update(status="FAILED", note=f"exit code {res['exit_code']}: {res['tail'][-1] if res['tail'] else ''}")
    else:
        res.update(status="PROBLEMS" if res["problems"] else "OK", note=summary)
    return res


# ---------------------------------------------------------------- report

def summary_line(r):
    if r["status"] in ("NOT CONFIGURED", "DISABLED") or str(r.get("name", "")).startswith("stock_sync"):
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
    # a marketplace that is switched on but did not run (a credential went missing) must not look like a normal day
    unconfigured = [r["label"] for r in results if r["status"] == "NOT CONFIGURED"]
    if unconfigured:
        bits.append("not configured: " + ", ".join(unconfigured))
        worst = "FAILED" if failed else "PROBLEMS"
    need = sum(1 for r in orders_rows() if r["needs_you"])
    if need:
        bits.append(f"{need} order(s) need you")
        worst = "FAILED" if failed else "PROBLEMS"
    prefix = "[Product push DRY RUN]" if dry_run else "[Product push]"
    return f"{prefix} {worst} - " + ", ".join(bits)


def orders_rows():
    path = Path(os.environ.get("STOCK_SYNC_DIR") or BASE) / "orders_report.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def orders_text(rows):
    if not rows:
        return []
    need = [r for r in rows if r["needs_you"]]
    out = ["", f"ORDERS: {sum(1 for r in rows if r['new'])} new" + (f", {len(need)} NEED YOU" if need else "")]
    for r in sorted(rows, key=lambda r: (not r["needs_you"], r["at"])):
        flag = "!!" if r["needs_you"] else "  "
        out.append(f"{flag} {r['at'][:16].replace('T', ' ')}  {r['channel']:<13} order {r['order']:<14} "
                   f"{r['qty']} x {r['sku']} {r['title'][:40]}  -> {r['status']}")
    return out


def orders_html(rows):
    if not rows:
        return ""
    need = sum(1 for r in rows if r["needs_you"])
    trs = "".join(
        f"<tr style='background:{'#fdecea' if r['needs_you'] else '#ffffff'}'>"
        f"<td style='padding:4px 8px'>{html.escape(r['at'][:16].replace('T', ' '))}</td>"
        f"<td style='padding:4px 8px'>{html.escape(r['channel'])}</td>"
        f"<td style='padding:4px 8px'>{html.escape(str(r['order']))}</td>"
        f"<td style='padding:4px 8px'>{r['qty']} x {html.escape(r['sku'])} {html.escape(r['title'][:50])}</td>"
        f"<td style='padding:4px 8px'><b>{html.escape(r['status'])}</b></td></tr>"
        for r in sorted(rows, key=lambda r: (not r["needs_you"], r["at"])))
    return (f"<h3>Orders: {sum(1 for r in rows if r['new'])} new" + (f", <span style='color:#c0392b'>{need} need you</span>" if need else "")
            + "</h3><table style='border-collapse:collapse' border='1' cellspacing='0'>"
            "<tr><th>Time (UTC)</th><th>Marketplace</th><th>Order</th><th>Book</th><th>Status</th></tr>"
            f"{trs}</table>")


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
    out += orders_text(orders_rows())
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
            f"{orders_html(orders_rows())}"
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

LOG_HEAD_CHARS = 2000        # of each pusher's log, the email carries the first part (the command)...
LOG_TAIL_CHARS = 300_000     # ...and the last part; the rest stays in logs/ (Gmail refuses mail over 25 MB)


def log_for_email(path):
    text = path.read_text(encoding="utf-8")
    if len(text) <= LOG_HEAD_CHARS + LOG_TAIL_CHARS:
        return text
    cut = len(text) - LOG_HEAD_CHARS - LOG_TAIL_CHARS
    return (text[:LOG_HEAD_CHARS] + f"\n[... {cut} characters cut to keep the email small; the full log is {path} ...]\n"
            + text[-LOG_TAIL_CHARS:])


def main():
    if hasattr(sys.stdout, "reconfigure"):
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
    probs = config_problems(cfg)
    if probs:
        sys.exit(f"Settings problems in {CONFIG_FILE.name}:\n  - " + "\n  - ".join(probs))
    if cfg.get("email", {}).get("to") and not os.environ.get("REPORT_TO"):
        to = cfg["email"]["to"]
        os.environ["REPORT_TO"] = ",".join(to) if isinstance(to, list) else str(to)
    only = {x.strip() for x in a.only.split(",")} if a.only else None
    if only and only - set(PUSHERS):
        sys.exit(f"Unknown pusher(s): {', '.join(sorted(only - set(PUSHERS)))}. Known: {', '.join(PUSHERS)}")

    take_lock()
    try:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        pruned = prune_logs(cfg.get("log_keep_days", LOG_KEEP_DAYS))
        if pruned:
            print(f"(removed {pruned} log folder(s) older than {cfg.get('log_keep_days', LOG_KEEP_DAYS)} days)")
        log_dir = LOG_DIR / time.strftime("%Y-%m-%d_%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=True)
        results = []
        sync_cfg = cfg.get("stock_sync", {})
        sync_on = bool(sync_cfg.get("enabled")) and only is None
        resync_every = float(sync_cfg.get("every_minutes", 15)) * 60
        synced = Path(os.environ.get("STOCK_SYNC_DIR") or BASE) / "books_synced.json"
        todo = [(name, spec, cfg.get("pushers", {}).get(name, {})) for name, spec in PUSHERS.items()
                if (name in only if only is not None else cfg.get("pushers", {}).get(name, {}).get("enabled"))]
        stop = None                                  # why no pusher may run today, if that is the case
        products = cfg.get("json_file")
        if products and not Path(products).exists():
            stop = f"products file not found: {products}"
            results.append(failure_result("products_file", "Products file", f"not found: {products}"))
        elif sync_on:
            t0 = time.time()
            sync = run_stock_sync(cfg, a, log_dir)
            results.append(sync)
            # Even a sync that exits with an error (a pusher failed inside it) has written a fresh books_synced.json
            # first. Only if it did not, the stock would be out of date: then nothing is pushed from raw stock.
            fresh = synced.exists() and synced.stat().st_mtime >= t0 - 2
            if sync["status"] != "FAILED":
                if synced.exists():
                    cfg = dict(cfg, json_file=str(synced))     # pushers list the synced stock
            elif fresh:
                cfg = dict(cfg, json_file=str(synced))
            elif sync_cfg.get("on_failure", "skip") == "skip":
                stop = "the stock sync failed, so the stock would be out of date"
            else:
                print("! The stock sync failed: pushing from the UNSYNCED products file (stock_sync.on_failure = continue).")
            last_sync = time.time()
        if stop:
            for name, spec, _ in todo:
                results.append(failure_result(name, spec["label"], f"not run: {stop}"))
        else:
            for name, spec, settings in todo:
                # a long run must not leave the orders unread for hours: read them again between pushers
                if sync_on and time.time() - last_sync >= resync_every:
                    again = run_stock_sync(cfg, a, log_dir, tag="stock_sync_again", label="Stock sync (again)")
                    last_sync = time.time()
                    if again["status"] != "OK":
                        results.append(again)
                try:
                    results.append(run_pusher(name, spec, settings, cfg, a, log_dir))
                except Exception as e:                    # one pusher's crash must not stop the others or the report
                    results.append(failure_result(name, spec["label"], f"internal error: {type(e).__name__}: {e}"))
    finally:
        LOCK_FILE.unlink(missing_ok=True)

    if not results:
        sys.exit(f"No pushers enabled. Set \"enabled\": true in {CONFIG_FILE.name} or use --only.")
    subject = subject_of(results, a.dry_run)
    text = text_report(results, started, a.dry_run)
    (log_dir / "report.txt").write_text(subject + "\n\n" + text, encoding="utf-8")
    print("\n" + "=" * 70 + "\n" + subject + "\n\n" + text)

    failed = any(r["status"] == "FAILED" for r in results)
    reported = [r["key"] for r in orders_rows() if r["new"]]
    when = cfg.get("email", {}).get("when", "always")
    trouble = (failed or any(r["status"] in ("PROBLEMS", "NOT CONFIGURED") for r in results)
               or any(r["needs_you"] for r in orders_rows()))
    if a.no_email or (when == "problems" and not trouble):
        print("\n(no email sent)")
    else:
        combined = "\n\n".join(f"{'#' * 70}\n# {r['label']}\n{'#' * 70}\n" + log_for_email(r["log"])
                               for r in results if r["log"])
        attachments = [("push_log.txt", combined)] if combined else []
        try:
            try:
                to = send_email(subject, text, html_report(results, started, a.dry_run), attachments)
            except smtplib.SMTPException as e:
                if not attachments:
                    raise
                # the mail server refused the email with the log attached (too big): send the report without it
                note = (f"The log attachment was left out because the mail server refused the email with it ({e}). "
                        f"The full logs are in {log_dir}.")
                print(f"\n({note})")
                to = send_email(subject, text + "\n\n" + note,
                                html_report(results, started, a.dry_run) + f"<p>{html.escape(note)}</p>", [])
            print(f"\nReport emailed to {', '.join(to)}.")
            if reported and not a.dry_run:                  # these orders were in an email: no longer "new"
                import stock_sync
                stock_sync.mark_reported(reported)
        except Exception as e:
            print(f"\nCould not send the report email: {e}")
            sys.exit(2)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
