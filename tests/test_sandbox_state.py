#!/usr/bin/env python3
"""
Sandbox safety: a test run against a marketplace's sandbox must never touch the
files the real runs use (the "posted" state, plus the Allegro login token and the
Vinted / OTTO caches). Otherwise a book listed in a sandbox would count as done on
the real site, and stock_sync would then try to change stock for it.

Each pusher is started in a child process with its API class swapped for a stub
that only prints the file names the pusher would use. Nothing goes to the network
and no file is written.

    python tests/test_sandbox_state.py
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


# Pushers with a --sandbox flag: module, API class to stub, argv, names of the file globals to print
FLAG = [
    ("amazon_push", "SPAPI", ["status"], ["STATE_FILE"]),
    ("fnac_push", "Fnac", ["status"], ["STATE_FILE"]),
    ("vinted_api_push", "VintedPro", ["status", "--token", "a,b"], ["STATE_FILE", "ONTOLOGY_CACHE"]),
]
STUB = """
import sys
import {mod} as m
def stub(*a, **k):
    print("FILES=" + "|".join(str(getattr(m, n)) for n in {names!r}))
    raise SystemExit(0)
m.{cls} = stub
sys.argv = ["{mod}.py"] + {argv!r}
m.main()
"""
# Pushers where the sandbox is chosen by an environment variable (no flag)
ENV = [
    ("allegro_push", {"ALLEGRO_SANDBOX": "1"}, ["STATE_FILE", "TOKEN_FILE"]),
    ("allegro_push", {"ALLEGRO_API_URL": "https://api.allegro.pl.allegrosandbox.pl"}, ["STATE_FILE", "TOKEN_FILE"]),
    ("cdon_push", {"CDON_API_URL": "https://merchants-api.sandbox.cdon.com/api"}, ["STATE_FILE"]),
    ("otto_push", {"OTTO_API_URL": "https://sandbox.api.otto.market"}, ["STATE_FILE", "BRANDS_CACHE"]),
]
PRINT = "import {mod} as m; print('FILES=' + '|'.join(str(getattr(m, n)) for n in {names!r}))"

# env variables that would switch a pusher into sandbox mode must not leak in from the shell
CLEAN = ("ALLEGRO_SANDBOX", "ALLEGRO_API_URL", "CDON_API_URL", "OTTO_API_URL")


def child(code, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in CLEAN}
    env.update(env_extra or {}, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    line = next((l for l in r.stdout.splitlines() if l.startswith("FILES=")), None)
    return (line[6:].split("|") if line else None), (r.stdout + r.stderr)[-400:]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n=== sandbox runs keep their own files ===")

    for mod, cls, argv, names in FLAG:
        real, out = child(STUB.format(mod=mod, cls=cls, argv=argv, names=names))
        sand, out2 = child(STUB.format(mod=mod, cls=cls, argv=argv + ["--sandbox"], names=names))
        check(f"{mod}: without --sandbox it uses its normal files", real is not None and not any("sandbox" in f for f in real),
              f"{real} {out}")
        check(f"{mod} --sandbox: own files, none shared with the real run",
              sand is not None and all("sandbox" in f for f in sand) and not set(sand) & set(real or []),
              f"{sand} {out2}")

    for mod, env, names in ENV:
        label = f"{mod} ({', '.join(env)})"
        real, out = child(PRINT.format(mod=mod, names=names))
        sand, out2 = child(PRINT.format(mod=mod, names=names), env)
        check(f"{mod}: normal run uses its normal files", real is not None and not any("sandbox" in f for f in real),
              f"{real} {out}")
        check(f"{label}: own files, none shared with the real run",
              sand is not None and all("sandbox" in f for f in sand) and not set(sand) & set(real or []),
              f"{sand} {out2}")

    # a local mock server URL (what the other test suites use) must not count as a sandbox
    for mod, var, names in (("allegro_push", "ALLEGRO_API_URL", ["STATE_FILE", "TOKEN_FILE"]),
                            ("cdon_push", "CDON_API_URL", ["STATE_FILE"]),
                            ("otto_push", "OTTO_API_URL", ["STATE_FILE", "BRANDS_CACHE"])):
        files, out = child(PRINT.format(mod=mod, names=names), {var: "http://127.0.0.1:8765"})
        check(f"{mod}: a local mock URL is not treated as a sandbox",
              files is not None and not any("sandbox" in f for f in files), f"{files} {out}")

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
