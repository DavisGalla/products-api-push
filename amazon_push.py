#!/usr/bin/env python3
"""
Push products to the European Amazon marketplaces (Amazon.es, .de, .fr, .it,
.nl, .be, .pl, .se, .ie - optionally .co.uk) through the official Selling
Partner API (SP-API).

Each product is matched to an EXISTING Amazon catalog item by its ISBN/EAN in
each marketplace, and your offer (price, condition, stock) is added to it with
the Listings Items API ("offer-only" listing). Products a marketplace doesn't
already have are skipped there: creating new catalog pages needs much more
data than a scraped feed has.

ONE-TIME SETUP (see docs/amazon_push.md for details)
  1. Professional seller account on Seller Central Europe, with the
     marketplaces you want enabled (Settings -> Account Info -> Marketplaces).
  2. Seller Central -> Apps & Services -> Develop Apps: register a private
     SP-API app with the "Product Listing" role, then authorize it to get a
     refresh token. One token covers all EU marketplaces of the account.
  3. Set environment variables (PowerShell):
        $env:AMAZON_LWA_CLIENT_ID     = "amzn1.application-oa2-client...."
        $env:AMAZON_LWA_CLIENT_SECRET = "..."
        $env:AMAZON_REFRESH_TOKEN     = "Atzr|..."
        $env:AMAZON_SELLER_ID         = "A1B2C3D4E5F6G7"   # Seller Central -> Account Info -> Merchant Token

USAGE
    pip install requests
    python amazon_push.py push --validate-only                  # Amazon.es only (default)
    python amazon_push.py push --markets ES,DE,FR --validate-only
    python amazon_push.py push --markets all --limit 1          # every EU marketplace
    python amazon_push.py status --markets all

NOTE: new books have a legally fixed price in several countries (ES, FR, DE,
IT, NL...). Don't use --price-factor to sell new books above the cover price.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

# Marketplaces used when you don't pass --markets.
DEFAULT_MARKETS = ["ES"]

# Exchange rates used to convert your EUR prices for non-euro marketplaces:
# 1 EUR = X units. UPDATE THESE before pushing to SE / PL / UK.
EUR_RATES = {"SEK": 11.2, "PLN": 4.25, "GBP": 0.84}
# ======================================================================

# code -> (marketplace id, currency, store domain)
MARKETS = {
    "ES": ("A1RKKUPIHCS9HS", "EUR", "amazon.es"),
    "DE": ("A1PA6795UKMFR9", "EUR", "amazon.de"),
    "FR": ("A13V1IB3VIYZZH", "EUR", "amazon.fr"),
    "IT": ("APJ6JRA9NG5V4", "EUR", "amazon.it"),
    "NL": ("A1805IZSGTT6HS", "EUR", "amazon.nl"),
    "BE": ("AMEN7PMS2XZ99", "EUR", "amazon.com.be"),
    "PL": ("A1C3SOZRARQ6R3", "PLN", "amazon.pl"),
    "SE": ("A2NODRKZP88ZB9", "SEK", "amazon.se"),
    "IE": ("A28R8C7NBKEWEA", "EUR", "amazon.ie"),
    "UK": ("A1F83G8C2ARO7P", "GBP", "amazon.co.uk"),  # not EU: only used when named explicitly
}
EU_MARKETS = [m for m in MARKETS if m != "UK"]

SPAPI_HOST = "https://sellingpartnerapi-eu.amazon.com"
SANDBOX_HOST = "https://sandbox.sellingpartnerapi-eu.amazon.com"
LWA_URL = "https://api.amazon.com/auth/o2/token"
STATE_FILE = Path("amazon_posted.json")

# Schema.org condition (scraped JSON) -> Amazon condition_type.
# An Amazon value (e.g. "used_good") in the JSON is passed through as-is.
CONDITION_MAP = {
    "NewCondition": "new_new",
    "UsedCondition": "used_very_good",
    "RefurbishedCondition": "used_good",  # books have no "refurbished" condition
    "DamagedCondition": "used_acceptable",
}


# --------------------------------------------------------------------------- API

class SPAPI:
    def __init__(self, sandbox=False):
        missing = [k for k in ("AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
                               "AMAZON_REFRESH_TOKEN", "AMAZON_SELLER_ID") if not os.environ.get(k)]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.host = SANDBOX_HOST if sandbox else SPAPI_HOST
        self.seller_id = os.environ["AMAZON_SELLER_ID"]
        self.token, self.token_exp = None, 0

    def _access_token(self):
        if self.token and time.time() < self.token_exp - 60:
            return self.token
        r = requests.post(LWA_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["AMAZON_REFRESH_TOKEN"],
            "client_id": os.environ["AMAZON_LWA_CLIENT_ID"],
            "client_secret": os.environ["AMAZON_LWA_CLIENT_SECRET"],
        }, timeout=30)
        if not r.ok:
            sys.exit(f"Login failed ({r.status_code}): {r.text[:500]}\n"
                     "Check AMAZON_LWA_CLIENT_ID / _SECRET / AMAZON_REFRESH_TOKEN.")
        data = r.json()
        self.token, self.token_exp = data["access_token"], time.time() + data.get("expires_in", 3600)
        return self.token

    def call(self, method, path, params=None, body=None, retries=5):
        """Call SP-API; retries on 429 (throttled) and 5xx with backoff."""
        for attempt in range(retries):
            r = requests.request(method, self.host + path, params=params, json=body, timeout=60, headers={
                "x-amz-access-token": self._access_token(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                print(f"   (Amazon returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                data = r.json() if r.content else {}
            except ValueError:
                data = {"raw": r.text[:500]}
            return r.status_code, data
        return r.status_code, {"errors": [{"message": "gave up after retries"}]}

    def find_asin(self, isbn, mkt_id):
        """Return (asin, product_type, title) for an ISBN/EAN in one marketplace, or (None, None, None)."""
        id_type = "ISBN" if len(isbn) == 10 else "EAN"  # ISBN-13 is an EAN
        code, data = self.call("GET", "/catalog/2022-04-01/items", params={
            "identifiers": isbn, "identifiersType": id_type, "marketplaceIds": mkt_id,
            "includedData": "summaries,productTypes",
        })
        if code != 200:
            raise RuntimeError(f"catalog search failed ({code}): {data}")
        for item in data.get("items") or []:
            pt = next((x["productType"] for x in item.get("productTypes", [])
                       if x.get("marketplaceId") == mkt_id), None)
            title = next((x.get("itemName") for x in item.get("summaries", [])
                          if x.get("marketplaceId") == mkt_id), "")
            return item["asin"], pt or "PRODUCT", title or ""
        return None, None, None

    def put_offer(self, sku, mkt_id, product_type, attributes, preview=False):
        params = {"marketplaceIds": mkt_id}
        if preview:
            params["mode"] = "VALIDATION_PREVIEW"
        return self.call("PUT", f"/listings/2021-08-01/items/{self.seller_id}/{quote(sku, safe='')}",
                         params=params, body={"productType": product_type,
                                              "requirements": "LISTING_OFFER_ONLY",
                                              "attributes": attributes})

    def get_listing(self, sku, mkt_id):
        return self.call("GET", f"/listings/2021-08-01/items/{self.seller_id}/{quote(sku, safe='')}",
                         params={"marketplaceIds": mkt_id, "includedData": "summaries,issues,offers"})


# ---------------------------------------------------------------------- helpers

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_markets(text):
    if not text:
        return list(DEFAULT_MARKETS)
    if text.strip().lower() == "all":
        return list(EU_MARKETS)
    codes = [c.strip().upper() for c in text.split(",") if c.strip()]
    bad = [c for c in codes if c not in MARKETS]
    if bad:
        sys.exit(f"Unknown market(s) {', '.join(bad)}. Choose from {', '.join(MARKETS)} or 'all'.")
    return list(dict.fromkeys(codes))  # dedupe, keep order


def normalize(raw):
    """Accept the scraper's JSON or the simple format."""
    code = str(raw.get("isbn") or raw.get("ean") or "").replace("-", "").replace(" ", "")
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:40],  # Amazon SKU max 40 chars
        "title": raw.get("title") or "",
        "price": raw.get("price_eur", raw.get("price")),
        "isbn": code,
        "condition": CONDITION_MAP.get(raw.get("condition"), raw.get("condition") or "new_new"),
        "quantity": raw.get("quantity"),
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    if not p["isbn"]:
        probs.append("no ISBN/EAN (needed to match the Amazon catalog)")
    elif not (p["isbn"].isdigit() and len(p["isbn"]) in (10, 13)) and not (
            len(p["isbn"]) == 10 and p["isbn"][:9].isdigit() and p["isbn"][9] in "Xx"):
        probs.append(f"invalid ISBN/EAN '{p['isbn']}'")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def local_price(eur, currency, factor):
    rate = 1.0 if currency == "EUR" else EUR_RATES[currency]
    return round(float(eur) * factor * rate, 2)


def offer_attributes(p, asin, market, a):
    mkt_id, currency, _ = MARKETS[market]
    availability = {"fulfillment_channel_code": "DEFAULT", "quantity": int(p["quantity"] or a.quantity)}
    if a.handling_days:
        availability["lead_time_to_ship_max_days"] = a.handling_days
    return {
        "merchant_suggested_asin": [{"value": asin, "marketplace_id": mkt_id}],
        "condition_type": [{"value": p["condition"], "marketplace_id": mkt_id}],
        "purchasable_offer": [{
            "marketplace_id": mkt_id, "currency": currency,
            "our_price": [{"schedule": [{"value_with_tax": local_price(p["price"], currency, a.price_factor)}]}],
        }],
        "fulfillment_availability": [availability],
    }


def issues_text(issues):
    return "; ".join(f"[{i.get('severity')}] {i.get('code')}: {i.get('message')}" for i in issues or [])


# --------------------------------------------------------------------- commands

def cmd_push(api, a):
    markets = parse_markets(a.markets)
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}")
    print(f"Marketplaces: {', '.join(f'{m} ({MARKETS[m][2]}, {MARKETS[m][1]})' for m in markets)}")
    for m in markets:
        if MARKETS[m][1] != "EUR":
            print(f"   note: {m} prices converted at 1 EUR = {EUR_RATES[MARKETS[m][1]]} {MARKETS[m][1]} (EUR_RATES)")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()

    todo = []  # (index, product, [markets still to do])
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        probs = problems_of(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        pending = [m for m in markets
                   if a.validate_only or state.get(f"{m}:{p['sku']}", {}).get("status") != "ACCEPTED"]
        if not pending:
            print(f"[{i}] already pushed to {', '.join(markets)}: {p['title']}")
            continue
        todo.append((i, p, pending))
    if a.limit:
        todo = todo[: a.limit]
    n_offers = sum(len(ms) for _, _, ms in todo)
    print(f"{len(todo)} product(s), up to {n_offers} offer(s) to {'validate' if a.validate_only else 'push'}.")
    if not todo:
        return

    if not (a.validate_only or a.yes):
        if input(f"This will create up to {n_offers} LIVE offer(s) on Amazon. Type 'yes': ").strip().lower() != "yes":
            return

    totals = {"ok": 0, "not_in_catalog": 0, "rejected": 0}
    for n, (i, p, pending) in enumerate(todo, 1):
        print(f"[{i}] ({n}/{len(todo)}) {p['title']}")
        for m in pending:
            mkt_id = MARKETS[m][0]
            try:
                asin, ptype, amz_title = api.find_asin(p["isbn"], mkt_id)
                time.sleep(0.6)  # catalog search rate limit is 2/s
                if not asin:
                    print(f"   {m}: SKIP - ISBN {p['isbn']} is not in the {MARKETS[m][2]} catalog")
                    totals["not_in_catalog"] += 1
                    continue
                code, data = api.put_offer(p["sku"], mkt_id, ptype, offer_attributes(p, asin, m, a),
                                           preview=a.validate_only)
                status = data.get("status")
                if code != 200 or status in ("INVALID", None):
                    print(f"   {m}: ! {code} {status}: {issues_text(data.get('issues')) or data.get('errors') or data}")
                    totals["rejected"] += 1
                    continue
                warn = issues_text(data.get("issues"))
                print(f"   {m}: {status}  {asin}  {amz_title[:50]}" + (f"  ({warn})" if warn else ""))
                totals["ok"] += 1
                if not a.validate_only:
                    state[f"{m}:{p['sku']}"] = {"asin": asin, "status": status,
                                                "submissionId": data.get("submissionId"),
                                                "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    save_state(state)
            except Exception as e:  # one bad offer shouldn't stop the run
                print(f"   {m}: ! failed: {e}")
                totals["rejected"] += 1
    print(f"Done: {totals['ok']} {'valid' if a.validate_only else 'submitted'}, "
          f"{totals['not_in_catalog']} not in catalog, {totals['rejected']} rejected/failed.")
    if not a.validate_only and totals["ok"]:
        print("Amazon processes offers asynchronously - run 'status' in a few minutes.")


def cmd_status(api, a):
    markets = parse_markets(a.markets) if a.markets else None  # default: everything recorded
    state = load_state()
    keys = [k for k in state if markets is None or k.split(":", 1)[0] in markets]
    if not keys:
        print(f"Nothing in {STATE_FILE}" + (f" for {', '.join(markets)}." if markets else "."))
        return
    for key in keys:
        m, sku = key.split(":", 1)
        info = state[key]
        code, data = api.get_listing(sku, MARKETS[m][0])
        if code != 200:
            print(f"{m}:{sku}: error {code} {data.get('errors') or data}")
            continue
        summary = next((s for s in data.get("summaries", []) if s.get("marketplaceId") == MARKETS[m][0]), {})
        info["listing_status"] = summary.get("status", [])
        info["issues"] = issues_text(data.get("issues"))
        live = "LIVE" if "BUYABLE" in info["listing_status"] else "not buyable yet"
        print(f"{m}:{sku}: {live} {info['listing_status']}  asin={info.get('asin')}"
              + (f"\n     issues: {info['issues']}" if info["issues"] else ""))
    save_state(state)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--markets", help=f"comma list of {','.join(MARKETS)} or 'all' (= all EU). "
                                          f"push default: {','.join(DEFAULT_MARKETS)}")
    common.add_argument("--sandbox", action="store_true", help="use the SP-API sandbox (canned responses only)")
    ap = argparse.ArgumentParser(description="Push offers to European Amazon marketplaces via SP-API")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("push", parents=[common], help="create offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'quantity' wins)")
    p.add_argument("--handling-days", type=int, help="max days until you ship (default: your account setting)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--validate-only", action="store_true", help="Amazon validates the offers, nothing is listed")
    p.add_argument("--limit", type=int, default=0, help="max products this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    sub.add_parser("status", parents=[common], help="check pushed offers (default: all recorded markets)")

    a = ap.parse_args()
    api = SPAPI(a.sandbox)
    {"push": cmd_push, "status": cmd_status}[a.cmd](api, a)


if __name__ == "__main__":
    main()
