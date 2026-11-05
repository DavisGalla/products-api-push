#!/usr/bin/env python3
"""
Push products to the Worten marketplace (Portugal and Spain) through its
seller API. Worten's marketplace runs on Mirakl, so this uses the standard
Mirakl Marketplace Platform seller API.

Each product is matched to an EXISTING Worten catalog product by its
ISBN/EAN, and your offer (price, stock, condition) is uploaded with an offers
import (Mirakl OF01). Books not in their catalog are skipped.

ONE-TIME SETUP (see docs/worten_push.md)
  In your Worten seller back office (Mirakl): your user menu ->
  "API Key" / "Chave API". Copy the key and the API base URL (shown in the
  back office's API documentation page, or ask Worten seller support).
  Worten doesn't publish its seller API docs; this uses Mirakl's standard
  seller calls. One Worten shop sells in Portugal and Spain (sales channels).
  Then (PowerShell):
      $env:WORTEN_API_URL = "https://<your-instance>.mirakl.net"
      $env:WORTEN_API_KEY = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

USAGE
    pip install requests
    python worten_push.py check                 # test the key, show your shop
    python worten_push.py states                # list condition codes
    python worten_push.py push --dry-run        # build the CSV, upload nothing
    python worten_push.py push --limit 1        # one real offer
    python worten_push.py push                  # everything
    python worten_push.py push --resend         # re-send all (price/stock sync)
    python worten_push.py status                # what's live

NOTE: new books have a legally fixed price in Portugal (Decreto-Lei 176/96,
max 10% discount) and in Spain (Ley 10/2007, max 5% discount). Don't use
--price-factor to sell above the cover price.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

# Source condition -> Worten offer state code. Run `states` to see
# the codes your marketplace uses. "11" is Mirakl's default code for "New".
# Products whose condition has no code here are skipped.
STATE_CODES = {
    "NewCondition": "11",
    # "UsedCondition": "?",   # fill in from `python worten_push.py states`
}

# How Worten identifies products in the offer file.
PRODUCT_ID_TYPE = "EAN"     # ISBN-13 is an EAN
DEFAULT_LEADTIME_DAYS = 3   # days until you ship (leadtime-to-ship)
# ======================================================================

STATE_FILE = Path("worten_posted.json")
CSV_FILE = Path("worten_offers.csv")
OFFER_COLUMNS = ["sku", "product-id", "product-id-type", "description", "internal-description",
                 "price", "quantity", "state", "leadtime-to-ship", "logistic-class", "update-delete"]


# --------------------------------------------------------------------------- API

class Mirakl:
    def __init__(self):
        url, key = os.environ.get("WORTEN_API_URL"), os.environ.get("WORTEN_API_KEY")
        missing = [k for k, v in (("WORTEN_API_URL", url), ("WORTEN_API_KEY", key)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.base = url.rstrip("/")
        if self.base.endswith("/api"):
            self.base = self.base[:-4]
        self.headers = {"Authorization": key, "Accept": "application/json"}
        self.shop_id = os.environ.get("WORTEN_SHOP_ID")  # only if your key manages several shops

    def call(self, method, path, params=None, files=None, data=None, raw=False, retries=4):
        params = dict(params or {})
        if self.shop_id:
            params["shop_id"] = self.shop_id
        for attempt in range(retries):
            r = requests.request(method, f"{self.base}/api{path}", params=params, files=files, data=data,
                                 headers=self.headers, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 5 * 2 ** attempt
                print(f"   (API returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                sys.exit("401 Unauthorized: check WORTEN_API_KEY (and that the URL is your shop's instance).")
            if raw:
                return r.status_code, r.content
            try:
                return r.status_code, (r.json() if r.content else {})
            except ValueError:
                return r.status_code, {"raw": r.text[:500]}
        return r.status_code, {"message": "gave up after retries"}

    def account(self):                                   # A01
        return self.call("GET", "/account")

    def states(self):                                    # offer condition list
        return self.call("GET", "/offers/states")

    def find_products(self, eans):                       # P31, max 100 references per call
        found = {}
        for i in range(0, len(eans), 100):
            chunk = eans[i:i + 100]
            code, data = self.call("GET", "/products", params={
                "product_references": ",".join(f"{PRODUCT_ID_TYPE}|{e}" for e in chunk)})
            if code != 200:
                raise RuntimeError(f"product lookup failed ({code}): {data}")
            for prod in data.get("products", []):
                for ref in prod.get("product_references", []):
                    if ref.get("reference") in chunk:
                        found[ref["reference"]] = prod.get("product_title") or prod.get("product_sku")
        return found

    def import_offers(self, csv_bytes):                  # OF01
        code, data = self.call("POST", "/offers/imports", data={"import_mode": "NORMAL"},
                               files={"file": ("offers.csv", csv_bytes, "text/csv")})
        if code not in (200, 201) or "import_id" not in data:
            raise RuntimeError(f"offer import rejected ({code}): {data}")
        return data["import_id"]

    def import_status(self, import_id):                  # OF02
        return self.call("GET", f"/offers/imports/{import_id}")

    def error_report(self, import_id):                   # OF03
        return self.call("GET", f"/offers/imports/{import_id}/error_report", raw=True)

    def offers(self):                                    # OF21, paginated
        out, offset = [], 0
        while True:
            code, data = self.call("GET", "/offers", params={"max": 100, "offset": offset})
            if code != 200:
                raise RuntimeError(f"offer list failed ({code}): {data}")
            page = data.get("offers", [])
            out += page
            offset += len(page)
            if not page or offset >= data.get("total_count", 0):
                return out


# ---------------------------------------------------------------------- helpers

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def to_ean(code):
    """Normalise an ISBN-10/13 or EAN to a 13-digit EAN, or return None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    if len(c) == 13 and c.isdigit():
        return c
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        core = "978" + c[:9]
        check = (10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10
        return core + str(check)
    return None


def normalize(raw):
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:40],
        "title": raw.get("title") or "",
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "quantity": raw.get("quantity"),
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (needed to match the Worten catalog)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in STATE_CODES:
        probs.append(f"no state code for condition '{p['condition']}' (set STATE_CODES, see `states`)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def money(price, factor):
    """price * factor as a 2-decimal string, rounded half-up (8.075 -> 8.08)."""
    v = Decimal(str(price)) * Decimal(str(factor))
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build_csv(rows, a):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=OFFER_COLUMNS, delimiter=";", lineterminator="\n")
    w.writeheader()
    for p in rows:
        w.writerow({
            "sku": p["sku"],
            "product-id": p["ean"],
            "product-id-type": PRODUCT_ID_TYPE,
            "description": "",
            "internal-description": p["title"][:255],
            "price": money(p["price"], a.price_factor),
            "quantity": int(p["quantity"] or a.quantity),
            "state": STATE_CODES[p["condition"]],
            "leadtime-to-ship": a.leadtime,
            "logistic-class": a.logistic_class or "",
            "update-delete": "update",
        })
    return buf.getvalue().encode("utf-8")


def parse_error_report(content):
    """Return {sku: message} from an OF03 error report CSV."""
    text = content.decode("utf-8-sig", errors="replace")
    sample = text.split("\n", 1)[0]
    reader = csv.DictReader(io.StringIO(text), delimiter=";" if ";" in sample else ",")
    errors = {}
    for row in reader:
        msg = row.get("error-message") or row.get("error_message") or row.get("errors") or "error"
        errors[row.get("sku") or "?"] = msg
    return errors


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    code, data = api.account()
    if code != 200:
        sys.exit(f"Account lookup failed ({code}): {data}")
    print(f"Connected to shop '{data.get('shop_name')}' (id {data.get('shop_id')}, state {data.get('shop_state')})")


def cmd_states(api, a):
    code, data = api.states()
    if code != 200:
        sys.exit(f"Could not list states ({code}): {data}")
    print("Offer state codes (use in STATE_CODES):")
    for s in data.get("offer_states", data.get("states", [])):
        print(f"   {s.get('code'):<6} {s.get('label')}")


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()

    todo = []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        probs = problems_of(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if p["sku"] in state and not a.resend:
            print(f"[{i}] already pushed: {p['title']}")
            continue
        todo.append(p)
    if a.limit:
        todo = todo[: a.limit]
    if not todo:
        print("0 offer(s) to push.")
        return

    if not a.no_match_check:
        print(f"Checking {len(todo)} ISBN(s) against the Worten catalog...")
        found = api.find_products([p["ean"] for p in todo])
        for p in [p for p in todo if p["ean"] not in found]:
            print(f"   SKIP '{p['title']}': EAN {p['ean']} is not in the Worten catalog")
        todo = [p for p in todo if p["ean"] in found]
    print(f"{len(todo)} offer(s) to push.")
    if not todo:
        return

    csv_bytes = build_csv(todo, a)
    CSV_FILE.write_bytes(csv_bytes)
    if a.dry_run:
        print(f"Dry run: offer file written to {CSV_FILE.resolve()} - nothing uploaded.")
        return
    if not a.yes:
        if input(f"This will upload {len(todo)} LIVE offer(s) to Worten. Type 'yes': ").strip().lower() != "yes":
            return

    import_id = api.import_offers(csv_bytes)
    print(f"Uploaded offer import {import_id}, waiting for Worten to process it...")
    deadline = time.time() + a.wait
    while True:
        code, st = api.import_status(import_id)
        status = st.get("status")
        if status in ("COMPLETE", "FAILED", "CANCELLED", "REJECTED"):
            break
        if time.time() > deadline:
            print(f"Still {status} after {a.wait}s. Nothing recorded; run `push` again later to retry.")
            return
        time.sleep(a.poll)
    print(f"Import {import_id}: {status}  (ok {st.get('lines_in_success', '?')}, errors {st.get('lines_in_error', '?')})")
    if status != "COMPLETE":
        print(f"   reason: {st.get('reason_status') or st}")
        return

    errors = {}
    if st.get("has_error_report") or st.get("lines_in_error"):
        code, content = api.error_report(import_id)
        if code == 200:
            errors = parse_error_report(content)
            for sku, msg in errors.items():
                print(f"   ! {sku}: {msg}")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for p in todo:
        if p["sku"] not in errors:
            state[p["sku"]] = {"ean": p["ean"], "import_id": import_id, "pushed_at": now}
    save_state(state)
    print(f"Recorded {len(todo) - len(errors)} offer(s). Run `status` to see them live.")


def cmd_status(api, a):
    state = load_state()
    offers = {o.get("shop_sku"): o for o in api.offers()}
    if not state and not offers:
        print("No offers yet.")
        return
    for sku in sorted(set(state) | set(offers)):
        o = offers.get(sku)
        if not o:
            print(f"{sku}: not found on Worten (yet)")
        else:
            live = "LIVE" if o.get("active") else "inactive"
            print(f"{sku}: {live}  price={o.get('price')}  qty={o.get('quantity')}  "
                  f"product={o.get('product_sku')}" + ("" if sku in state else "  (not pushed by this script)"))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push offers to the Worten marketplace (Mirakl)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the API key and show your shop")
    sub.add_parser("states", help="list the offer condition codes")
    p = sub.add_parser("push", help="upload offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'quantity' wins)")
    p.add_argument("--leadtime", type=int, default=DEFAULT_LEADTIME_DAYS, help="days until you ship")
    p.add_argument("--logistic-class", help="logistic class code, if your shop requires one")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--resend", action="store_true", help="also re-send already pushed offers (price/stock sync)")
    p.add_argument("--no-match-check", action="store_true", help="skip the catalog lookup before uploading")
    p.add_argument("--dry-run", action="store_true", help="write the offer CSV, upload nothing")
    p.add_argument("--limit", type=int, default=0, help="max offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--wait", type=int, default=600, help="max seconds to wait for the import to finish")
    p.add_argument("--poll", type=int, default=15, help="seconds between import status checks")
    sub.add_parser("status", help="list your offers on Worten")
    a = ap.parse_args()
    api = Mirakl()
    {"check": cmd_check, "states": cmd_states, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)


if __name__ == "__main__":
    main()
