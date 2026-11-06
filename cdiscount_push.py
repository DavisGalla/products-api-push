#!/usr/bin/env python3
"""
Push products to Cdiscount (France) through the official Octopia Seller API
(https://developer.octopia-io.net, gateway https://api.octopia-io.net/seller/v2).
Octopia runs Cdiscount's marketplace; the same API serves its other sales
channels (SalesChannelId CDISFR = Cdiscount).

Each product is matched to an EXISTING Cdiscount catalog product by its
ISBN/EAN (GET /products?gtin=...), and your offer (price incl. VAT, stock,
condition, delivery modes, preparation time) is sent in an offer package of
type "Upsert": create the package, upload the offers (100 per call), submit
it, wait until Octopia has integrated it, then read the result of every
offer. Upsert creates new offers and replaces existing ones with the same
reference, so re-sending is always safe. New and used books can be sold.

ONE-TIME SETUP (see docs/cdiscount_push.md)
  developer.octopia-io.net -> API Credentials Management -> create your
  clientId and clientSecret (and note your sellerId). Then (PowerShell), or
  as Bitwarden secrets:
      $env:OCTOPIA_CLIENT_ID = "..."
      $env:OCTOPIA_CLIENT_SECRET = "..."
      $env:OCTOPIA_SELLER_ID = "..."

USAGE
    pip install requests
    python cdiscount_push.py check                    # credentials, shop
    python cdiscount_push.py lookup 9788409591923     # does Cdiscount know this ISBN?
    python cdiscount_push.py push --dry-run           # build the offers, send nothing
    python cdiscount_push.py push --limit 1           # one real offer
    python cdiscount_push.py push                     # everything
    python cdiscount_push.py push --resend --zero-sold-out   # sync price/stock, stock 0 for sold-out
    python cdiscount_push.py status                   # active / online on Cdiscount?
    python cdiscount_push.py package <packageId>      # results of an earlier package

NOTE: new books in France have a legally fixed price (loi Lang: at most 5%
below the publisher's price). Prices include French VAT (books: 5.5%).
"""
import argparse
import json
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

SALES_CHANNEL_ID = "CDISFR"     # Cdiscount France

# Source condition -> Octopia offer condition. Products whose condition isn't here are skipped.
CONDITION_MAP = {
    "NewCondition": "New",
    "UsedCondition": "UsedVeryGoodState",
    "RefurbishedCondition": "UsedLikeNew",
    "DamagedCondition": "UsedAverageState",
}

VAT_RATE = 5.5                  # French VAT on books, in %
# Delivery modes offered with every book: code, cost and cost per additional copy (EUR).
# THD = tracked home delivery, NTHD = untracked (Cdiscount only). See docs/cdiscount_push.md.
DELIVERY_MODES = [{"code": "THD", "cost": 4.99, "additionalCost": 1.00}]
PREPARATION_DAYS = 2            # days before you hand the parcel to the carrier
# ======================================================================

API_URL = "https://api.octopia-io.net/seller/v2"
AUTH_URL = "https://auth.octopia-io.net/auth/realms/maas/protocol/openid-connect/token"
STATE_FILE = Path("cdiscount_posted.json")
PREVIEW_FILE = Path("cdiscount_offers.json")
UPLOAD_BATCH = 100              # Octopia: max 100 offer requests per upload call


# --------------------------------------------------------------------------- API

class Octopia:
    def __init__(self):
        self.client = os.environ.get("OCTOPIA_CLIENT_ID")
        self.secret = os.environ.get("OCTOPIA_CLIENT_SECRET")
        self.seller = os.environ.get("OCTOPIA_SELLER_ID")
        missing = [k for k, v in (("OCTOPIA_CLIENT_ID", self.client), ("OCTOPIA_CLIENT_SECRET", self.secret),
                                  ("OCTOPIA_SELLER_ID", self.seller)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.base = (os.environ.get("OCTOPIA_API_URL") or API_URL).rstrip("/")
        self.auth = os.environ.get("OCTOPIA_AUTH_URL") or AUTH_URL
        self.token, self.token_until = None, 0

    def access_token(self, force=False):
        """Client credentials token (valid 2 hours, max 500 per hour): reused for the whole run."""
        if self.token and not force and time.time() < self.token_until - 60:
            return self.token
        r = requests.post(self.auth, data={"client_id": self.client, "client_secret": self.secret,
                                           "grant_type": "client_credentials"}, timeout=60)
        if r.status_code in (400, 401):
            sys.exit(f"Octopia login failed ({r.status_code}): check OCTOPIA_CLIENT_ID / OCTOPIA_CLIENT_SECRET "
                     "(developer.octopia-io.net -> API Credentials Management).")
        r.raise_for_status()
        data = r.json()
        self.token, self.token_until = data["access_token"], time.time() + int(data.get("expires_in", 7200))
        return self.token

    def call(self, method, path, params=None, body=None, channel=False, retries=5):
        url = path if path.startswith("http") else f"{self.base}{path}"
        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {self.access_token()}", "SellerId": str(self.seller),
                       "Accept": "application/json", "Accept-Language": "en-US"}
            if channel:
                headers["SalesChannelId"] = SALES_CHANNEL_ID
            if body is not None:
                headers["Content-Type"] = "application/json"
            r = requests.request(method, url, params=params, headers=headers, timeout=120,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
            if r.status_code == 401 and attempt == 0:
                self.access_token(force=True)
                continue
            if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
                wait = int(r.headers.get("Retry-After") or 2 ** attempt)
                print(f"   (API returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                data = r.json() if r.content else {}
            except ValueError:
                data = {"detail": r.text[:300]}
            if r.status_code == 403:
                sys.exit(f"403 Forbidden: {problem_text(data)} (is OCTOPIA_SELLER_ID right, and the clientId linked to it?)")
            return r, data
        return r, {"detail": "gave up after retries"}

    def seller_info(self):
        r, data = self.call("GET", "/sellers")
        if r.status_code != 200:
            raise RuntimeError(f"seller lookup failed ({r.status_code}): {problem_text(data)}")
        return data

    def known_gtins(self, gtins):
        known = set()
        for i in range(0, len(gtins), 10):                      # max 10 GTINs per call
            r, data = self.call("GET", "/products", params={"gtin": ",".join(gtins[i:i + 10])})
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                raise RuntimeError(f"product lookup failed ({r.status_code}): {problem_text(data)}")
            known |= {str(p.get("gtin")) for p in data.get("items") or []}
        return known

    def create_package(self, package_type="Upsert"):
        r, data = self.call("POST", "/offer-packages", body={"packageType": package_type}, channel=True)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"package creation failed ({r.status_code}): {problem_text(data)}")
        location = r.headers.get("Content-Location") or data.get("packageId") or ""
        package_id = str(location).rstrip("/").rsplit("/", 1)[-1]
        if not package_id:
            raise RuntimeError("package created but no packageId returned")
        return package_id

    def upload(self, package_id, requests_):
        for i in range(0, len(requests_), UPLOAD_BATCH):
            r, data = self.call("POST", f"/offer-packages/{package_id}/offer-requests", body=requests_[i:i + UPLOAD_BATCH])
            if r.status_code not in (200, 201, 202, 204):
                raise RuntimeError(f"upload to package {package_id} failed ({r.status_code}): {problem_text(data)}")

    def submit(self, package_id):
        r, data = self.call("PATCH", f"/offer-packages/{package_id}", body={"state": "Ready"})
        if r.status_code not in (200, 202, 204):
            raise RuntimeError(f"submitting package {package_id} failed ({r.status_code}): {problem_text(data)}")

    def package(self, package_id):
        r, data = self.call("GET", f"/offer-packages/{package_id}")
        if r.status_code != 200:
            raise RuntimeError(f"package lookup failed ({r.status_code}): {problem_text(data)}")
        return data

    def results(self, package_id):
        out, url, params = [], f"/offer-packages/{package_id}/offer-requests-results", {"limit": 100}
        while url:
            r, data = self.call("GET", url, params=params)
            if r.status_code != 200:
                raise RuntimeError(f"results lookup failed ({r.status_code}): {problem_text(data)}")
            out += data.get("items") or []
            m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', r.headers.get("Link") or "")
            url, params = (m.group(1), None) if m else (None, None)
            if url and not url.startswith("http"):
                url = url if url.startswith("/") else "/" + url
                url = url.replace("/seller/v2", "", 1) if url.startswith("/seller/v2") else url
        return out

    def offers(self, refs):
        out = []
        for i in range(0, len(refs), 50):
            r, data = self.call("GET", "/offers", params={"salesChannelId": SALES_CHANNEL_ID, "limit": 1000,
                                                          "sellerExternalReferences": ",".join(refs[i:i + 50]),
                                                          "expand": "salesChannelFeedback"})
            if r.status_code != 200:
                raise RuntimeError(f"offer lookup failed ({r.status_code}): {problem_text(data)}")
            out += data.get("items") or []
        return out


def problem_text(p):
    if not isinstance(p, dict):
        return str(p)[:300]
    msg = p.get("detail") or p.get("title") or p.get("error_description") or "error"
    errs = p.get("errors") or {}
    if isinstance(errs, dict):
        detail = "; ".join(f"{k}: {', '.join(map(str, v if isinstance(v, list) else [v]))}" for k, v in errs.items())
    else:
        detail = "; ".join(map(str, errs))
    return f"{msg} ({detail})" if detail else msg


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


def money(price, factor=1.0):
    """price * factor rounded half-up to cents (8.075 -> 8.08), as a float for the JSON body."""
    return float((Decimal(str(price)) * Decimal(str(factor))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize(raw):
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
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
        probs.append("no ISBN/EAN (needed to match the Cdiscount catalog)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in CONDITION_MAP:
        probs.append(f"no Octopia condition for '{p['condition']}' (set CONDITION_MAP)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def offer_request(p, a, quantity=None):
    return {
        "sellerExternalReference": p["sku"],
        "product": {"gtin": p["ean"]},
        "condition": CONDITION_MAP[p["condition"]],
        "price": {"price": money(p["price"], a.price_factor), "taxes": [{"code": "VAT", "value": VAT_RATE}]},
        "deliveryModes": [dict(m) for m in DELIVERY_MODES],
        "preparationTime": a.preparation_days,
        "quantity": int(quantity if quantity is not None else (p["quantity"] or a.quantity)),
    }


def wait_for(api, package_id, a):
    deadline = time.time() + a.wait
    while True:
        pkg = api.package(package_id)
        state = pkg.get("state")
        if state in ("Integrated", "Rejected"):
            return pkg
        if time.time() > deadline:
            return pkg
        time.sleep(a.poll)


def apply_results(api, package_id, sent, state):
    """Record integrated offers, print rejected ones. sent: {reference: (product, request)}"""
    ok = bad = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for item in api.results(package_id):
        ref = item.get("sellerExternalReference")
        status = item.get("integrationStatus")
        msgs = "; ".join(f"{x.get('resultCode')} {x.get('message')}" for x in item.get("results") or [])
        if ref not in sent:
            continue
        p, req = sent[ref]
        if status == "Integrated":
            state[ref] = {"ean": p["ean"], "price": req["price"]["price"], "quantity": req["quantity"],
                          "condition": req["condition"], "package_id": package_id, "pushed_at": now}
            ok += 1
            print(f"   + {ref}: {req['price']['price']:.2f} EUR x{req['quantity']}  ({msgs or 'integrated'})")
        else:
            bad += 1
            print(f"   ! {ref}: {status} - {msgs}")
    save_state(state)
    return ok, bad


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    s = api.seller_info()
    print(f"Connected: shop '{s.get('shop_name')}' (seller {s.get('seller_id')}, state {s.get('seller_state')}"
          + (f", {s.get('seller_sub_state')}" if s.get("seller_sub_state") else "") + ")")
    if s.get("seller_state") not in (None, "Activated"):
        print("   ! the seller account is not activated - offers won't go online")


def cmd_lookup(api, a):
    ean = to_ean(a.isbn)
    if not ean:
        sys.exit(f"Not a valid ISBN/EAN: {a.isbn}")
    print(f"{ean}: " + ("in the Cdiscount catalog" if ean in api.known_gtins([ean]) else "NOT in the Cdiscount catalog"))


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}  (sales channel {SALES_CHANNEL_ID})")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()

    new, resend, zero = [], [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and a.zero_sold_out and p["sku"] in state and state[p["sku"]].get("quantity", 1) > 0 \
                and p["ean"] and p["condition"] in CONDITION_MAP:
            zero.append(p)
            continue
        probs = problems_of(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if p["sku"] in state:
            if a.resend:
                resend.append(p)
            else:
                print(f"[{i}] already pushed: {p['title']}")
            continue
        new.append(p)
    if a.limit:
        new = new[: a.limit]

    if api and new and not a.no_match_check:
        print(f"Checking {len(new)} ISBN(s) against the Cdiscount catalog...")
        known = api.known_gtins([p["ean"] for p in new])
        for p in [p for p in new if p["ean"] not in known]:
            print(f"   SKIP '{p['title']}': EAN {p['ean']} is not in the Cdiscount catalog")
        new = [p for p in new if p["ean"] in known]
    elif not api and new:
        print("(dry run without credentials: catalog check skipped)")

    sent = {p["sku"]: (p, offer_request(p, a)) for p in new}
    for p in resend:
        req, info = offer_request(p, a), state[p["sku"]]
        if req["price"]["price"] != info.get("price") or req["quantity"] != info.get("quantity"):
            sent[p["sku"]] = (p, req)
        else:
            print(f"   = {p['sku']}: unchanged")
    for p in zero:
        sent[p["sku"]] = (p, offer_request(p, a, quantity=0))
    print(f"{len(new)} new offer(s), {len(sent) - len(new) - len(zero)} update(s)"
          + (f", {len(zero)} sold-out to set to stock 0" if zero else "") + ".")
    if not sent:
        return
    PREVIEW_FILE.write_text(json.dumps([req for _, req in sent.values()], ensure_ascii=False, indent=2), encoding="utf-8")
    if a.dry_run:
        print(f"Dry run: offers written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will send {len(sent)} LIVE offer(s) to Cdiscount. Type 'yes': ").strip().lower() != "yes":
            return

    package_id = api.create_package("Upsert")
    api.upload(package_id, [req for _, req in sent.values()])
    api.submit(package_id)
    print(f"Submitted offer package {package_id} ({len(sent)} offer(s)), waiting for Octopia to integrate it...")
    pkg = wait_for(api, package_id, a)
    st = pkg.get("state")
    if st == "Rejected":
        res = pkg.get("result") or {}
        print(f"Package {package_id} REJECTED: {res.get('resultCode', '')} {res.get('message', '')}. Nothing recorded; run push again.")
        return
    if st != "Integrated":
        print(f"Package {package_id} is still {st} after {a.wait}s. Nothing recorded yet; later run:\n"
              f"    python cdiscount_push.py package {package_id}\n(or simply run push again - Upsert is safe to repeat)")
        return
    ok, bad = apply_results(api, package_id, sent, state)
    print(f"Done: {ok} offer(s) integrated, {bad} rejected. Run `status` to see whether they are online.")


def cmd_package(api, a):
    pkg = api.package(a.package_id)
    print(f"Package {a.package_id}: {pkg.get('state')}  type {pkg.get('type')}  "
          f"{pkg.get('offerRequestCount', pkg.get('offerRequestsCount', '?'))} offer request(s)")
    if pkg.get("state") == "Rejected":
        res = pkg.get("result") or {}
        print(f"   reason: {res.get('resultCode', '')} {res.get('message', '')}")
        return
    if pkg.get("state") != "Integrated":
        return
    for item in api.results(a.package_id):
        msgs = "; ".join(f"{x.get('resultCode')} {x.get('message')}" for x in item.get("results") or [])
        print(f"   {item.get('sellerExternalReference')}: {item.get('integrationStatus')}  {msgs}")


def cmd_status(api, a):
    state = load_state()
    if not state:
        print("No offers yet.")
        return
    found = {o.get("sellerExternalReference"): o for o in api.offers(sorted(state))}
    for sku in sorted(state):
        o = found.get(sku)
        if not o:
            print(f"{sku}: not found on {SALES_CHANNEL_ID} (deleted, or not integrated yet)")
            continue
        fb = o.get("salesChannelFeedback") or {}
        qty = sum(int(d.get("quantity") or 0) for d in o.get("deliveryOffers") or [])
        online = fb.get("isOnline")
        best = (fb.get("bestOfferDetails") or {}).get("isBestOffer")
        print(f"{sku}: {o.get('offerState', '?').upper()}"
              + ("" if online is None else ("  ONLINE" if online else "  not online"))
              + f"  price={(o.get('integrationPrice') or {}).get('price')}  stock={qty}"
              + ("  (best offer)" if best else ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push offers to Cdiscount (Octopia Seller API)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the credentials and show the shop")
    s = sub.add_parser("lookup", help="check whether Cdiscount knows an ISBN/EAN")
    s.add_argument("isbn")
    p = sub.add_parser("push", help="send offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--preparation-days", type=int, default=PREPARATION_DAYS, help="days before shipping")
    p.add_argument("--resend", action="store_true", help="re-send already pushed offers whose price/stock changed")
    p.add_argument("--zero-sold-out", action="store_true", help="set stock 0 for pushed offers now sold out")
    p.add_argument("--no-match-check", action="store_true", help="skip the catalog lookup")
    p.add_argument("--dry-run", action="store_true", help="write cdiscount_offers.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--wait", type=int, default=900, help="max seconds to wait for the package integration")
    p.add_argument("--poll", type=int, default=15, help="seconds between package status checks")
    s = sub.add_parser("package", help="show the state and results of an offer package")
    s.add_argument("package_id")
    sub.add_parser("status", help="list your offers: active / online, price, stock")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run and not all(os.environ.get(k) for k in
                                                 ("OCTOPIA_CLIENT_ID", "OCTOPIA_CLIENT_SECRET", "OCTOPIA_SELLER_ID")):
        return cmd_push(None, a)                 # no credentials needed to build the preview
    api = Octopia()
    try:
        {"check": cmd_check, "lookup": cmd_lookup, "push": cmd_push, "package": cmd_package,
         "status": cmd_status}[a.cmd](api, a)
    except RuntimeError as e:
        sys.exit(f"Octopia API error: {e}")


if __name__ == "__main__":
    main()
