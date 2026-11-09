#!/usr/bin/env python3
"""
Push books to OnBuy (UK marketplace, now on 21 country sites) through the
official OnBuy / OnCommerce API v2 (docs: https://docs.api.onbuy.com).

How it works:
  * Each book is matched to an EXISTING OnBuy product by its ISBN/EAN
    (GET /v2/products, filter[field]=product_code, 50 barcodes per call),
    which gives the OnBuy product code (OPC).
  * Your listing (SKU, condition new, price, stock, handling time) is
    attached to that product with POST /v2/listings (batch). OnBuy answers
    per row: created / updated / failed with a message.
  * Price and stock changes go through PUT /v2/listings/by-sku (OnBuy's
    required way, batches up to 1000); sold-out books can get stock 0 or be
    deleted (DELETE /v2/listings/by-sku).
  * Books OnBuy doesn't know are skipped: creating new products needs a
    category, brand and OnBuy's product queue (not built).

Several country sites can be served in one run (--sites gb,es,de or 'eur' /
'all'); each site is listed separately with its own currency.

ONE-TIME SETUP (see docs/onbuy_push.md)
  OnBuy Seller Control Panel -> Integrations -> OnBuy API: consumer key and
  secret key. Then (PowerShell), or as Bitwarden secrets:
      $env:ONBUY_CONSUMER_KEY = "..."
      $env:ONBUY_SECRET_KEY = "..."
  The access token lasts 15 minutes and only works from the IP it was made on;
  the script renews it automatically.

USAGE
    python onbuy_push.py check                    # login, sites, delivery templates, settings
    python onbuy_push.py lookup 9788409591923     # does OnBuy know this ISBN? (OPC)
    python onbuy_push.py push --dry-run           # write onbuy_listings.json, send nothing
    python onbuy_push.py push --limit 1           # one real listing
    python onbuy_push.py push --resend            # update changed prices / stock
    python onbuy_push.py push --sites gb,es,de    # several country sites
    python onbuy_push.py status                   # listings, price, stock, buy box (winning)
"""
import argparse
import json
import os
import sys
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

SITES = ["gb"]                # default country sites (see SITE_TABLE); override with --sites
# Source prices are EUR. Rates for the non-euro sites; update them regularly.
EUR_RATE = {"EUR": "1", "GBP": "0.85", "CZK": "25.0", "DKK": "7.46", "HUF": "395", "NOK": "11.7",
            "PLN": "4.25", "RON": "5.09", "SEK": "11.0", "CHF": "0.94"}
HANDLING_TIME_DAYS = 3        # days to dispatch
DELIVERY_TEMPLATE_ID = None   # your OnBuy delivery template (see `check`); None = account default
FREE_RETURNS = None           # None = account default, or True / False
# GPSR responsible person (EU sites). Sent with new listings when the name is filled in.
RESPONSIBLE_PERSON = {"name": "", "postal_address": "", "country_code": "", "phone_number": "",
                      "electronic_contact_method": ""}
ALLOWED_CONDITIONS = {"NewCondition"}   # OnBuy book listings: condition "new"
DEFAULT_STOCK = 1             # stock when the product has no "stock"/"quantity" field
# ======================================================================

API_URL = "https://api.onbuy.com/v2"
# site code -> (site_id, currency), from the OnCommerce docs ("Site ID" / "Currency")
SITE_TABLE = {
    "gb": (2000, "GBP"), "fr": (2001, "EUR"), "de": (2002, "EUR"), "cz": (2003, "CZK"), "nl": (2004, "EUR"),
    "at": (2007, "EUR"), "be": (2008, "EUR"), "dk": (2011, "DKK"), "es": (2013, "EUR"), "fi": (2014, "EUR"),
    "gr": (2015, "EUR"), "hu": (2017, "HUF"), "ie": (2019, "EUR"), "it": (2020, "EUR"), "no": (2026, "NOK"),
    "pl": (2027, "PLN"), "pt": (2028, "EUR"), "ro": (2029, "RON"), "se": (2030, "SEK"), "ch": (2031, "CHF"),
    "sk": (2033, "EUR"),
}
SEARCH_BATCH = 50             # GTINs per product search
LISTING_BATCH = 500           # listings per POST /listings
UPDATE_BATCH = 1000           # SKUs per PUT / DELETE by-sku (OnBuy's limit)
STATE_FILE = Path("onbuy_posted.json")
PREVIEW_FILE = Path("onbuy_listings.json")


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------- API

class OnBuy:
    def __init__(self):
        ck, sk = os.environ.get("ONBUY_CONSUMER_KEY"), os.environ.get("ONBUY_SECRET_KEY")
        missing = [k for k, v in (("ONBUY_CONSUMER_KEY", ck), ("ONBUY_SECRET_KEY", sk)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.ck, self.sk = ck, sk
        self.base = (os.environ.get("ONBUY_API_URL") or API_URL).rstrip("/")
        self.token, self.expires = None, 0

    def login(self):
        r = requests.post(f"{self.base}/auth/request-token", data={"secret_key": self.sk, "consumer_key": self.ck},
                          timeout=60)
        if r.status_code != 200:
            sys.exit(f"OnBuy refused the login ({r.status_code}). Check ONBUY_CONSUMER_KEY / ONBUY_SECRET_KEY "
                     f"(Seller Control Panel -> Integrations -> OnBuy API): {r.text[:200]}")
        data = r.json()
        self.token = data["access_token"]
        exp = data.get("expires_at")
        self.expires = exp if isinstance(exp, (int, float)) and exp > time.time() else time.time() + 15 * 60

    def call(self, method, path, params=None, body=None, retries=4):
        """Returns (status, JSON). Renews the 15-minute token before it runs out, and once on 401."""
        relogged = False
        for attempt in range(retries):
            if not self.token or time.time() > self.expires - 60:
                self.login()
            r = requests.request(method, f"{self.base}/{path.lstrip('/')}", params=params,
                                 data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Authorization": self.token, "Content-Type": "application/json",
                                          "Accept": "application/json"}, timeout=120)
            if r.status_code == 401 and not relogged:
                relogged, self.token = True, None       # expired, or the IP changed
                continue
            if r.status_code == 401:
                sys.exit("OnBuy refused the token (401). Tokens only work from the IP they were made on.")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries - 1:
                    break
                pause = 2 ** attempt * 5
                print(f"   (OnBuy returned {r.status_code}, retrying in {pause}s)")
                time.sleep(pause)
                continue
            try:
                return r.status_code, (r.json() if r.content else {})
            except ValueError:
                raise ApiError(f"{path}: HTTP {r.status_code}, not JSON: {r.text[:200]}")
        raise ApiError(f"{path}: gave up after {retries} tries (HTTP {r.status_code}); "
                       f"OnBuy limits are per site: POST/PUT/DELETE 240 an hour, GET 600 an hour")

    def pages(self, path, params, limit=100):
        offset = 0
        while True:
            code, data = self.call("GET", path, params=dict(params, limit=limit, offset=offset))
            if code != 200:
                raise ApiError(f"{path}: {code} {err_text(data)}")
            rows = data.get("results") or []
            yield from rows
            total = (data.get("metadata") or {}).get("total_rows")
            offset += limit
            if not rows or (total is not None and offset >= int(total)):
                return

    def find_opcs(self, site_id, gtins):
        """{gtin: product} for the GTINs OnBuy knows on this site."""
        found = {}
        for i in range(0, len(gtins), SEARCH_BATCH):
            part = gtins[i:i + SEARCH_BATCH]
            code, data = self.call("GET", "products", params={"site_id": site_id, "filter[field]": "product_code",
                                                              "filter[query]": ",".join(part), "limit": 100})
            if code != 200:
                raise ApiError(f"product search: {code} {err_text(data)}")
            for prod in data.get("results") or []:
                for c in prod.get("product_codes") or []:
                    if str(c) in part:
                        found.setdefault(str(c), prod)
        return found


def err_text(data):
    if not isinstance(data, dict):
        return str(data)[:300]
    err = data.get("error") or data.get("errors") or data.get("message")
    if isinstance(err, dict):
        return "; ".join(str(err.get(k)) for k in ("message", "errorCode", "detail") if err.get(k)) or json.dumps(err)[:300]
    if isinstance(err, list):
        return "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in err)
    return str(err or json.dumps(data)[:300])


# ----------------------------------------------------------------------- helpers

def load_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def to_gtin(code):
    """Normalise an ISBN-10/13 or EAN to a valid 13-digit GTIN, or return None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    digit = lambda core: str((10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10)
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        return "978" + c[:9] + digit("978" + c[:9])
    if len(c) == 13 and c.isdigit() and digit(c[:12]) == c[12]:
        return c
    return None


def site_price(price_eur, currency, factor):
    try:
        v = Decimal(str(price_eur)) * Decimal(str(factor)) * Decimal(EUR_RATE[currency])
    except (InvalidOperation, KeyError):
        return None
    v = v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(v) if v > 0 else None


def parse_sites(text):
    v = (text or "").strip().lower()
    if v == "all":
        codes = list(SITE_TABLE)
    elif v == "eur":
        codes = [c for c, (_, cur) in SITE_TABLE.items() if cur == "EUR"]
    elif v:
        codes = [c.strip().lower() for c in v.split(",") if c.strip()]
    else:
        codes = [c.lower() for c in SITES]
    bad = [c for c in codes if c not in SITE_TABLE]
    if bad or not codes:
        sys.exit(f"Unknown OnBuy site(s): {', '.join(bad) or '(none)'}. Known: {', '.join(SITE_TABLE)}, or 'eur' / 'all'.")
    return list(dict.fromkeys(codes))


def normalize(raw, a):
    stock = raw.get("stock", raw.get("quantity"))
    try:
        qty = int(stock) if stock not in (None, "") else a.quantity
    except (TypeError, ValueError):
        qty = -1
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": (raw.get("title") or "").strip(),
        "price": raw.get("price_eur", raw.get("price")),
        "gtin": to_gtin(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "stock": qty,
        "sold_out": bool(raw.get("sold_out", False)),
    }


def problems_of(p, a):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    elif len(p["sku"]) > 100:
        probs.append("id/sku too long")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (OnBuy matches listings to products by barcode)")
    elif not p["gtin"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if site_price(p["price"], "EUR", a.price_factor) is None:
        probs.append("no valid price")
    if p["condition"] not in ALLOWED_CONDITIONS:
        probs.append(f"condition '{p['condition']}' not offered (see ALLOWED_CONDITIONS)")
    if p["stock"] < 0:
        probs.append("stock must be a whole number >= 0")
    return probs


def responsible_person():
    rp = RESPONSIBLE_PERSON or {}
    return {k: v for k, v in rp.items() if v} if rp.get("name") else None


def new_listing(p, opc, price):
    row = {"opc": opc, "sku": p["sku"], "condition": "new", "price": price, "stock": p["stock"],
           "handling_time": HANDLING_TIME_DAYS}
    if DELIVERY_TEMPLATE_ID:
        row["delivery_template_id"] = DELIVERY_TEMPLATE_ID
    if FREE_RETURNS is not None:
        row["free_returns"] = bool(FREE_RETURNS)
    rp = responsible_person()
    if rp:
        row["responsible_person"] = rp
    return row


def put_by_sku(api, site_id, rows, label):
    """PUT /listings/by-sku in batches. Returns set of SKUs OnBuy accepted."""
    ok = set()
    for i in range(0, len(rows), UPDATE_BATCH):
        part = rows[i:i + UPDATE_BATCH]
        code, data = api.call("PUT", "listings/by-sku", body={"site_id": site_id, "listings": part})
        if code != 200:
            print(f"   ! {label}: batch of {len(part)} refused ({code}): {err_text(data)}")
            continue
        for res in data.get("results") or []:
            if res.get("error"):
                print(f"   ! {res.get('sku')}: {label} rejected: {res['error']}")
            else:
                ok.add(str(res.get("sku")))
    return ok


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    sites = parse_sites(a.sites)
    print("Sites: " + ", ".join(f"{c} ({SITE_TABLE[c][0]}, {SITE_TABLE[c][1]})" for c in sites))
    for c in sites:
        cur = SITE_TABLE[c][1]
        if cur != "EUR":
            print(f"   {c}: 1 EUR = {EUR_RATE.get(cur)} {cur}")
    api.login()
    print(f"Login OK ({api.base}); token valid until {time.strftime('%H:%M', time.localtime(api.expires))}")
    for c in sites:
        sid = SITE_TABLE[c][0]
        code, data = api.call("GET", "sellers/deliveries", params={"site_id": sid, "limit": 100})
        if code == 200:
            rows = data.get("results") or []
            print(f"   {c}: {len(rows)} delivery template(s)")
            for d in rows[:10]:
                print(f"      {d.get('delivery_template_id') or d.get('id')}: {d.get('name') or d.get('template_name') or ''}")
        else:
            print(f"   {c}: delivery templates not readable ({code}: {err_text(data)})")
    if not responsible_person():
        print("! RESPONSIBLE_PERSON (GPSR) is not filled in; EU sites may require it.")


def cmd_lookup(api, a):
    gtin = to_gtin(a.isbn)
    if not gtin:
        sys.exit(f"Not a valid ISBN/EAN: {a.isbn}")
    for c in parse_sites(a.sites):
        hit = api.find_opcs(SITE_TABLE[c][0], [gtin]).get(gtin)
        print(f"{c}: " + (f"OPC {hit.get('opc')}  {hit.get('name')}  {hit.get('url') or hit.get('product_url') or ''}"
                          if hit else "not in the OnBuy catalog"))


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    sites = parse_sites(a.sites)
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_json(STATE_FILE)

    books, seen_sku, seen_gtin = [], set(), set()
    for i, r in enumerate(raw, 1):
        p = normalize(r, a)
        probs = problems_of(p, a)
        if p["sku"] in seen_sku:
            probs.append(f"id/sku {p['sku']} appears twice")
        if p["gtin"] and p["gtin"] in seen_gtin:
            probs.append(f"ISBN {p['gtin']} is already used by another book")
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        seen_sku.add(p["sku"])
        seen_gtin.add(p["gtin"])
        books.append(p)

    plan, preview = [], []
    for c in sites:
        sid, cur = SITE_TABLE[c]
        new = [p for p in books if f"{c}:{p['sku']}" not in state and not p["sold_out"]]
        if a.limit:
            new = new[:a.limit]
        updates, deletes = [], []
        for p in books:
            e = state.get(f"{c}:{p['sku']}")
            if not e:
                continue
            price = site_price(p["price"], cur, a.price_factor)
            if p["sold_out"]:
                if a.delete_sold_out:
                    deletes.append(p["sku"])
                elif a.zero_sold_out and e.get("stock") != 0:
                    updates.append({"sku": p["sku"], "stock": 0})
            elif a.resend and (e.get("price") != price or e.get("stock") != p["stock"]):
                updates.append({"sku": p["sku"], "price": price, "stock": p["stock"]})
        plan.append((c, sid, cur, new, updates, deletes))
        preview += [dict(new_listing(p, "<OPC>", site_price(p["price"], cur, a.price_factor)), site=c) for p in new]
        print(f"{c}: {len(new)} new book(s) to match, {len(updates)} update(s), {len(deletes)} delete(s)")
    save_json(PREVIEW_FILE, preview)
    if a.dry_run:
        print(f"Dry run: listings written to {PREVIEW_FILE} (OPC is looked up on a real push). Nothing sent.")
        return
    if not any(n or u or d for _, _, _, n, u, d in plan):
        print("Nothing to send.")
        return
    if not a.yes and input("Send these to OnBuy? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return

    for c, sid, cur, new, updates, deletes in plan:
        print(f"--- {c} (site {sid}, {cur}) ---")
        if new:
            hits = api.find_opcs(sid, [p["gtin"] for p in new])
            rows = []
            for p in new:
                prod = hits.get(p["gtin"])
                if not prod:
                    print(f"   SKIP '{p['title']}': ISBN {p['gtin']} is not in the OnBuy catalog")
                    continue
                rows.append((p, new_listing(p, prod["opc"], site_price(p["price"], cur, a.price_factor)), prod))
            created = 0
            for i in range(0, len(rows), LISTING_BATCH):
                part = rows[i:i + LISTING_BATCH]
                code, data = api.call("POST", "listings", body={"site_id": sid, "listings": [r for _, r, _ in part]})
                if code not in (200, 201):
                    print(f"   ! listing batch of {len(part)} refused ({code}): {err_text(data)}")
                    continue
                results = {str(x.get("sku")): x for x in data.get("results") or []}
                for p, row, prod in part:
                    res = results.get(p["sku"])
                    if not res or not res.get("success"):
                        print(f"   ! {p['sku']}: listing rejected: {(res or {}).get('message') or 'no result returned'}")
                        continue
                    created += 1
                    state[f"{c}:{p['sku']}"] = {"site_id": sid, "opc": row["opc"], "gtin": p["gtin"],
                                                "listing_id": res.get("product_listing_id"), "price": row["price"],
                                                "currency": cur, "stock": row["stock"], "url": prod.get("url"),
                                                "created_at": now_iso(), "updated_at": now_iso()}
                save_json(STATE_FILE, state)
            print(f"   {created} listing(s) created")
        if updates:
            ok = put_by_sku(api, sid, updates, "update")
            for u in updates:
                if u["sku"] in ok:
                    e = state[f"{c}:{u['sku']}"]
                    e.update({k: u[k] for k in ("price", "stock") if k in u}, updated_at=now_iso())
            save_json(STATE_FILE, state)
            print(f"   {len(ok)} listing(s) updated")
        for i in range(0, len(deletes), UPDATE_BATCH):
            part = deletes[i:i + UPDATE_BATCH]
            code, data = api.call("DELETE", "listings/by-sku", body={"site_id": sid, "skus": part})
            if code != 200:
                print(f"   ! delete batch refused ({code}): {err_text(data)}")
                continue
            results = data.get("results") or {}
            for sku in part:
                res = results.get(sku) if isinstance(results, dict) else None
                if isinstance(res, dict) and res.get("error") and "not found" not in str(res["error"]).lower():
                    print(f"   ! {sku}: delete failed: {res['error']}")
                    continue
                state.pop(f"{c}:{sku}", None)
                print(f"   - {sku}: deleted (sold out)")
            save_json(STATE_FILE, state)


def cmd_status(api, a):
    state = load_json(STATE_FILE)
    if not state:
        print("Nothing pushed yet.")
        return
    for c in parse_sites(a.sites):
        sid, cur = SITE_TABLE[c]
        mine = sorted(k.split(":", 1)[1] for k in state if k.startswith(c + ":"))
        if not mine:
            continue
        live = {}
        for row in api.pages("listings", {"site_id": sid}):
            if str(row.get("sku")) in mine:
                live[str(row.get("sku"))] = row
        winning = {}
        for i in range(0, len(mine), 1000):
            code, data = api.call("GET", "listings/check-winning", body={"site_id": sid, "skus": mine[i:i + 1000]})
            if code == 200:
                for w in data.get("results") or []:
                    winning[str(w.get("sku"))] = w
        print(f"--- {c} (site {sid}, {cur}): {len(live)} of {len(mine)} listing(s) found ---")
        for sku in mine:
            row, w = live.get(sku), winning.get(sku) or {}
            if not row:
                print(f"   {sku}: NOT FOUND on OnBuy")
                continue
            win = ("winning" if w.get("winning") else f"not winning (lead price {w.get('lead_price')})") if w else ""
            print(f"   {sku}: {row.get('price')} {cur}  stock {row.get('stock')}  {win}  {row.get('product_url') or ''}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to OnBuy (OnCommerce API v2)")
    ap.add_argument("--sites", help=f"country sites, e.g. gb,es,de, or 'eur' / 'all' (default {','.join(SITES)})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check", help="login, delivery templates, settings")
    lk = sub.add_parser("lookup", help="is this ISBN in the OnBuy catalog?")
    lk.add_argument("isbn")
    p = sub.add_parser("push", help="list new books, update changed ones")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--dry-run", action="store_true", help="write onbuy_listings.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new books per site this run (0 = all)")
    p.add_argument("--resend", action="store_true", help="update prices / stock that changed")
    p.add_argument("--zero-sold-out", action="store_true", help="stock 0 for pushed books that are now sold out")
    p.add_argument("--delete-sold-out", action="store_true", help="delete pushed books that are now sold out")
    p.add_argument("--quantity", type=int, default=DEFAULT_STOCK, help="stock when the book has none")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    st = sub.add_parser("status", help="listings, price, stock and buy box per site")
    for sp in (ck, lk, p, st):
        sp.add_argument("--sites", dest="sites_sub", help="country sites, e.g. gb,es,de, or 'eur' / 'all'")
    a = ap.parse_args()
    if getattr(a, "sites_sub", None):
        a.sites = a.sites_sub
    if a.cmd == "push" and a.dry_run:
        return cmd_push(None, a)
    api = OnBuy()
    try:
        {"check": cmd_check, "lookup": cmd_lookup, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except (ApiError, requests.RequestException) as e:
        sys.exit(f"OnBuy API error: {e}")


if __name__ == "__main__":
    main()
