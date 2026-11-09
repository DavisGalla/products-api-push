#!/usr/bin/env python3
"""
Push products to Kaufland.es (Kaufland Global Marketplace) through the
official Kaufland Seller API v2 (https://sellerapi.kaufland.com).

Each product is matched to an EXISTING Kaufland catalog product by its
ISBN/EAN, and your offer (a "unit": price, stock, condition, handling time)
is created on the chosen storefront. Books not in Kaufland's catalog are
skipped. One Kaufland seller account covers every storefront (es, de, at,
fr, it, nl, sk, cz, pl); this script defaults to Spain, and --storefronts
serves several in one run (each gets its own offers; prices are converted to
CZK / PLN for kaufland.cz / kaufland.pl).

ONE-TIME SETUP (see docs/kaufland_push.md)
  Kaufland Seller Portal -> Settings -> API (sellerportal.kaufland.de/settings/api):
  generate a Client Key and a Secret Key (the secret is shown only once).
  In the Seller Portal, set up a shipping group and a default warehouse for
  the Spanish storefront. Then (PowerShell), or as Bitwarden secrets:
      $env:KAUFLAND_CLIENT_KEY = "..."      # 32 characters
      $env:KAUFLAND_SECRET_KEY = "..."      # 64 characters

USAGE
    pip install requests
    python kaufland_push.py check                  # test the keys, list storefronts
    python kaufland_push.py push --dry-run         # build the offers, send nothing
    python kaufland_push.py push --limit 1         # one real offer
    python kaufland_push.py push                   # everything
    python kaufland_push.py push --resend --zero-sold-out   # sync price/stock, stock 0 for sold-out
    python kaufland_push.py status                 # what's live (and why not)
    python kaufland_push.py push --storefronts es,de,fr    # several storefronts in one run
    python kaufland_push.py push --storefronts all         # all 9 (or: eur = the 7 euro ones)

NOTE: new books in Spain have a legally fixed price (Ley 10/2007, max 5%
discount). Don't use --price-factor to sell above the cover price.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlencode

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

STOREFRONT = "es"            # kaufland.es. Others: de, at, cz, sk, pl, fr, it, nl (see `check`)
ALL_STOREFRONTS = ["es", "de", "at", "fr", "it", "nl", "sk", "cz", "pl"]
STOREFRONT_CURRENCY = {"cz": "CZK", "pl": "PLN"}   # all others: EUR
# Source prices are EUR. Rates for kaufland.cz / kaufland.pl; update them regularly.
EUR_RATE = {"EUR": "1", "CZK": "25.0", "PLN": "4.25"}

# Source condition -> Kaufland unit condition.
CONDITION_MAP = {
    "NewCondition": "NEW",
    "UsedCondition": "USED___VERY_GOOD",
    "RefurbishedCondition": "USED___GOOD",
    "DamagedCondition": "USED___ACCEPTABLE",
}

DEFAULT_HANDLING_DAYS = 3    # working days until you ship
SHIPPING_GROUP_ID = ""       # optional: id_shipping_group from the Seller Portal ("" = default)
WAREHOUSE_ID = ""            # optional: id_warehouse ("" = your default warehouse)
# ======================================================================

API_URL = "https://sellerapi.kaufland.com/v2"
STATE_FILE = Path("kaufland_posted.json")
PREVIEW_FILE = Path("kaufland_units.json")
USER_AGENT = "Inhouse_development"
MAX_NOTE = 250
PREVIEW_ROWS = []
SKIPS_SHOWN = set()


# --------------------------------------------------------------------------- API

class Kaufland:
    def __init__(self):
        self.client = os.environ.get("KAUFLAND_CLIENT_KEY")
        self.secret = os.environ.get("KAUFLAND_SECRET_KEY")
        missing = [k for k, v in (("KAUFLAND_CLIENT_KEY", self.client), ("KAUFLAND_SECRET_KEY", self.secret)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.base = (os.environ.get("KAUFLAND_API_URL") or API_URL).rstrip("/")

    @staticmethod
    def signature(secret, method, uri, body, timestamp):
        """HMAC-SHA256 over METHOD\\nURI\\nBODY\\nTIMESTAMP, lowercase hex (Kaufland docs)."""
        msg = "\n".join([method, uri, body, str(timestamp)])
        return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def call(self, method, path, params=None, body=None, retries=5):
        uri = f"{self.base}{path}" + (f"?{urlencode(params)}" if params else "")
        data = json.dumps(body, ensure_ascii=False) if body is not None else ""
        for attempt in range(retries):
            ts = int(time.time())
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT,
                       "Shop-Client-Key": self.client, "Shop-Timestamp": str(ts),
                       "Shop-Signature": self.signature(self.secret, method, uri, data, ts)}
            if body is not None:
                headers["Content-Type"] = "application/json"
            r = requests.request(method, uri, data=data.encode() if data else None, headers=headers, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                print(f"   (API returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                payload = r.json() if r.content else {}
            except ValueError:
                payload = {"message": r.text[:300]}
            if r.status_code == 401:
                sys.exit(f"401 Unauthorized: {error_text(payload)}\n"
                         "Check KAUFLAND_CLIENT_KEY / KAUFLAND_SECRET_KEY and your PC clock "
                         "(the timestamp must be within 5 minutes).")
            if r.status_code == 403:
                sys.exit(f"403 Forbidden: {error_text(payload)} (is the seller account active?)")
            return r.status_code, payload
        return r.status_code, {"message": "gave up after retries"}

    def storefronts(self):
        return self.call("GET", "/info/storefront")

    def product_by_ean(self, ean, storefront):
        return self.call("GET", f"/products/ean/{ean}", {"storefront": storefront})

    def create_unit(self, body):                 # also updates when id_offer already exists
        return self.call("POST", "/units/", body=body)

    def units(self, storefront):
        out, offset = [], 0
        while True:
            code, data = self.call("GET", "/units/", {"storefront": storefront, "limit": 100, "offset": offset})
            if code != 200:
                raise RuntimeError(f"unit list failed ({code}): {error_text(data)}")
            page = data.get("data", [])
            out += page
            offset += len(page)
            if not page or offset >= data.get("pagination", {}).get("total", 0):
                return out

    def unit_reasons(self, unit_ids, storefront):  # why a unit isn't live (max 20 per call)
        reasons = {}
        for i in range(0, len(unit_ids), 20):
            code, data = self.call("POST", "/units/status", {"storefront": storefront},
                                   body={"unit_ids": unit_ids[i:i + 20]})
            if code in (200, 207):
                for d in data.get("data", []):
                    reasons[d.get("id_unit")] = d.get("reasons") or []
        return reasons


def error_text(payload):
    if not isinstance(payload, dict):
        return str(payload)[:300]
    msg = payload.get("message") or payload.get("type") or "error"
    details = "; ".join(f"{e.get('field', '?')}: {e.get('message', '')}" for e in payload.get("errors") or [])
    return f"{msg} ({details})" if details else msg


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
        probs.append("no ISBN/EAN (needed to match the Kaufland catalog)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in CONDITION_MAP:
        probs.append(f"no Kaufland condition for '{p['condition']}' (set CONDITION_MAP)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def currency_of(storefront):
    return STOREFRONT_CURRENCY.get(storefront.lower(), "EUR")


def to_cents(price, factor=1.0, currency="EUR"):
    """Price in EUR -> integral cents of the storefront currency, rounded half-up (8.075 -> 808)."""
    value = Decimal(str(price)) * Decimal(str(factor)) * Decimal(EUR_RATE[currency]) * 100
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def unit_body(p, a, amount=None):
    body = {
        "ean": p["ean"],
        "id_offer": p["sku"],
        "condition": CONDITION_MAP[p["condition"]],
        "listing_price": to_cents(p["price"], a.price_factor, currency_of(a.storefront)),   # integral cents
        "amount": min(int(amount if amount is not None else (p["quantity"] or a.quantity)), 99999),
        "handling_time": a.handling_days,
        "storefront": a.storefront,
        "note": (a.note or "")[:MAX_NOTE],
    }
    if a.shipping_group:
        body["id_shipping_group"] = a.shipping_group
    if a.warehouse:
        body["id_warehouse"] = a.warehouse
    return body


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    code, data = api.storefronts()
    if code != 200:
        sys.exit(f"Storefront lookup failed ({code}): {error_text(data)}")
    items = data.get("data", data)
    fronts = [s.get("storefront", s) if isinstance(s, dict) else s for s in (items if isinstance(items, list) else [])]
    print("Keys accepted. Storefronts: " + (", ".join(map(str, fronts)) or str(items)))
    if fronts and a.storefront not in [str(f).lower() for f in fronts]:
        print(f"   ! storefront '{a.storefront}' is not in the list - check STOREFRONT / --storefront")


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}  (storefront: {a.storefront})")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()
    key = lambda p: f"{a.storefront.upper()}:{p['sku']}"

    todo, zero = [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and a.zero_sold_out and key(p) in state and state[key(p)].get("amount", 1) > 0:
            zero.append(p)                       # previously listed, now sold out -> stock 0
            continue
        probs = problems_of(p)
        if probs:
            if i not in SKIPS_SHOWN:                 # report a bad book once, not once per storefront
                SKIPS_SHOWN.add(i)
                print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if key(p) in state and not a.resend:
            print(f"[{i}] already pushed: {p['title']}")
            continue
        todo.append(p)
    if a.limit:
        todo = todo[: a.limit]

    if api and not a.no_match_check and todo:
        print(f"Checking {len(todo)} EAN(s) against the Kaufland catalog...")
        keep = []
        for p in todo:
            code, data = api.product_by_ean(p["ean"], a.storefront)
            if code == 200 and data.get("data"):
                keep.append(p)
            elif code == 404 or (code == 200 and not data.get("data")):
                print(f"   SKIP '{p['title']}': EAN {p['ean']} is not in the Kaufland catalog")
            else:
                print(f"   ! {p['sku']}: catalog lookup failed ({code}): {error_text(data)}")
        todo = keep
    elif not api:
        print("(dry run without credentials: catalog check skipped)")

    bodies = [unit_body(p, a) for p in todo] + [unit_body(p, a, amount=0) for p in zero]
    print(f"{len(todo)} offer(s) to push" + (f", {len(zero)} sold-out offer(s) to set to stock 0" if zero else "") + ".")
    if not bodies:
        return
    PREVIEW_ROWS.extend(bodies)                  # one preview file for all storefronts of this run
    PREVIEW_FILE.write_text(json.dumps(PREVIEW_ROWS, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.dry_run:
        print(f"Dry run: offers written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will send {len(bodies)} LIVE offer(s) to Kaufland ({a.storefront}). Type 'yes': ").strip().lower() != "yes":
            return

    ok = failed = 0
    for p, body in zip(todo + zero, bodies):
        code, data = api.create_unit(body)
        if code in (200, 201):
            unit = data.get("data", data) if isinstance(data, dict) else {}
            state[key(p)] = {"ean": p["ean"], "id_unit": unit.get("id_unit"), "listing_price": body["listing_price"],
                             "amount": body["amount"], "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_state(state)
            print(f"   + {p['sku']}: {body['listing_price'] / 100:.2f} {currency_of(a.storefront)} x{body['amount']}"
                  + (f" -> unit {unit.get('id_unit')}" if unit.get("id_unit") else ""))
            ok += 1
        else:
            print(f"   ! {p['sku']}: {code} {error_text(data)}")
            failed += 1
        time.sleep(0.05)
    print(f"Done: {ok} sent, {failed} rejected. Units can take a few minutes to go live - run `status`.")


def cmd_status(api, a):
    state = load_state()
    units = api.units(a.storefront)
    by_offer = {u.get("id_offer"): u for u in units}
    prefix = a.storefront.upper() + ":"
    mine = {k[len(prefix):] for k in state if k.startswith(prefix)}
    if not mine and not units:
        print("No offers yet.")
        return
    not_live = [u["id_unit"] for u in units if not u.get("is_live") and u.get("id_unit")]
    reasons = api.unit_reasons(not_live, a.storefront) if not_live else {}
    for sku in sorted(mine | set(by_offer)):
        u = by_offer.get(sku)
        if not u:
            print(f"{sku}: not found on Kaufland (yet - new units can take a few minutes)")
            continue
        live = "LIVE" if u.get("is_live") else "not live"
        why = reasons.get(u.get("id_unit"))
        print(f"{sku}: {live}  price={(u.get('listing_price') or 0) / 100:.2f}  qty={u.get('amount')}  "
              f"status={u.get('status')}" + (f"  reasons={', '.join(map(str, why))}" if why else "")
              + ("" if sku in mine else "  (not pushed by this script)"))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--storefront", default=STOREFRONT, help=f"one storefront (default: {STOREFRONT})")
    common.add_argument("--storefronts", help="several, comma separated (es,de,fr), or 'eur' / 'all'")
    ap = argparse.ArgumentParser(description="Push offers to Kaufland (Seller API v2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", parents=[common], help="test the keys and list storefronts")
    p = sub.add_parser("push", parents=[common], help="create offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'quantity' wins)")
    p.add_argument("--handling-days", type=int, default=DEFAULT_HANDLING_DAYS, help="working days until you ship")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--note", default="", help=f"offer note shown to buyers (max {MAX_NOTE} chars)")
    p.add_argument("--shipping-group", default=SHIPPING_GROUP_ID, help="id_shipping_group (default: Seller Portal default)")
    p.add_argument("--warehouse", default=WAREHOUSE_ID, help="id_warehouse (default: your default warehouse)")
    p.add_argument("--resend", action="store_true", help="also re-send already pushed offers (price/stock sync)")
    p.add_argument("--zero-sold-out", action="store_true", help="set stock 0 for pushed offers now sold out")
    p.add_argument("--no-match-check", action="store_true", help="skip the catalog lookup")
    p.add_argument("--dry-run", action="store_true", help="write kaufland_units.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", parents=[common], help="list your offers and whether they are live")
    a = ap.parse_args()
    fronts = parse_storefronts(a)
    no_keys = not all(os.environ.get(k) for k in ("KAUFLAND_CLIENT_KEY", "KAUFLAND_SECRET_KEY"))
    api = None if (a.cmd == "push" and a.dry_run and no_keys) else Kaufland()   # dry run needs no keys
    for sf in fronts:
        a.storefront = sf
        if len(fronts) > 1:
            print(f"--- kaufland.{sf} ({currency_of(sf)}) ---")
        {"check": cmd_check, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)


def parse_storefronts(a):
    if a.storefronts:
        v = a.storefronts.strip().lower()
        if v == "all":
            fronts = list(ALL_STOREFRONTS)
        elif v == "eur":
            fronts = [s for s in ALL_STOREFRONTS if currency_of(s) == "EUR"]
        else:
            fronts = [s.strip().lower() for s in v.split(",") if s.strip()]
        bad = [s for s in fronts if s not in ALL_STOREFRONTS]
        if bad or not fronts:
            sys.exit(f"Unknown Kaufland storefront(s): {', '.join(bad) or '(none)'}. "
                     f"Known: {', '.join(ALL_STOREFRONTS)}, or 'eur' / 'all'.")
        return list(dict.fromkeys(fronts))
    return [a.storefront.lower()]


if __name__ == "__main__":
    main()
