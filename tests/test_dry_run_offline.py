#!/usr/bin/env python3
"""
Every pusher must be able to preview its offers with no account: `push --dry-run`
with no credentials in the environment has to succeed and write its preview file,
so you can check your data before you have a marketplace account.

Each pusher runs in a temporary folder with every credential variable removed, on
tests/test_products.json. Nothing goes to the network.

    python tests/test_dry_run_offline.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS = HERE / "test_products.json"
RESULTS = []

# Pushers without a --dry-run (Amazon, Vinted) or with their own command line (eBay, tested elsewhere)
SKIP = {"amazon_push.py", "vinted_api_push.py", "ebay_push.py"}
# Mirakl pushers: the preview file each one writes
MIRAKL = {"carrefour": "carrefour_offers.csv", "casadellibro": "casadellibro_offers.csv",
          "elcorteingles": "elcorteingles_offers.csv", "empik": "empik_offers.csv",
          "ibs": "ibs_offers.csv", "leclerc": "leclerc_offers.csv", "worten": "worten_offers.csv"}
CRED = re.compile(r"(_API_KEY|_API_URL|_CLIENT_ID|_CLIENT_SECRET|_TOKEN|_KEY|_SECRET|_PASSWORD|"
                  r"_SHOP_ID|_PARTNER_ID|_MERCHANT_ID|_REFRESH_TOKEN|_SANDBOX)$")


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n=== push --dry-run works without credentials ===")
    env = {k: v for k, v in os.environ.items() if not CRED.search(k)}
    env.update(PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    for f in sorted(ROOT.glob("*_push.py")):
        if f.name in SKIP:
            continue
        name = f.name[: -len("_push.py")]
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, str(f), "push", str(PRODUCTS), "--dry-run"], cwd=tmp, env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            full = r.stdout + r.stderr
            ok = r.returncode == 0 and "Traceback" not in full and "Missing environment" not in full
            if name in MIRAKL:
                ok = ok and (Path(tmp) / MIRAKL[name]).exists() and "dry run without credentials" in full
            check(f"{name}: dry run without credentials", ok, f"exit {r.returncode}: {full[-500:]}")
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
