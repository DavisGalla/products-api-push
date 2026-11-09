#!/usr/bin/env python3
"""
Push books to OTTO Market (otto.de, Germany) through the official OTTO
Market API (https://api.otto.market/docs): Products API v5 for the product
data and price, Availability API v1 for stock and delivery.

On OTTO every seller describes the product itself (there is no "offer on an
existing catalog page"): each book becomes a product variation with its EAN,
one OTTO category, a brand from OTTO's brand list, a description, images,
the price and the GPSR (product safety) address. OTTO processes the upload
asynchronously; the script waits for the result and reads the rejected
products with their error messages. Stock and the shipping profile are set
with the Availability API once a product is accepted.

ONE-TIME SETUP (see docs/otto_push.md)
  OTTO Partner Connect -> create a "self-app" with the interfaces
  "products" and "availability". You get a client ID and a client secret.
  Then (PowerShell), or as Bitwarden secrets:
      $env:OTTO_CLIENT_ID = "..."
      $env:OTTO_CLIENT_SECRET = "..."
  Set up a shipping profile in Partner Connect, and fill in CATEGORY,
  BRAND_ID and GPSR_DISTRIBUTOR below (find them with the commands).

USAGE
    pip install requests
    python otto_push.py check                       # token, shipping profiles, settings
    python otto_push.py categories --find Buch      # find the books category
    python otto_push.py attributes "<category>"     # its attributes
    python otto_push.py brands --find Planeta       # find brand IDs (publishers)
    python otto_push.py push --dry-run              # build the products, send nothing
    python otto_push.py push --limit 1              # one real product
    python otto_push.py push                        # everything
    python otto_push.py push --resend --zero-sold-out   # sync price/stock, stock 0 for sold-out
    python otto_push.py status                      # ONLINE / PENDING / REJECTED and why

NOTE: new books have a fixed price in Germany (Buchpreisbindung): the price
must be the German fixed price. Prices include 7% VAT ("REDUCED").
"""
import argparse
import html
import json
import os
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlparse

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

CATEGORY = ""                # OTTO category for books (find it: `categories --find Buch`)
BRAND_ID = ""                # brand ID used when the publisher isn't an OTTO brand (`brands --find ...`)
MATCH_PUBLISHER_BRAND = True # use the publisher's own OTTO brand when one with exactly that name is usable
VAT = "REDUCED"              # books: reduced VAT (7%) - prices include it
# Fixed attributes for every book, e.g. {"Sprache": ["Spanisch"]} (names/values: `attributes <category>`).
ATTRIBUTES = {}
# Source field -> OTTO attribute name, e.g. {"author": "Autor", "pages": "Seitenanzahl"}.
FIELD_ATTRIBUTES = {}
SHIPPING_PROFILE_ID = ""     # "" = your first shipping profile (see `check`)
PROCESSING_TIME = "DEFAULT"  # days to prepare an order (1-99) or "DEFAULT" (the profile's value)
# GPSR: at least one DISTRIBUTOR address is mandatory (usually your own company).
GPSR_DISTRIBUTOR = {"name": "", "address": "", "regionCode": "", "email": ""}
# OTTO sells new goods: there is no condition field. Only these source conditions are pushed.
ALLOWED_CONDITIONS = {"NewCondition"}
# ======================================================================

API_URL = "https://api.otto.market"
SCOPES = "products availability"
STATE_FILE = Path("otto_posted.json")
PREVIEW_FILE = Path("otto_products.json")
BRANDS_CACHE = Path("otto_brands.json")
BATCH = 500                  # max product variations / prices / statuses per request (API limit)
QTY_BATCH = 200              # quantities / delivery information per request


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------- API

class Otto:
    def __init__(self):
        self.client = os.environ.get("OTTO_CLIENT_ID")
        self.secret = os.environ.get("OTTO_CLIENT_SECRET")
        missing = [k for k, v in (("OTTO_CLIENT_ID", self.client), ("OTTO_CLIENT_SECRET", self.secret)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.base = (os.environ.get("OTTO_API_URL") or API_URL).rstrip("/")
        self.token, self.token_until = None, 0

    def access_token(self, force=False):
        """Client credentials token for the self-app, reused until it's about to expire."""
        if self.token and not force and time.time() < self.token_until - 60:
            return self.token
        r = requests.post(f"{self.base}/v1/token", timeout=60,
                          data={"grant_type": "client_credentials", "client_id": self.client,
                                "client_secret": self.secret, "scope": SCOPES},
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if r.status_code in (400, 401, 403):
            sys.exit(f"OTTO login failed ({r.status_code}): check OTTO_CLIENT_ID / OTTO_CLIENT_SECRET and that the "
                     f"self-app has the interfaces '{SCOPES}' (OTTO Partner Connect).")
        r.raise_for_status()
        data = r.json()
        self.token, self.token_until = data["access_token"], time.time() + int(data.get("expires_in", 1800))
        return self.token

    def call(self, method, path, params=None, body=None, retries=5):
        url = path if path.startswith("http") else f"{self.base}{path}"
        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {self.access_token()}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            r = requests.request(method, url, params=params, headers=headers, timeout=120,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
            if r.status_code == 401 and attempt == 0:
                self.access_token(force=True)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After") or 2 ** attempt)
                print(f"   (OTTO returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                data = r.json() if r.content else {}
            except ValueError:
                data = {"detail": r.text[:300]}
            if r.status_code == 403:
                raise ApiError(f"403 Forbidden on {path}: is the interface in your self-app? {err_text(data)}")
            return r.status_code, data
        raise ApiError(f"{method} {path}: gave up after {retries} tries (last HTTP {r.status_code})")

    def pages(self, path, key, params=None):
        """Follow the 'next' links of a paginated list."""
        url, params = path, dict(params or {})
        while url:
            code, data = self.call("GET", url, params=params)
            if code != 200:
                raise ApiError(f"GET {url}: {code} {err_text(data)}")
            yield from data.get(key) or []
            nxt = next((l.get("href") for l in data.get("links") or [] if l.get("rel") == "next"), None)
            url, params = nxt, None

    def wait_task(self, task, wait, poll):
        """Poll an update task until it is done (or the wait time is over)."""
        href = next((l["href"] for l in task.get("links") or [] if l.get("rel") == "self"), None)
        deadline = time.time() + wait
        while task.get("state") != "done" and href and time.time() < deadline:
            time.sleep(poll)
            code, task = self.call("GET", href)
            if code != 200:
                raise ApiError(f"update task {href}: {code} {err_text(task)}")
        return task

    def task_part(self, task, rel):
        href = next((l["href"] for l in task.get("links") or [] if l.get("rel") == rel), None)
        if not href:
            return []
        code, data = self.call("GET", href)
        return data.get("results") or [] if code == 200 else []


def err_text(data):
    if isinstance(data, dict):
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            return "; ".join(f"{e.get('title') or e.get('code')}: {e.get('detail') or ''}".strip(": ") for e in errs)
        return str(data.get("message") or data.get("detail") or data.get("title") or data)[:300]
    return str(data)[:300]


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


def money(price, factor=1.0):
    """Price * factor, rounded half-up to cents, as a float for JSON (8.075 -> 8.08)."""
    return float((Decimal(str(price)) * Decimal(str(factor))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize(raw):
    images = raw.get("images") or ([raw["image"]] if raw.get("image") else [])
    stock = raw.get("stock", raw.get("quantity"))
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": " ".join((raw.get("title") or "").split()),
        "description": (raw.get("description") or "").strip(),
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "author": raw.get("author") or "",
        "publisher": (raw.get("publisher") or raw.get("brand") or "").strip(),
        "year": raw.get("year") or "",
        "binding": raw.get("binding") or "",
        "pages": raw.get("pages") or "",
        "images": [u for u in images if urlparse(str(u)).scheme in ("http", "https")],
        "stock": int(stock) if stock not in (None, "") else None,
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    elif len(p["sku"]) > 50 or p["sku"] != p["sku"].strip():
        probs.append("id/sku must be at most 50 characters without leading/trailing spaces")
    if not p["price"]:
        probs.append("no price")
    if not p["title"]:
        probs.append("no title")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (mandatory on OTTO)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    elif p["ean"].startswith("2"):
        probs.append("EANs starting with 2 are not allowed on OTTO")
    if not p["images"]:
        probs.append("no image URL (OTTO needs at least one image, min. 500x1000 px)")
    if p["condition"] not in ALLOWED_CONDITIONS:
        probs.append(f"condition '{p['condition']}' is not sold on OTTO (new goods only, see ALLOWED_CONDITIONS)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def description_html(p):
    text = p["description"] or p["title"]
    out = "<p>" + "<br>".join(html.escape(line.strip()) for line in text.splitlines() if line.strip()) + "</p>"
    return out if len(out) <= 2000 else out[:1990].rsplit(" ", 1)[0] + "&hellip;</p>"


def bullets(p):
    out = []
    for label, value in (("Autor", p["author"]), ("Verlag", p["publisher"]), ("Einband", p["binding"]),
                         ("Seiten", p["pages"]), ("Erscheinungsjahr", p["year"])):
        if value:
            out.append(f"{label}: {value}"[:180])
    return out[:5]


def attributes(p):
    attrs = {name: list(values) for name, values in ATTRIBUTES.items()}
    for field, name in FIELD_ATTRIBUTES.items():
        if p.get(field) not in (None, ""):
            attrs[name] = [str(p[field])]
    return [{"name": n, "values": v} for n, v in attrs.items()]


def variation(p, a, brand):
    return {
        "productReference": p["sku"],
        "sku": p["sku"],
        "ean": p["ean"],
        "productDescription": {
            "category": a.category,
            "brandId": brand,
            "productLine": p["title"][:70],
            "description": description_html(p),
            "bulletPoints": bullets(p),
            "attributes": attributes(p),
        },
        "mediaAssets": [{"type": "IMAGE", "location": u} for u in p["images"]],
        "pricing": {"standardPrice": {"amount": money(p["price"], a.price_factor), "currency": "EUR"}, "vat": VAT},
        "compliance": {"productSafety": {"addresses": [dict(GPSR_DISTRIBUTOR, roles=["DISTRIBUTOR"])]}},
    }


def gpsr_missing():
    return [k for k in ("name", "address", "regionCode") if not GPSR_DISTRIBUTOR.get(k)] + \
        ([] if GPSR_DISTRIBUTOR.get("email") or GPSR_DISTRIBUTOR.get("url") else ["email or url"])


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def brand_names(api, refresh=False):
    """name (lower case) -> brand ID of usable OTTO brands, cached for 7 days in otto_brands.json."""
    cache = load_json(BRANDS_CACHE, {})
    if refresh or not cache or time.time() - cache.get("_ts", 0) > 7 * 86400:
        print("Loading the OTTO brand list (cached for 7 days)...")
        cache = {"_ts": time.time(), "brands": {}}
        for b in api.pages("/v5/products/brands", "brands", {"limit": 1000}):
            if b.get("usable") and b.get("name"):
                cache["brands"][b["name"].strip().lower()] = b["id"]
        save_json(BRANDS_CACHE, cache)
    return cache["brands"]


# --------------------------------------------------------------------- commands

def shipping_profiles(api):
    return list(api.pages("/v1/shipping-profiles", "results"))


def cmd_check(api, a):
    api.access_token()
    print(f"Login OK ({api.base}), scopes: {SCOPES}")
    profiles = shipping_profiles(api)
    for sp in profiles:
        print(f"   shipping profile {sp.get('shippingProfileId')}: {sp.get('shippingProfileName')} "
              f"({sp.get('deliveryType')}, {sp.get('defaultProcessingTime')}+{sp.get('transportTime')} days)")
    if not profiles:
        print("   ! no shipping profile: create one in OTTO Partner Connect (needed to sell)")
    if CATEGORY:
        code, data = api.call("GET", "/v5/products/categories", params={"category": CATEGORY})
        print(f"   category '{CATEGORY}': " + ("found" if code == 200 else f"NOT FOUND ({code})"))
    else:
        print("   ! CATEGORY is not set (find it with: categories --find Buch)")
    print("   BRAND_ID: " + (BRAND_ID or "! not set (needed when a publisher isn't an OTTO brand)"))
    missing = gpsr_missing()
    print("   GPSR distributor: " + ("OK" if not missing else "! missing " + ", ".join(missing)))


def cmd_categories(api, a):
    find = (a.find or "").lower()
    n = 0
    for g in api.pages("/v5/products/categories", "categoryGroups", {"limit": 2000}):
        cats = [c for c in g.get("categories") or [] if find in c.lower() or find in str(g.get("categoryGroup")).lower()]
        if cats:
            n += 1
            print(f"{g.get('categoryGroup')}: " + ", ".join(cats[:30]) + (" ..." if len(cats) > 30 else ""))
    print(f"{n} category group(s)" + (f" matching '{a.find}'" if a.find else ""))


def cmd_attributes(api, a):
    code, data = api.call("GET", "/v5/products/categories", params={"category": a.category_name})
    if code != 200:
        sys.exit(f"Category '{a.category_name}' not found ({code}): {err_text(data)}")
    for g in data.get("categoryGroups") or []:
        print(f"Category group {g.get('categoryGroup')}  (title: {g.get('title')})")
        for at in sorted(g.get("attributes") or [], key=lambda x: str(x.get("relevance")) != "HIGH"):
            vals = at.get("allowedValues") or at.get("exampleValues") or []
            print(f"   {at.get('name')} [{at.get('type')}{', multi' if at.get('multiValue') else ''}"
                  f"{', ' + at['unit'] if at.get('unit') else ''}] relevance={at.get('relevance')}"
                  + (f"  values: {', '.join(map(str, vals[:12]))}{' ...' if len(vals) > 12 else ''}" if vals else ""))
        for req in g.get("additionalRequirements") or []:
            print(f"   requirement: {req.get('name')}: {req.get('description')}")


def cmd_brands(api, a):
    names = brand_names(api, refresh=a.refresh)
    find = a.find.lower()
    hits = sorted((n, i) for n, i in names.items() if find in n)
    for n, i in hits[:50]:
        print(f"{i}  {n}")
    print(f"{len(hits)} usable brand(s) matching '{a.find}'")


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_json(STATE_FILE, {})

    new, resend, gone = [], [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and (a.zero_sold_out or a.offline_sold_out) and p["sku"] in state:
            info = state[p["sku"]]
            if (a.zero_sold_out and info.get("stock", 1) > 0) or (a.offline_sold_out and info.get("active", True)):
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

    if new and not a.category:
        sys.exit("CATEGORY is not set: find the books category with `categories --find Buch` and set it "
                 "(or pass --category).")
    if new and gpsr_missing():
        msg = "GPSR_DISTRIBUTOR is incomplete (missing " + ", ".join(gpsr_missing()) + "): OTTO requires it."
        if not a.dry_run:
            sys.exit(msg)
        print("   ! " + msg)

    brands = {}
    if api and new and MATCH_PUBLISHER_BRAND:
        brands = brand_names(api)
    payload, skipped = [], 0
    for p in new:
        brand = brands.get(p["publisher"].lower()) if p["publisher"] else None
        brand = brand or BRAND_ID or (None if api else "BRAND_ID")
        if not brand:
            print(f"   SKIP '{p['title']}': publisher '{p['publisher']}' is not an OTTO brand and BRAND_ID is not set")
            skipped += 1
            continue
        payload.append((p, variation(p, a, brand)))

    print(f"{len(payload)} new product(s)" + (f", {len(resend)} to update" if resend else "")
          + (f", {len(gone)} sold-out" if gone else "") + ".")
    if not (payload or resend or gone):
        return
    PREVIEW_FILE.write_text(json.dumps([v for _, v in payload], ensure_ascii=False, indent=2), encoding="utf-8")
    if a.dry_run:
        print(f"Dry run: products written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(payload) + len(resend) + len(gone)} LIVE product(s) on OTTO. "
                 "Type 'yes': ").strip().lower() != "yes":
            return
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")
    profile = SHIPPING_PROFILE_ID
    if payload and not profile:
        profiles = shipping_profiles(api)
        if not profiles:
            sys.exit("No shipping profile in OTTO Partner Connect: create one first (see `check`).")
        profile = profiles[0]["shippingProfileId"]

    # 1. product data (async update task per batch of 500)
    accepted = []
    for batch in chunks(payload, BATCH):
        code, task = api.call("POST", "/v5/products", body=[v for _, v in batch])
        if code not in (200, 202):
            print(f"   ! product upload rejected: {code} {err_text(task)}")
            continue
        task = api.wait_task(task, a.wait, a.poll)
        if task.get("state") != "done":
            print(f"   Still {task.get('state')} after {a.wait}s: nothing recorded for this batch; "
                  "run push again later (re-sending is safe).")
            continue
        failed = {f.get("variation"): f.get("errors") or [] for f in api.task_part(task, "failed")}
        for p, v in batch:
            if p["sku"] in failed:
                errs = "; ".join(f"{e.get('code')} {e.get('title')}" + (f" ({e.get('path') or e.get('jsonPath')})"
                                 if e.get("path") or e.get("jsonPath") else "") for e in failed[p["sku"]])
                print(f"   ! {p['sku']}: {errs or 'rejected'}")
                continue
            accepted.append((p, v))
        print(f"   product data: {task.get('succeeded', 0)} succeeded, {task.get('unchanged', 0)} unchanged, "
              f"{task.get('failed', 0)} failed")

    # 2. stock and delivery for the accepted products
    if accepted:
        qty = [{"sku": p["sku"], "quantity": p["stock"] if p["stock"] is not None else a.quantity} for p, _ in accepted]
        bad = set()
        for part in chunks(qty, QTY_BATCH):
            code, data = api.call("POST", "/v1/availability/quantities", body=part)
            bad |= report_multi(code, data, "stock", part)
        for part in chunks([{"sku": p["sku"], "shippingProfileId": profile, "processingTime": str(PROCESSING_TIME)}
                            for p, _ in accepted], QTY_BATCH):
            code, data = api.call("POST", "/v1/availability/product-delivery-information", body=part)
            bad |= report_multi(code, data, "delivery", part)
        for (p, v), q in zip(accepted, qty):
            price = v["pricing"]["standardPrice"]["amount"]
            state[p["sku"]] = {"ean": p["ean"], "price": price, "stock": q["quantity"], "active": True,
                               "brand": v["productDescription"]["brandId"], "pushed_at": now()}
            if p["sku"] in bad:
                state[p["sku"]]["stock_pending"] = True
            print(f"   + {p['sku']}: {price:.2f} EUR x{q['quantity']}")
        save_json(STATE_FILE, state)

    # 3. --resend: prices and stock of pushed products
    if resend:
        prices, qty = [], []
        for p in resend:
            info, price = state[p["sku"]], money(p["price"], a.price_factor)
            stock = p["stock"] if p["stock"] is not None else a.quantity
            if price != info.get("price"):
                prices.append({"sku": p["sku"], "standardPrice": {"amount": price, "currency": "EUR"}})
            if stock != info.get("stock") or info.get("stock_pending"):
                qty.append({"sku": p["sku"], "quantity": stock})
            if price == info.get("price") and not (stock != info.get("stock") or info.get("stock_pending")):
                print(f"   = {p['sku']}: unchanged")
        failed_prices = set()
        for part in chunks(prices, BATCH):
            code, task = api.call("POST", "/v5/products/prices", body=part)
            if code not in (200, 202):
                print(f"   ! price update rejected: {code} {err_text(task)}")
                failed_prices |= {x["sku"] for x in part}
                continue
            task = api.wait_task(task, a.wait, a.poll)
            for f in api.task_part(task, "failed"):
                failed_prices.add(f.get("variation"))
                print(f"   ! {f.get('variation')}: price: " + "; ".join(str(e.get("title")) for e in f.get("errors") or []))
        bad = set()
        for part in chunks(qty, QTY_BATCH):
            code, data = api.call("POST", "/v1/availability/quantities", body=part)
            bad |= report_multi(code, data, "stock", part)
        for x in prices:
            if x["sku"] not in failed_prices:
                state[x["sku"]].update(price=x["standardPrice"]["amount"], updated_at=now())
                print(f"   ~ {x['sku']}: {x['standardPrice']['amount']:.2f} EUR")
        for x in qty:
            if x["sku"] not in bad:
                state[x["sku"]].update(stock=x["quantity"], updated_at=now())
                state[x["sku"]].pop("stock_pending", None)
                print(f"   ~ {x['sku']}: stock {x['quantity']}")
        save_json(STATE_FILE, state)

    # 4. sold out: stock 0 and/or offline
    if gone:
        if a.zero_sold_out:
            part = [{"sku": p["sku"], "quantity": 0} for p in gone if state[p["sku"]].get("stock", 1) > 0]
            bad = set()
            for c in chunks(part, QTY_BATCH):
                code, data = api.call("POST", "/v1/availability/quantities", body=c)
                bad |= report_multi(code, data, "stock", c)
            for x in part:
                if x["sku"] not in bad:
                    state[x["sku"]].update(stock=0, updated_at=now())
                    print(f"   - {x['sku']}: sold out, stock 0")
        if a.offline_sold_out:
            part = [{"sku": p["sku"], "active": False} for p in gone if state[p["sku"]].get("active", True)]
            for c in chunks(part, BATCH):
                code, task = api.call("POST", "/v5/products/active-status", body={"status": c})
                if code not in (200, 202):
                    print(f"   ! active-status update rejected: {code} {err_text(task)}")
                    continue
                task = api.wait_task(task, a.wait, a.poll)
                failed = {f.get("variation") for f in api.task_part(task, "failed")}
                for x in c:
                    if x["sku"] in failed:
                        print(f"   ! {x['sku']}: could not take it offline")
                        continue
                    state[x["sku"]].update(active=False, updated_at=now())
                    print(f"   - {x['sku']}: sold out, taken offline")
        save_json(STATE_FILE, state)
    print("Done. Run `status` to see whether the products are ONLINE.")


def report_multi(code, data, what, part):
    """Availability API: 200/202 = all OK, 207 = some failed (results = accepted). Returns failed SKUs."""
    skus = {x["sku"] for x in part}
    if code in (200, 202):
        return set()
    if code == 207:
        for e in data.get("errors") or []:
            print(f"   ! {what}: {e.get('title')}: {e.get('detail')}")
        return skus - {r.get("sku") for r in data.get("results") or []}
    print(f"   ! {what} update rejected: {code} {err_text(data)}")
    return skus


def cmd_status(api, a):
    state = load_json(STATE_FILE, {})
    if not state:
        print("No products yet.")
        return
    seen = {}
    for st in api.pages("/v5/products/marketplace-status", "marketPlaceStatus", {"limit": 1000}):
        seen[st.get("sku")] = st
    for sku in sorted(state):
        st = seen.get(sku)
        info = state[sku]
        if not st:
            print(f"{sku}: not found on OTTO yet")
            continue
        line = f"{sku}: {st.get('status')}  price={info.get('price')} EUR  stock={info.get('stock')}"
        if not info.get("active", True):
            line += "  (offline)"
        shop = next((l.get("href") for l in st.get("links") or [] if l.get("rel") == "shop"), None)
        if shop:
            line += f"  {shop}"
        for e in st.get("errors") or []:
            line += f"\n      ! {e.get('code')}: {e.get('title')}"
        for e in st.get("information") or []:
            line += f"\n      i {e.get('code')}: {e.get('title')}"
        print(line)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to OTTO Market (Products API v5)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the credentials, list shipping profiles, check the settings")
    s = sub.add_parser("categories", help="list OTTO categories")
    s.add_argument("--find", default="", help="only categories containing this text")
    s = sub.add_parser("attributes", help="attributes of one category")
    s.add_argument("category_name")
    s = sub.add_parser("brands", help="find usable OTTO brand IDs")
    s.add_argument("--find", required=True, help="text in the brand name")
    s.add_argument("--refresh", action="store_true", help="reload the brand list")
    p = sub.add_parser("push", help="create products from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--category", default=CATEGORY, help="OTTO category (default: CATEGORY setting)")
    p.add_argument("--quantity", type=int, default=1, help="stock per product (per-item 'stock'/'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--resend", action="store_true", help="update price/stock of already pushed products")
    p.add_argument("--zero-sold-out", action="store_true", help="set stock 0 for pushed products now sold out")
    p.add_argument("--offline-sold-out", action="store_true", help="take pushed products now sold out offline")
    p.add_argument("--dry-run", action="store_true", help="write otto_products.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new products this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--wait", type=int, default=600, help="max seconds to wait for OTTO to process an upload")
    p.add_argument("--poll", type=int, default=10, help="seconds between update task checks")
    sub.add_parser("status", help="marketplace status (ONLINE, PENDING, REJECTED ...) and errors")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run and not os.environ.get("OTTO_CLIENT_ID"):
        return cmd_push(None, a)
    api = Otto()
    try:
        {"check": cmd_check, "categories": cmd_categories, "attributes": cmd_attributes, "brands": cmd_brands,
         "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except ApiError as e:
        sys.exit(f"OTTO API error: {e}")


if __name__ == "__main__":
    main()
