#!/usr/bin/env python3
"""
Push products to bol.com (Netherlands and Belgium) through the official
bol.com Retailer API, using the new Offers API v11 (the v10 offer calls stop
working on 1 February 2027).

Each product is matched to an EXISTING bol.com catalog product by its
ISBN/EAN, and your offer (price, stock, condition, delivery) is created on it.
Books bol doesn't know are skipped. New AND used books can be sold.

ONE-TIME SETUP (see docs/bol_push.md)
  bol.com Seller Dashboard -> Instellingen -> API Instellingen: fill in the
  technical contact, then create "API Credentials for the Retailer API".
  You get a Client ID and a Client Secret. Then (PowerShell), or as
  Bitwarden secrets:
      $env:BOL_CLIENT_ID = "..."
      $env:BOL_CLIENT_SECRET = "..."
  Also set up your delivery promise in the Seller Dashboard, and an economic
  operator (GPSR) if you don't have one yet (see `check`).

USAGE
    pip install requests
    python bol_push.py check                    # test the keys, economic operators
    python bol_push.py lookup 9788409591923     # does bol know this ISBN?
    python bol_push.py push --dry-run           # build the offers, send nothing
    python bol_push.py push --limit 1           # one real offer
    python bol_push.py push                     # everything
    python bol_push.py push --resend --zero-sold-out   # sync price/stock, stock 0 for sold-out
    python bol_push.py status                   # for sale? if not, bol's reasons

NOTE: prices on bol include Dutch/Belgian VAT (books: 9% NL, 6% BE).
Dutch-language books have a fixed price in the Netherlands and Flanders.
"""
import argparse
import base64
import json
import os
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

COUNTRIES = ["NL", "BE"]                # where the offers are for sale

# Source condition -> bol condition. Products whose condition isn't here are skipped.
CONDITION_MAP = {
    "NewCondition": {"category": "NEW"},
    "UsedCondition": {"category": "SECONDHAND", "state": "GOOD"},
    "RefurbishedCondition": {"category": "SECONDHAND", "state": "AS_NEW"},
    "DamagedCondition": {"category": "SECONDHAND", "state": "MODERATE"},
}

# Delivery: "MY_DELIVERY_PROMISE" uses the delivery promise you set in the
# Seller Dashboard. Or pass --delivery-days MIN-MAX to state it per offer.
DELIVERY_SCHEDULE = "MY_DELIVERY_PROMISE"
DELIVERY_PROFILE_ID = ""                # optional delivery promise profile id
ECONOMIC_OPERATOR_ID = ""               # GPSR. "" = the first one in your account (see `check`)
# ======================================================================

API_URL = "https://api.bol.com"
LOGIN_URL = "https://login.bol.com"
OFFERS_V11 = "application/vnd.retailer.v11+json"
RETAILER_V10 = "application/vnd.retailer.v10+json"
EO_V1 = "application/vnd.economic-operator.v1+json"
STATE_FILE = Path("bol_posted.json")
PREVIEW_FILE = Path("bol_offers.json")


# --------------------------------------------------------------------------- API

class Bol:
    def __init__(self):
        self.client = os.environ.get("BOL_CLIENT_ID")
        self.secret = os.environ.get("BOL_CLIENT_SECRET")
        missing = [k for k, v in (("BOL_CLIENT_ID", self.client), ("BOL_CLIENT_SECRET", self.secret)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.base = (os.environ.get("BOL_API_URL") or API_URL).rstrip("/")
        self.login = (os.environ.get("BOL_LOGIN_URL") or LOGIN_URL).rstrip("/")
        self.token, self.token_until = None, 0

    def access_token(self, force=False):
        """Client credentials token (valid ~5 minutes); reused until it's about to expire."""
        if self.token and not force and time.time() < self.token_until - 20:
            return self.token
        basic = base64.b64encode(f"{self.client}:{self.secret}".encode()).decode()
        r = requests.post(f"{self.login}/token", data={"grant_type": "client_credentials"}, timeout=60,
                          headers={"Authorization": f"Basic {basic}", "Accept": "application/json",
                                   "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code in (400, 401):
            sys.exit(f"bol.com login failed ({r.status_code}): check BOL_CLIENT_ID / BOL_CLIENT_SECRET "
                     "(Seller Dashboard -> Instellingen -> API Instellingen).")
        r.raise_for_status()
        data = r.json()
        self.token, self.token_until = data["access_token"], time.time() + int(data.get("expires_in", 299))
        return self.token

    def call(self, method, path, media=OFFERS_V11, params=None, body=None, retries=5):
        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {self.access_token()}", "Accept": media}
            if body is not None:
                headers["Content-Type"] = media
            r = requests.request(method, f"{self.base}{path}", params=params, headers=headers, timeout=60,
                                 data=json.dumps(body).encode("utf-8") if body is not None else None)
            if r.status_code == 401 and attempt == 0:
                self.access_token(force=True)          # token expired early: get a new one once
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
                sys.exit(f"403 Forbidden: {problem_text(data)} (do your API credentials have the Offers scopes?)")
            return r.status_code, data
        return r.status_code, {"detail": "gave up after retries"}

    def known_product(self, ean):
        code, data = self.call("GET", f"/retailer/products/{ean}/product-ids", media=RETAILER_V10)
        if code == 200:
            return data.get("bolProductId") or True
        if code == 404:
            return None
        raise RuntimeError(f"catalog lookup failed ({code}): {problem_text(data)}")

    def economic_operators(self):
        code, data = self.call("GET", "/retailer/economic-operators", media=EO_V1, params={"page-size": 50})
        if code != 200:
            raise RuntimeError(f"economic operator list failed ({code}): {problem_text(data)}")
        return data.get("operators") or []

    def create_offer(self, body):
        return self.call("POST", "/retailer/offers", body=body)

    def find_offers(self, ean):
        code, data = self.call("GET", "/retailer/offers", params={"eans": ean, "page-size": 100})
        return data.get("offers") or [] if code == 200 else []

    def offer(self, offer_id):
        return self.call("GET", f"/retailer/offers/{offer_id}")

    def patch_offer(self, offer_id, body):
        return self.call("PATCH", f"/retailer/offers/{offer_id}", body=body)

    def not_for_sale_reasons(self, offer_id):
        code, data = self.call("GET", f"/retailer/offers/{offer_id}/not-for-sale-reasons")
        return data.get("countries") or [] if code == 200 else []


def problem_text(p):
    if not isinstance(p, dict):
        return str(p)[:300]
    msg = p.get("detail") or p.get("title") or "error"
    v = "; ".join(f"{x.get('name')}: {x.get('reason')}" for x in p.get("violations") or [])
    return f"{msg} ({v})" if v else msg


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
    """price * factor rounded half-up to cents, as a Decimal (8.075 -> 8.08)."""
    return (Decimal(str(price)) * Decimal(str(factor))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize(raw):
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:100],
        "title": raw.get("title") or "",
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "quantity": raw.get("quantity"),
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p, factor):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    elif not 1 <= money(p["price"], factor) <= 9999:
        probs.append(f"price {money(p['price'], factor)} outside bol's 1-9999 EUR")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (needed to match the bol catalog)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in CONDITION_MAP:
        probs.append(f"no bol condition for '{p['condition']}' (set CONDITION_MAP)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def fulfilment(a):
    if a.delivery_days:
        lo, hi = a.delivery_days
        return {"method": "FBR", "schedule": "BOL_DELIVERY_PROMISE",
                "deliveryPromise": {"minimumDaysToCustomer": lo, "maximumDaysToCustomer": hi}}
    f = {"method": "FBR", "schedule": DELIVERY_SCHEDULE}
    if DELIVERY_PROFILE_ID:
        f["profileId"] = DELIVERY_PROFILE_ID
    return f


def offer_body(p, a, operator_id, amount=None):
    body = {
        "ean": p["ean"],
        "reference": p["sku"],
        "onHoldByRetailer": False,
        "condition": dict(CONDITION_MAP[p["condition"]]),
        "pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": float(money(p["price"], a.price_factor))}]},
        "countryAvailabilities": [{"countryCode": c} for c in a.countries],
        "fulfilment": fulfilment(a),
        "stock": {"amount": int(amount if amount is not None else (p["quantity"] or a.quantity)), "managedByRetailer": True},
    }
    if operator_id:
        body["economicOperatorId"] = operator_id
    return body


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    api.access_token()
    print("Credentials accepted (token received).")
    ops = api.economic_operators()
    print("Economic operators (GPSR):" + ("" if ops else " none - add one in the Seller Dashboard, or offers "
                                                          "may go offline ('incomplete economic operator')"))
    for o in ops:
        print(f"   {o.get('id')}  {o.get('name')}" + (f"  ({o.get('status')})" if o.get("status") else ""))


def cmd_lookup(api, a):
    ean = to_ean(a.isbn)
    if not ean:
        sys.exit(f"Not a valid ISBN/EAN: {a.isbn}")
    pid = api.known_product(ean)
    print(f"{ean}: " + (f"in the bol catalog (bol product id {pid})" if pid else "NOT in the bol catalog"))


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}  (for sale in: {', '.join(a.countries)})")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()

    new, resend, zero = [], [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and a.zero_sold_out and p["sku"] in state and state[p["sku"]].get("stock", 1) > 0:
            zero.append(p)
            continue
        probs = problems_of(p, a.price_factor)
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

    operator = ECONOMIC_OPERATOR_ID or None
    if api and new:
        if not a.no_match_check:
            print(f"Checking {len(new)} ISBN(s) against the bol catalog...")
            keep = []
            for p in new:
                if api.known_product(p["ean"]):
                    keep.append(p)
                else:
                    print(f"   SKIP '{p['title']}': EAN {p['ean']} is not in the bol catalog")
            new = keep
        if not operator:
            ops = api.economic_operators()
            operator = ops[0].get("id") if ops else None
            if not ops:
                print("   ! no economic operator (GPSR) in your account - offers may go offline until you add one")
    elif not api and new:
        print("(dry run without credentials: catalog check and economic operator lookup skipped)")

    print(f"{len(new)} new offer(s)" + (f", {len(resend)} to update" if resend else "")
          + (f", {len(zero)} sold-out to set to stock 0" if zero else "") + ".")
    if not (new or resend or zero):
        return
    PREVIEW_FILE.write_text(json.dumps([offer_body(p, a, operator) for p in new], ensure_ascii=False, indent=2),
                            encoding="utf-8")
    if a.dry_run:
        print(f"Dry run: offers written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(new) + len(resend) + len(zero)} LIVE offer(s) on bol.com. "
                 "Type 'yes': ").strip().lower() != "yes":
            return
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")

    created = 0
    for p in new:
        body = offer_body(p, a, operator)
        code, data = api.create_offer(body)
        offer_id = data.get("offerId") if code in (200, 201) else None
        if code == 409:                          # an offer for this EAN + condition already exists
            match = next((o for o in api.find_offers(p["ean"]) if o.get("reference") == p["sku"]), None)
            if match:
                offer_id = match.get("offerId")
                print(f"   = {p['sku']}: already on bol as offer {offer_id} - linked")
        if not offer_id:
            print(f"   ! {p['sku']}: {code} {problem_text(data)}")
            continue
        unit = body["pricing"]["bundlePrices"][0]["unitPrice"]
        state[p["sku"]] = {"offer_id": offer_id, "ean": p["ean"], "price": unit, "stock": body["stock"]["amount"],
                           "condition": body["condition"], "pushed_at": now()}
        save_state(state)
        created += 1
        if code != 409:
            print(f"   + {p['sku']}: offer {offer_id}  {unit:.2f} EUR x{body['stock']['amount']}  "
                  f"({body['condition']['category']})")

    for p in resend + zero:
        info = state[p["sku"]]
        price = float(money(p["price"], a.price_factor)) if p in resend else info.get("price")
        stock = int(p["quantity"] or a.quantity) if p in resend else 0
        patch = {}
        if price != info.get("price"):
            patch["pricing"] = {"bundlePrices": [{"quantity": 1, "unitPrice": price}]}
        if stock != info.get("stock"):
            patch["stock"] = {"amount": stock, "managedByRetailer": True}
        if not patch:
            print(f"   = {p['sku']}: unchanged")
            continue
        code, data = api.patch_offer(info["offer_id"], patch)
        if code not in (200, 202, 204):
            print(f"   ! {p['sku']}: update failed: {code} {problem_text(data)}")
            continue
        info.update(price=price, stock=stock, updated_at=now())
        save_state(state)
        print(f"   ~ {p['sku']}: {price:.2f} EUR x{stock}" + ("  (sold out)" if p in zero else ""))
    print(f"Done: {created} created. Run `status` to see whether they are for sale.")


def cmd_status(api, a):
    state = load_state()
    if not state:
        print("No offers yet.")
        return
    for sku, info in sorted(state.items()):
        code, o = api.offer(info["offer_id"])
        if code == 404:
            print(f"{sku}: offer {info['offer_id']} not found on bol (deleted?)")
            continue
        if code != 200:
            print(f"{sku}: lookup failed ({code}): {problem_text(o)}")
            continue
        selling = [c["countryCode"] for c in o.get("countryAvailabilities") or [] if c.get("forSale")]
        price = ((o.get("pricing") or {}).get("bundlePrices") or [{}])[0].get("unitPrice")
        stock = o.get("stock") or {}
        line = (f"{sku}: " + (f"FOR SALE in {', '.join(selling)}" if selling else "not for sale")
                + f"  price={price}  stock={stock.get('amount')} (available {stock.get('correctedStock', stock.get('amount'))})"
                + ("  ON HOLD" if o.get("onHoldByRetailer") else ""))
        if not selling or len(selling) < len(o.get("countryAvailabilities") or []):
            reasons = ["{}: {}".format(c.get("countryCode"), ", ".join(r.get("description", "") for r in c.get("reasons") or []))
                       for c in api.not_for_sale_reasons(info["offer_id"]) if c.get("reasons")]
            if reasons:
                line += "  reasons: " + "; ".join(reasons)
        print(line)


def parse_days(s):
    try:
        lo, hi = (int(x) for x in s.split("-"))
        assert 0 <= lo <= hi
        return lo, hi
    except (ValueError, AssertionError):
        raise argparse.ArgumentTypeError("use MIN-MAX days, e.g. 2-4")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push offers to bol.com (Retailer API, Offers v11)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the credentials and list economic operators")
    s = sub.add_parser("lookup", help="check whether bol knows an ISBN/EAN")
    s.add_argument("isbn")
    p = sub.add_parser("push", help="create offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--countries", default=",".join(COUNTRIES), help="NL, BE or NL,BE")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--delivery-days", type=parse_days, help="state the delivery time per offer, e.g. 2-4 "
                                                             "(default: your Seller Dashboard delivery promise)")
    p.add_argument("--resend", action="store_true", help="update price/stock of already pushed offers")
    p.add_argument("--zero-sold-out", action="store_true", help="set stock 0 for pushed offers now sold out")
    p.add_argument("--no-match-check", action="store_true", help="skip the catalog lookup")
    p.add_argument("--dry-run", action="store_true", help="write bol_offers.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="list your offers, whether they are for sale, and why not")
    a = ap.parse_args()
    if a.cmd == "push":
        a.countries = [c.strip().upper() for c in a.countries.split(",") if c.strip()]
        bad = [c for c in a.countries if c not in ("NL", "BE")]
        if bad or not a.countries:
            sys.exit(f"--countries must be NL, BE or NL,BE (got {', '.join(bad) or 'nothing'})")
        if a.dry_run and not all(os.environ.get(k) for k in ("BOL_CLIENT_ID", "BOL_CLIENT_SECRET")):
            return cmd_push(None, a)             # no credentials needed to build the preview
    api = Bol()
    try:
        {"check": cmd_check, "lookup": cmd_lookup, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except RuntimeError as e:
        sys.exit(f"bol.com API error: {e}")


if __name__ == "__main__":
    main()
