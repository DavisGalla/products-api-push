#!/usr/bin/env python3
"""
Push books to eMAG Marketplace (Romania, Bulgaria, Hungary) through the
official eMAG Marketplace API (documentation v4.5.1, "api-3").

Each book is matched to an EXISTING eMAG product by its ISBN/EAN
(documentation/find_by_eans), and your offer (price, stock, handling time,
VAT) is attached to it with product_offer/save, using the product's
part_number_key. Books eMAG doesn't know are skipped: creating a new product
needs the full documentation and a manual review by eMAG. Price and stock
updates use the light offer API (offer/save).

ONE-TIME SETUP (see docs/emag_push.md)
  eMAG Marketplace (marketplace.emag.ro / .bg / .hu) -> create an API user
  and send eMAG the IP addresses the script runs from (eMAG only accepts
  API calls from whitelisted IPs). Request access to the books categories.
  Then (PowerShell), or as Bitwarden secrets:
      $env:EMAG_USERNAME = "..."
      $env:EMAG_PASSWORD = "..."

USAGE
    pip install requests
    python emag_push.py check                    # login, VAT rates, handling times
    python emag_push.py lookup 9788409591923     # does eMAG know this ISBN?
    python emag_push.py push --dry-run           # build the offers, send nothing
    python emag_push.py push --limit 1           # one real offer
    python emag_push.py push                     # everything
    python emag_push.py push --resend --zero-sold-out   # sync price/stock, stock 0 for sold-out
    python emag_push.py status                   # active? price valid? buy button rank

NOTE: eMAG prices are WITHOUT VAT. The script converts your gross EUR prices.
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

PLATFORM = "RO"              # "RO" (emag.ro), "BG" (emag.bg) or "HU" (emag.hu)
# Reduced VAT rate for books on each platform, in percent: used to find the vat_id
# and to take VAT out of your gross prices. Check them with `check`.
VAT_RATE = {"RO": 11, "BG": 9, "HU": 5}
VAT_ID = None                # force a vat_id instead (see `check`)
# Send prices in EUR (eMAG converts them; BG uses EUR anyway). False = local currency via the rate below.
PRICES_IN_EUR = True
EUR_RATE = {"RO": 5.09, "HU": 390.0}   # only used when PRICES_IN_EUR = False
# Price check window required by eMAG on the first save: sale price must stay between these.
MIN_PRICE_FACTOR = 0.8
MAX_PRICE_FACTOR = 1.5
HANDLING_TIME_DAYS = 2       # days from order to shipment (valid values: `check`)
WAREHOUSE_ID = 1             # 1 when you have a single warehouse
# eMAG offers are for new products. Only these source conditions are pushed.
ALLOWED_CONDITIONS = {"NewCondition"}
# ======================================================================

API_URLS = {"RO": "https://marketplace-api.emag.ro/api-3",
            "BG": "https://marketplace-api.emag.bg/api-3",
            "HU": "https://marketplace-api.emag.hu/api-3"}
CURRENCY = {"RO": "RON", "BG": "EUR", "HU": "HUF"}
STATE_FILE = Path("emag_posted.json")
IDS_FILE = Path("emag_ids.json")        # your SKU -> eMAG numeric offer id (kept forever)
PREVIEW_FILE = Path("emag_offers.json")
BATCH = 50                               # max entities per save request
EAN_BATCH = 100                          # max EANs per find_by_eans request
MIN_INTERVAL = 0.35                      # eMAG: max 3 requests/second (non-order resources)


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------- API

class Emag:
    def __init__(self):
        user, pw = os.environ.get("EMAG_USERNAME"), os.environ.get("EMAG_PASSWORD")
        missing = [k for k, v in (("EMAG_USERNAME", user), ("EMAG_PASSWORD", pw)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
        self.base = (os.environ.get("EMAG_API_URL") or API_URLS[PLATFORM]).rstrip("/")
        self.last = 0.0

    def call(self, path, data=None, method="POST", params=None, retries=5):
        """POST {"data": ...} (or GET with params). Returns (ok, results, messages)."""
        for attempt in range(retries):
            wait = MIN_INTERVAL - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            headers = {"Authorization": f"Basic {self.auth}", "Accept": "application/json"}
            body = None
            if method == "POST":
                headers["Content-Type"] = "application/json"
                body = json.dumps({"data": data if data is not None else {}}, ensure_ascii=False).encode("utf-8")
            r = requests.request(method, f"{self.base}/{path}", params=params, data=body, headers=headers, timeout=120)
            if r.status_code in (401, 403):
                sys.exit(f"eMAG refused the login ({r.status_code}). Check EMAG_USERNAME / EMAG_PASSWORD, that the "
                         "user has API rights, and that this computer's IP address is whitelisted by eMAG.")
            if r.status_code == 429 or r.status_code >= 500:
                pause = 2 ** attempt
                print(f"   (eMAG returned {r.status_code}, retrying in {pause}s)")
                time.sleep(pause)
                continue
            try:
                res = r.json()
            except ValueError:
                raise ApiError(f"{path}: HTTP {r.status_code}, not JSON: {r.text[:200]}")
            if "isError" not in res:            # eMAG: a response without isError was not interpreted
                raise ApiError(f"{path}: unexpected response (no isError): {str(res)[:200]}")
            return (not res["isError"]), res.get("results"), messages(res.get("messages"))
        raise ApiError(f"{path}: gave up after {retries} tries")

    def read_all(self, path, filters=None):
        page = 1
        while True:
            ok, results, msgs = self.call(path, dict(filters or {}, currentPage=page, itemsPerPage=100))
            if not ok:
                raise ApiError(f"{path}: " + "; ".join(msgs))
            results = results or []
            yield from results
            if len(results) < 100:
                return
            page += 1

    def find_by_eans(self, eans):
        found = {}
        for i in range(0, len(eans), EAN_BATCH):
            part = eans[i:i + EAN_BATCH]
            ok, results, msgs = self.call("documentation/find_by_eans", method="GET",
                                          params=[("eans[]", e) for e in part])
            if not ok:
                raise ApiError("find_by_eans: " + "; ".join(msgs))
            for r in results or []:
                found.setdefault(str(r.get("eans") or r.get("ean")), r)
        return found


def messages(m):
    if not m:
        return []
    if isinstance(m, str):
        return [m]
    out = []
    for x in m:
        out.append(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False))
    return out


# ---------------------------------------------------------------------- helpers

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def to_ean(code):
    """Normalise an ISBN-10/13 or EAN to a valid EAN-13, or return None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    digit = lambda core: str((10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10)
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        return "978" + c[:9] + digit("978" + c[:9])
    if len(c) == 13 and c.isdigit() and digit(c[:12]) == c[12]:
        return c
    return None


def currency():
    return "EUR" if PRICES_IN_EUR or PLATFORM == "BG" else CURRENCY[PLATFORM]


def net_price(gross_eur, a, factor=1.0):
    """Gross EUR price -> eMAG sale price without VAT, in the offer currency, 4 decimals max (2 used)."""
    v = Decimal(str(gross_eur)) * Decimal(str(a.price_factor))
    if currency() != "EUR":
        v *= Decimal(str(EUR_RATE[PLATFORM]))
    v = v / (1 + Decimal(str(VAT_RATE[PLATFORM])) / 100) * Decimal(str(factor))
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize(raw):
    stock = raw.get("stock", raw.get("quantity"))
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": " ".join((raw.get("title") or "").split()),
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "stock": int(stock) if stock not in (None, "") else None,
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    if not p["title"]:
        probs.append("no title")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (eMAG matches offers by EAN)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in ALLOWED_CONDITIONS:
        probs.append(f"condition '{p['condition']}' is not sold on eMAG (new products only, see ALLOWED_CONDITIONS)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def offer_id(ids, sku):
    """Stable numeric eMAG offer id (1..16777215) for a SKU, kept in emag_ids.json."""
    if sku not in ids:
        ids[sku] = max(ids.values(), default=0) + 1
        if ids[sku] > 16777215:
            sys.exit("Out of eMAG offer ids (max 16777215).")
    return ids[sku]


def new_offer(p, a, oid, pnk, vat_id):
    body = {
        "id": oid,
        "part_number_key": pnk,
        "name": p["title"][:255],
        "status": 1,
        "sale_price": net_price(p["price"], a),
        "min_sale_price": net_price(p["price"], a, MIN_PRICE_FACTOR),
        "max_sale_price": net_price(p["price"], a, MAX_PRICE_FACTOR),
        "vat_id": vat_id,
        "stock": [{"warehouse_id": WAREHOUSE_ID, "value": p["stock"] if p["stock"] is not None else a.quantity}],
        "handling_time": [{"warehouse_id": WAREHOUSE_ID, "value": HANDLING_TIME_DAYS}],
    }
    if currency() != CURRENCY[PLATFORM]:
        body["currency_type"] = currency()
    return body


def find_vat_id(api):
    if VAT_ID is not None:
        return VAT_ID
    ok, rates, msgs = api.call("vat/read")
    if not ok:
        raise ApiError("vat/read: " + "; ".join(msgs))
    for v in rates or []:
        if float(v.get("vat_rate", -1)) == float(VAT_RATE[PLATFORM]):
            return v.get("vat_id")
    sys.exit(f"No eMAG VAT rate of {VAT_RATE[PLATFORM]}% on {PLATFORM}: rates are "
             + ", ".join(f"{v.get('vat_rate')}% (id {v.get('vat_id')})" for v in rates or [])
             + ". Fix VAT_RATE or set VAT_ID.")


def save_batches(api, path, items, label):
    """Save in batches of 50. A batch with isError is re-sent item by item to find the bad ones.
    Returns {id: error text} for the failed items."""
    failed = {}
    for i in range(0, len(items), BATCH):
        part = items[i:i + BATCH]
        ok, _, msgs = api.call(path, part)
        if ok:
            continue
        if len(part) == 1:
            failed[part[0]["id"]] = "; ".join(msgs) or "rejected"
            continue
        print(f"   ({label}: eMAG reported an error in a batch of {len(part)}; re-sending one by one)")
        for item in part:
            ok, _, msgs = api.call(path, [item])
            if not ok:
                failed[item["id"]] = "; ".join(msgs) or "rejected"
    return failed


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    ok, rates, msgs = api.call("vat/read")
    if not ok:
        sys.exit("vat/read failed: " + "; ".join(msgs))
    print(f"Login OK ({api.base}, platform {PLATFORM}, offers in {currency()})")
    print("   VAT rates: " + ", ".join(f"{v.get('vat_rate')}% = id {v.get('vat_id')}" for v in rates or []))
    match = [v for v in rates or [] if float(v.get("vat_rate", -1)) == float(VAT_RATE[PLATFORM])]
    print(f"   books VAT {VAT_RATE[PLATFORM]}%: " + (f"vat_id {match[0]['vat_id']}" if match or VAT_ID
                                                    else "! not found - set VAT_RATE or VAT_ID"))
    ok, times, msgs = api.call("handling_time/read")
    vals = [t.get("value") for t in times or []]
    print(f"   handling times: {vals}" + ("" if HANDLING_TIME_DAYS in vals or not vals
                                          else f"  ! HANDLING_TIME_DAYS={HANDLING_TIME_DAYS} is not one of them"))


def cmd_lookup(api, a):
    ean = to_ean(a.isbn)
    if not ean:
        sys.exit(f"Not a valid ISBN/EAN: {a.isbn}")
    hit = api.find_by_eans([ean]).get(ean)
    if not hit:
        print(f"{ean}: not in the eMAG catalog")
        return
    print(f"{ean}: {hit.get('product_name')}  PNK {hit.get('part_number_key')}  category '{hit.get('category_name')}'"
          f"  you may add an offer: {hit.get('allow_to_add_offer')}  you already have one: {hit.get('vendor_has_offer')}")


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}  (eMAG {PLATFORM}, prices without VAT in {currency()})")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state, ids = load_json(STATE_FILE, {}), load_json(IDS_FILE, {})

    new, resend, gone = [], [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and (a.zero_sold_out or a.offline_sold_out) and p["sku"] in state:
            info = state[p["sku"]]
            if (a.zero_sold_out and info.get("stock", 1) > 0) or (a.offline_sold_out and info.get("status", 1) == 1):
                gone.append(p)
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

    pnks = {}
    if api and new:
        print(f"Looking up {len(new)} ISBN(s) in the eMAG catalog...")
        found = api.find_by_eans([p["ean"] for p in new])
        keep = []
        for p in new:
            hit = found.get(p["ean"])
            if not hit or not hit.get("part_number_key"):
                print(f"   SKIP '{p['title']}': EAN {p['ean']} is not in the eMAG catalog")
            elif not hit.get("allow_to_add_offer"):
                print(f"   SKIP '{p['title']}': no access to category '{hit.get('category_name')}' "
                      "(request it in the eMAG Marketplace interface)")
            elif hit.get("vendor_has_offer") and p["sku"] not in ids:
                print(f"   SKIP '{p['title']}': you already have an offer on {hit['part_number_key']} "
                      "(made outside this script)")
            else:
                pnks[p["sku"]] = hit["part_number_key"]
                keep.append(p)
        new = keep
    elif not api and new:
        print("(dry run without credentials: catalog lookup skipped, PNK and vat_id are placeholders)")
        pnks = {p["sku"]: "PNK?" for p in new}

    vat_id = find_vat_id(api) if api and new else (VAT_ID or 0)
    bodies = [new_offer(p, a, offer_id(ids, p["sku"]), pnks[p["sku"]], vat_id) for p in new]
    print(f"{len(bodies)} new offer(s)" + (f", {len(resend)} to update" if resend else "")
          + (f", {len(gone)} sold-out" if gone else "") + ".")
    if not (bodies or resend or gone):
        return
    PREVIEW_FILE.write_text(json.dumps(bodies, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.dry_run:
        print(f"Dry run: offers written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(bodies) + len(resend) + len(gone)} LIVE offer(s) on eMAG {PLATFORM}. "
                 "Type 'yes': ").strip().lower() != "yes":
            return
    save_json(IDS_FILE, ids)
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")

    failed = save_batches(api, "product_offer/save", bodies, "offers")
    created = 0
    for p, b in zip(new, bodies):
        if b["id"] in failed:
            print(f"   ! {p['sku']}: {failed[b['id']]}")
            continue
        state[p["sku"]] = {"id": b["id"], "pnk": b["part_number_key"], "ean": p["ean"], "price": b["sale_price"],
                           "min": b["min_sale_price"], "max": b["max_sale_price"], "currency": currency(),
                           "stock": b["stock"][0]["value"], "status": 1, "pushed_at": now()}
        created += 1
        print(f"   + {p['sku']}: offer {b['id']} on {b['part_number_key']}  {b['sale_price']:.2f} {currency()} "
              f"excl. VAT x{b['stock'][0]['value']}")
    save_json(STATE_FILE, state)

    updates = []
    for p in resend:
        info = state[p["sku"]]
        price, stock = net_price(p["price"], a), p["stock"] if p["stock"] is not None else a.quantity
        u = {"id": info["id"]}
        if price != info.get("price"):
            u["sale_price"] = price
            if currency() != CURRENCY[PLATFORM]:
                u["currency_type"] = currency()
            if not (info.get("min", 0) <= price <= info.get("max", 0)):   # outside the price check window
                u["min_sale_price"] = net_price(p["price"], a, MIN_PRICE_FACTOR)
                u["max_sale_price"] = net_price(p["price"], a, MAX_PRICE_FACTOR)
        if stock != info.get("stock"):
            u["stock"] = [{"warehouse_id": WAREHOUSE_ID, "value": stock}]
        if info.get("status") != 1:
            u["status"] = 1
        if len(u) == 1:
            print(f"   = {p['sku']}: unchanged")
            continue
        updates.append((p["sku"], u))
    for p in gone:
        info = state[p["sku"]]
        u = {"id": info["id"]}
        if a.zero_sold_out and info.get("stock", 1) > 0:
            u["stock"] = [{"warehouse_id": WAREHOUSE_ID, "value": 0}]
        if a.offline_sold_out and info.get("status", 1) == 1:
            u["status"] = 0
        updates.append((p["sku"], u))
    if updates:
        failed = save_batches(api, "offer/save", [u for _, u in updates], "updates")
        for sku, u in updates:
            if u["id"] in failed:
                print(f"   ! {sku}: update failed: {failed[u['id']]}")
                continue
            info = state[sku]
            if "sale_price" in u:
                info["price"] = u["sale_price"]
            if "min_sale_price" in u:
                info.update(min=u["min_sale_price"], max=u["max_sale_price"])
            if "stock" in u:
                info["stock"] = u["stock"][0]["value"]
            if "status" in u:
                info["status"] = u["status"]
            info["updated_at"] = now()
            parts = []
            if "sale_price" in u:
                parts.append(f"{u['sale_price']:.2f} {currency()}")
            if "stock" in u:
                parts.append(f"stock {u['stock'][0]['value']}")
            if "status" in u:
                parts.append("active" if u["status"] else "inactive (sold out)")
            print(f"   ~ {sku}: " + ", ".join(parts))
        save_json(STATE_FILE, state)
    print(f"Done: {created} created. Run `status` to see whether they are for sale.")


def cmd_status(api, a):
    state = load_json(STATE_FILE, {})
    if not state:
        print("No offers yet.")
        return
    by_id = {}
    for o in api.read_all("product_offer/read"):
        by_id[o.get("id")] = o
    for sku in sorted(state):
        info = state[sku]
        o = by_id.get(info["id"])
        if not o:
            print(f"{sku}: offer {info['id']} not found on eMAG")
            continue
        val = o.get("validation_status") or [{}]
        val = val[0] if isinstance(val, list) and val else val
        ov = o.get("offer_validation_status") or {}
        ov = ov[0] if isinstance(ov, list) and ov else ov
        selling = (o.get("status") == 1 and ov.get("value") == 1 and (o.get("general_stock") or 0) > 0
                   and val.get("value") in (3, 9, 11, 12))
        print(f"{sku}: " + ("FOR SALE" if selling else "not for sale")
              + f"  status={o.get('status')}  price={o.get('sale_price')} {o.get('currency', '')} ({ov.get('description')})"
              + f"  doc: {val.get('description')}  stock={o.get('general_stock')}"
              + (f"  buy button rank {o.get('buy_button_rank')} of {o.get('number_of_offers')}"
                 f" (best price {o.get('best_offer_sale_price')})" if o.get("number_of_offers") else ""))
        for err in val.get("errors") or []:
            print(f"      ! {err}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to eMAG Marketplace (API v4.5)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the login, list VAT rates and handling times")
    s = sub.add_parser("lookup", help="check whether eMAG knows an ISBN/EAN")
    s.add_argument("isbn")
    p = sub.add_parser("push", help="create offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'stock'/'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--resend", action="store_true", help="update price/stock of already pushed offers")
    p.add_argument("--zero-sold-out", action="store_true", help="set stock 0 for pushed offers now sold out")
    p.add_argument("--offline-sold-out", action="store_true", help="set pushed offers now sold out to inactive")
    p.add_argument("--dry-run", action="store_true", help="write emag_offers.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="offer status, price validation and buy button rank")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run and not os.environ.get("EMAG_USERNAME"):
        return cmd_push(None, a)
    api = Emag()
    try:
        {"check": cmd_check, "lookup": cmd_lookup, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except ApiError as e:
        sys.exit(f"eMAG API error: {e}")


if __name__ == "__main__":
    main()
