#!/usr/bin/env python3
"""
Push books to CDON (Nordic marketplace: cdon.se, cdon.dk, cdon.fi, cdon.no)
through CDON's merchant API (the new platform; the old CDON Connect API was
closed in 2026). Docs: https://docs.cdon.com (cdon-api-documentation.readme.io).

How it works:
  * Each book becomes a CDON article: SKU, ISBN as GTIN, title and
    description, publisher as brand, images, condition NEW, and per market a
    price (converted from EUR, including VAT), VAT rate and shipping time.
    POST /v2/articles/bulk, up to 100 per call.
  * CDON validates the request at once (immediate "failed" list), then
    processes the batch in the background. The script polls
    /v1/statuses/batch and shows per book and market whether it was accepted
    or why not (invalid_category, sku_already_exists, ...).
  * Updates go through PUT /v2/articles/bulk actions: update_article_price,
    update_article_quantity, update_article (pause / reactivate),
    delete_article. Only what changed is sent.

ONE-TIME SETUP (see docs/cdon_push.md)
  CDON Merchant Center -> API: copy the Merchant ID and the API token, then
  (PowerShell), or as Bitwarden secrets:
      $env:CDON_MERCHANT_ID = "..."
      $env:CDON_API_TOKEN = "..."
  Pick the category: python cdon_push.py categories --find book

USAGE
    python cdon_push.py check                     # login, categories, shipping configurations, settings
    python cdon_push.py categories --find book    # category IDs (set CATEGORY)
    python cdon_push.py push --dry-run            # write cdon_articles.json, send nothing
    python cdon_push.py push --limit 1            # one real article
    python cdon_push.py push --resend             # update prices / stock that changed
    python cdon_push.py status                    # per book and market: ok / failed (+ reason) / pending
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

MARKETS = ["SE", "DK", "FI"]            # any of SE, DK, FI, NO (NO = outside the EU: customs)
# EUR -> market currency. Source prices are EUR. Update these regularly.
EUR_RATE = {"SE": "11.0", "DK": "7.46", "FI": "1", "NO": "11.7"}
# VAT rate for books per market, in %. Verify with your accountant before selling.
VAT_RATE = {"SE": 6, "DK": 25, "FI": 14, "NO": 0}
# CDON accepts texts only in sv-SE, en-US, da-DK, fi-FI, nb-NO. The scraped texts are
# Spanish; they are sent under these language tags (CDON shows the matching one per site).
TEXT_LANGUAGES = ["en-US"]
CATEGORY = ""                 # CDON category ID (find it: `categories --find book`); "" = let CDON decide
SHIPPING_DAYS = (3, 7)        # min / max days to the customer (0..21 / 1..21)
DELIVERY_TYPE = None          # None, or "mailbox" / "service_point" / "home_delivery"
SHIPPED_FROM = "EU"
# CDON shipping configuration IDs per market (GET /v1/shipping-configurations; see `check`).
SHIPPING_CONFIGURATIONS = {}  # e.g. {"SE": ["<id>"], "DK": ["<id>"], "FI": ["<id>"]}
# GPSR manufacturer (the publisher). Sent only when name and full address are filled in.
MANUFACTURER = {"name": "", "address": {"street_address": "", "city": "", "postal_code": "", "country": ""},
                "website": "", "email": ""}
ALLOWED_CONDITIONS = {"NewCondition"}
DEFAULT_STOCK = 1             # stock when the product has no "stock"/"quantity" field
WAIT_SECONDS = 90             # how long `push` waits for CDON to process a batch
# ======================================================================

API_URL = "https://merchants-api.cdon.com/api"
SANDBOX_URL = "https://merchants-api.sandbox.cdon.com/api"
CURRENCY = {"SE": "SEK", "DK": "DKK", "FI": "EUR", "NO": "NOK"}
BATCH = 100
STATE_FILE = Path("cdon_posted.json")
PREVIEW_FILE = Path("cdon_articles.json")


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------- API

class Cdon:
    def __init__(self):
        mid, token = os.environ.get("CDON_MERCHANT_ID"), os.environ.get("CDON_API_TOKEN")
        missing = [k for k, v in (("CDON_MERCHANT_ID", mid), ("CDON_API_TOKEN", token)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.auth = base64.b64encode(f"{mid}:{token}".encode()).decode()
        self.base = (os.environ.get("CDON_API_URL") or API_URL).rstrip("/")

    def call(self, method, path, body=None, params=None, retries=5):
        """Returns (http status, parsed JSON or None)."""
        for attempt in range(retries):
            r = requests.request(method, f"{self.base}/{path.lstrip('/')}", params=params,
                                 data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Authorization": f"Basic {self.auth}", "Accept": "application/json",
                                          "Content-Type": "application/json"}, timeout=120)
            if r.status_code == 401:
                sys.exit("CDON refused the login (401). Check CDON_MERCHANT_ID and CDON_API_TOKEN "
                         "(Merchant Center -> API).")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries - 1:
                    break
                reset = r.headers.get("X-RateLimit-Burst-Reset")
                pause = 2 ** attempt
                if r.status_code == 429 and reset and reset.isdigit():
                    pause = min(60, max(1, int(reset) - int(time.time())))
                print(f"   (CDON returned {r.status_code}, retrying in {pause}s)")
                time.sleep(pause)
                continue
            try:
                return r.status_code, (r.json() if r.content else None)
            except ValueError:
                raise ApiError(f"{path}: HTTP {r.status_code}, not JSON: {r.text[:200]}")
        raise ApiError(f"{path}: gave up after {retries} tries (HTTP {r.status_code})")


def err_text(data):
    """CDON error bodies: message + errors (dict or list) / details / detail."""
    if not isinstance(data, dict):
        return str(data)[:300]
    parts = [str(data.get("message") or data.get("detail") or "")]
    errs = data.get("errors") or data.get("details")
    if isinstance(errs, dict):
        parts += [f"{k}: {v}" for k, v in errs.items()]
    elif isinstance(errs, list):
        parts += [item_errors(errs)]
    return "; ".join(p for p in parts if p) or json.dumps(data)[:300]


def item_errors(errors):
    out = []
    for e in errors or []:
        if isinstance(e, dict):
            field = e.get("field")
            field = ".".join(map(str, field)) if isinstance(field, list) else field
            out.append(f"{field}: {e.get('message')}" if field else str(e.get("message")))
        else:
            out.append(str(e))
    return "; ".join(out)


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


def market_prices(gross_eur, factor):
    """{market: amount incl. VAT} in the market currency. SEK/DKK/NOK whole units, EUR cents."""
    try:
        base = Decimal(str(gross_eur)) * Decimal(str(factor))
    except InvalidOperation:
        return None
    if base <= 0:
        return None
    out = {}
    for m in MARKETS:
        v = base * Decimal(EUR_RATE[m])
        q = Decimal("0.01") if CURRENCY[m] == "EUR" else Decimal("1")
        v = v.quantize(q, rounding=ROUND_HALF_UP)
        out[m] = int(v) if q == 1 else float(v)
    return out


def price_body(prices):
    return [{"market": m, "value": {"amount_including_vat": a, "currency": CURRENCY[m], "vat_rate": VAT_RATE[m]}}
            for m, a in prices.items()]


def clean(text, limit):
    t = re.sub(r"<[^>]+>", " ", str(text or ""))           # CDON: no HTML
    lines = [" ".join(line.split()) for line in t.replace("\r", "\n").split("\n")]
    t = "\n".join(line for line in lines if line).strip()
    if len(t) > limit:
        t = t[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return t


def manufacturer():
    m = MANUFACTURER or {}
    addr = m.get("address") or {}
    if not m.get("name") or not all(addr.get(k) for k in ("street_address", "city", "postal_code", "country")):
        return None
    out = {"name": m["name"], "address": {k: addr[k] for k in ("street_address", "city", "postal_code", "country")}}
    for k in ("website", "email"):
        if m.get(k):
            out[k] = m[k]
    if m.get("responsible_person"):
        out["responsible_person"] = m["responsible_person"]
    return out


def normalize(raw, a):
    stock = raw.get("stock", raw.get("quantity"))
    images = raw.get("images") or ([raw["image"]] if raw.get("image") else [])
    try:
        qty = int(stock) if stock not in (None, "") else a.quantity
    except (TypeError, ValueError):
        qty = -1
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": clean(raw.get("title"), 150).replace("\n", " "),
        "description": clean(raw.get("description"), 4096),
        "brand": (raw.get("brand") or raw.get("publisher") or "").strip()[:50],
        "prices": market_prices(raw.get("price_eur", raw.get("price")), a.price_factor),
        "gtin": to_gtin(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "quantity": qty,
        "sold_out": bool(raw.get("sold_out", False)),
        "images": [u for u in images if isinstance(u, str) and u.startswith(("http://", "https://"))
                   and len(u) <= 1500][:10],
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    elif len(p["sku"]) > 64:
        probs.append("id/sku longer than 64 characters")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN")
    elif not p["gtin"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if len(p["title"]) < 5:
        probs.append("title shorter than 5 characters (CDON minimum)")
    if not p["prices"]:
        probs.append("no valid price")
    if not p["images"]:
        probs.append("no image URL (CDON needs main_image)")
    if p["condition"] not in ALLOWED_CONDITIONS:
        probs.append(f"condition '{p['condition']}' not offered (see ALLOWED_CONDITIONS)")
    if not 0 <= p["quantity"] <= 500000:
        probs.append("stock must be a whole number 0..500000")
    return probs


def texts(value):
    return [{"language": lang, "value": value} for lang in TEXT_LANGUAGES]


def base_body(p, status):
    """The fields update_article needs (and create shares)."""
    desc = p["description"]
    if len(desc) < 10:                                  # CDON: description 10..4096 characters
        desc = " - ".join(x for x in (p["title"], p["brand"], p["gtin"]) if x)
    body = {
        "status": status,
        "main_image": p["images"][0],
        "markets": list(MARKETS),
        "shipping_time": [{"market": m, "min": SHIPPING_DAYS[0], "max": SHIPPING_DAYS[1]} for m in MARKETS],
        "title": texts(p["title"]),
        "description": texts(desc),
    }
    if p["images"][1:]:
        body["images"] = p["images"][1:]
    if p["brand"]:
        body["brand"] = p["brand"]
    if CATEGORY:
        body["category"] = str(CATEGORY)
    body["gtin"] = p["gtin"]
    body["classifications"] = [{"name": "CONDITION", "value": "NEW"}]
    body["shipped_from"] = SHIPPED_FROM
    if DELIVERY_TYPE:
        body["delivery_type"] = [{"market": m, "value": DELIVERY_TYPE} for m in MARKETS]
    man = manufacturer()
    if man:
        body["manufacturer"] = man
    if SHIPPING_CONFIGURATIONS:
        body["shipping_configurations"] = [{"market": m, "configuration_ids": SHIPPING_CONFIGURATIONS[m]}
                                           for m in MARKETS if SHIPPING_CONFIGURATIONS.get(m)]
    return body


def new_article(p):
    art = {"sku": p["sku"], "quantity": p["quantity"], "price": price_body(p["prices"])}
    art.update(base_body(p, "for sale"))
    return art


def settings_problems():
    probs = []
    bad = [m for m in MARKETS if m not in CURRENCY]
    if bad or not MARKETS:
        probs.append(f"MARKETS must be a non-empty list of SE, DK, FI, NO (got {MARKETS})")
    for m in MARKETS:
        if m in CURRENCY and (m not in EUR_RATE or m not in VAT_RATE):
            probs.append(f"EUR_RATE and VAT_RATE need an entry for {m}")
    if not TEXT_LANGUAGES or any(l not in ("sv-SE", "en-US", "da-DK", "fi-FI", "nb-NO") for l in TEXT_LANGUAGES):
        probs.append("TEXT_LANGUAGES must use sv-SE, en-US, da-DK, fi-FI or nb-NO")
    if not (0 <= SHIPPING_DAYS[0] <= 21 and 1 <= SHIPPING_DAYS[1] <= 21 and SHIPPING_DAYS[0] <= SHIPPING_DAYS[1]):
        probs.append("SHIPPING_DAYS must be (min 0..21, max 1..21), min <= max")
    if CATEGORY and not re.fullmatch(r"\d{1,10}", str(CATEGORY)):
        probs.append("CATEGORY must be a CDON category ID (digits)")
    return probs


# ---------------------------------------------------------------- status polling

def summarize(markets):
    """Overall status from [{market, status, error_code}]: ok / failed / pending, plus reasons."""
    states = {m.get("market"): (m.get("status") or "pending").lower() for m in markets or []}
    reasons = [f"{m.get('market')}: {m.get('error_code')}" for m in markets or [] if m.get("error_code")]
    if states and all(s == "ok" for s in states.values()):
        return "ok", states, reasons
    if any(s == "failed" for s in states.values()):
        return "failed", states, reasons
    return "pending", states, reasons


def apply_statuses(statuses, state, label=""):
    """Write CDON statuses into the state; print rejections. Returns number still pending."""
    pending = 0
    for st in statuses:
        sku = st.get("sku")
        if sku not in state:
            continue
        overall, states, reasons = summarize(st.get("markets"))
        entry = state[sku]
        action = st.get("action") or "create_article"
        if overall == "failed" and any("sku_already_exists" in r for r in reasons) and action.startswith("create"):
            overall, reasons = "ok", []
            print(f"   {sku}: already exists at CDON - taken over (updates go to the existing article)")
        entry["markets"] = states
        entry["errors"] = reasons
        if action.startswith("create"):
            entry["status"] = overall
        entry["checked_at"] = now_iso()
        if overall == "failed":
            print(f"   ! {sku}{label}: CDON rejected {action}: " + "; ".join(reasons or ["no reason given"]))
        elif overall == "pending":
            pending += 1
    return pending


def wait_for(api, batch_ids, state, wait):
    if not batch_ids:
        return 0
    deadline, pending = time.time() + wait, None
    while True:
        statuses = []
        code, data = api.call("POST", "v1/statuses/batch", {"batch_ids": batch_ids})
        if code != 200:
            print(f"   (could not read batch status: {code} {err_text(data)})")
            return -1
        for b in (data or {}).get("batches") or []:
            statuses += b.get("statuses") or []
        pending = sum(1 for s in statuses if summarize(s.get("markets"))[0] == "pending")
        if pending == 0 or time.time() >= deadline:
            apply_statuses(statuses, state)
            return pending
        time.sleep(5)


def bulk(api, method, items, key, state, label):
    """Send items in batches of 100. Returns (batch_ids, succeeded skus, failed skus)."""
    batch_ids, ok, failed = [], set(), set()
    for i in range(0, len(items), BATCH):
        part = items[i:i + BATCH]
        code, data = api.call(method, "v2/articles/bulk", {key: part})
        skus = [x["sku"] for x in part]
        if code not in (200, 201, 202):
            print(f"   ! {label} batch of {len(part)} refused ({code}): {err_text(data)}")
            failed.update(skus)
            continue
        data = data or {}
        if data.get("batch_id"):
            batch_ids.append(data["batch_id"])
        for s in data.get("success") or []:
            ok.add(s.get("sku"))
        for f in data.get("failed") or []:
            failed.add(f.get("sku"))
            print(f"   ! {f.get('sku')}: {label} rejected: {item_errors(f.get('errors')) or 'no reason given'}")
    return batch_ids, ok, failed


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    print(f"Markets {', '.join(MARKETS)}; texts as {', '.join(TEXT_LANGUAGES)}; category {CATEGORY or '(none)'}; "
          f"shipping {SHIPPING_DAYS[0]}-{SHIPPING_DAYS[1]} days")
    for m in MARKETS:
        print(f"   {m}: 1 EUR = {EUR_RATE.get(m)} {CURRENCY.get(m)}, VAT {VAT_RATE.get(m)} %")
    for p in settings_problems():
        print("! " + p)
    code, data = api.call("GET", f"v1/categories/{MARKETS[0]}/en-US/")
    if code != 200:
        sys.exit(f"Category list failed ({code}): {err_text(data)}")
    print(f"Login OK ({api.base}): {len(data or [])} categories on {MARKETS[0]}")
    if CATEGORY:
        hit = [c for c in data or [] if str(c.get("id")) == str(CATEGORY)]
        print(f"   CATEGORY {CATEGORY}: " + (hit[0].get("path") or hit[0].get("name") if hit else "NOT FOUND"))
    code, data = api.call("GET", "v1/shipping-configurations")
    if code == 200:
        confs = data if isinstance(data, list) else (data or {}).get("shipping_configurations") or (data or {}).get("items") or []
        print(f"Shipping configurations: {len(confs)}")
        for c in confs[:20]:
            print(f"   {c.get('id')}: {c.get('name') or ''} {c.get('market') or ''}")
    else:
        print(f"   (shipping configurations not readable: {code})")
    if not manufacturer():
        print("! MANUFACTURER (GPSR: publisher name + address) is not filled in; it is not sent.")


def cmd_categories(api, a):
    code, data = api.call("GET", f"v1/categories/{a.market}/{a.language}/")
    if code != 200:
        sys.exit(f"Category list failed ({code}): {err_text(data)}")
    find = (a.find or "").lower()
    rows = [c for c in data or [] if not find or find in f"{c.get('name')} {c.get('path')} "
                                                          f"{c.get('search_friendly_name')}".lower()]
    for c in rows[:200]:
        print(f"{c.get('id'):>8}  {c.get('path') or c.get('name')}")
    print(f"{len(rows)} categor{'y' if len(rows) == 1 else 'ies'}" + (f" matching '{a.find}'" if find else ""))


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    probs = settings_problems()
    if probs:
        sys.exit("Settings problems:\n  - " + "\n  - ".join(probs))
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_json(STATE_FILE)

    new, updates, seen_sku, seen_gtin = [], [], set(), set()
    for i, r in enumerate(raw, 1):
        p = normalize(r, a)
        probs = problems_of(p)
        if p["sku"] in seen_sku:
            probs.append(f"id/sku {p['sku']} appears twice")
        if p["gtin"] and p["gtin"] in seen_gtin:
            probs.append(f"ISBN {p['gtin']} is already used by another book")
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        seen_sku.add(p["sku"])
        seen_gtin.add(p["gtin"])
        known = state.get(p["sku"])
        if known and known.get("status") in ("ok", "pending"):
            updates.append(p)
            continue
        if p["sold_out"]:
            print(f"[{i}] SKIP '{p['title']}': sold out")
            continue
        new.append(p)
    if a.limit:
        new = new[:a.limit]

    actions = []
    for p in updates:
        e = state[p["sku"]]
        if e.get("status") != "ok":
            continue
        if p["sold_out"]:
            if a.delete_sold_out:
                actions.append({"sku": p["sku"], "action": "delete_article"})
            elif a.pause_sold_out and e.get("sale_status") != "paused":
                actions.append({"sku": p["sku"], "action": "update_article", "body": base_body(p, "paused")})
            elif a.zero_sold_out and e.get("quantity") != 0:
                actions.append({"sku": p["sku"], "action": "update_article_quantity", "body": {"quantity": 0}})
            continue
        if not a.resend:
            continue
        if e.get("sale_status") == "paused":
            actions.append({"sku": p["sku"], "action": "update_article", "body": base_body(p, "for sale")})
        if e.get("prices") != p["prices"]:
            actions.append({"sku": p["sku"], "action": "update_article_price", "body": {"price": price_body(p["prices"])}})
        if e.get("quantity") != p["quantity"]:
            actions.append({"sku": p["sku"], "action": "update_article_quantity", "body": {"quantity": p["quantity"]}})

    articles = [new_article(p) for p in new]
    save_json(PREVIEW_FILE, articles)
    print(f"{len(articles)} new article(s), {len(actions)} update action(s). Articles written to {PREVIEW_FILE}.")
    if a.dry_run:
        print("Dry run: nothing sent.")
        return
    if not articles and not actions:
        print("Nothing to send.")
        return
    if not a.yes and input(f"Send {len(articles)} new article(s) and {len(actions)} update(s) to CDON? [y/N] "
                           ).strip().lower() != "y":
        print("Cancelled.")
        return

    by_sku = {p["sku"]: p for p in new + updates}
    batch_ids = []
    if articles:
        ids, ok, failed = bulk(api, "POST", articles, "articles", state, "create")
        batch_ids += ids
        now = now_iso()
        for sku in ok:
            p = by_sku[sku]
            state[sku] = {"gtin": p["gtin"], "status": "pending", "sale_status": "for sale",
                          "prices": p["prices"], "quantity": p["quantity"], "markets": {}, "errors": [],
                          "batch_id": ids[-1] if ids else None, "created_at": now, "updated_at": now}
        save_json(STATE_FILE, state)
        print(f"Created: {len(ok)} accepted for processing, {len(failed)} rejected.")
    if actions:
        ids, ok, failed = bulk(api, "PUT", actions, "actions", state, "update")
        batch_ids += ids
        now = now_iso()
        for act in actions:
            sku = act["sku"]
            if sku not in ok:
                continue
            e, p = state[sku], by_sku[sku]
            if act["action"] == "update_article_price":
                e["prices"] = p["prices"]
            elif act["action"] == "update_article_quantity":
                e["quantity"] = act["body"]["quantity"]
            elif act["action"] == "update_article":
                e["sale_status"] = act["body"]["status"]
            elif act["action"] == "delete_article":
                state.pop(sku)
                print(f"   - {sku}: deleted (sold out)")
                continue
            e["updated_at"] = now
        save_json(STATE_FILE, state)
        print(f"Updates: {len(ok)} accepted, {len(failed)} rejected.")

    pending = wait_for(api, batch_ids, state, a.wait)
    save_json(STATE_FILE, state)
    done = sum(1 for e in state.values() if e.get("status") == "ok")
    print(f"CDON status: {done} article(s) ok" + (f", {pending} still processing - run `status` later." if pending > 0 else "."))


def cmd_status(api, a):
    state = load_json(STATE_FILE)
    if not state:
        print("Nothing pushed yet.")
        return
    skus = sorted(state)
    statuses = []
    for i in range(0, len(skus), BATCH):
        code, data = api.call("POST", "v1/statuses/sku", {"skus": skus[i:i + BATCH]})
        if code != 200:
            sys.exit(f"Status request failed ({code}): {err_text(data)}")
        for b in (data or {}).get("batches") or [data or {}]:
            statuses += b.get("statuses") or []
    latest = {}
    for s in statuses:                       # keep the create status per SKU (updates don't change listing state)
        if (s.get("action") or "create_article").startswith("create") or s.get("sku") not in latest:
            latest[s.get("sku")] = s
    apply_statuses(list(latest.values()), state)
    save_json(STATE_FILE, state)
    for sku in skus:
        e = state[sku]
        mk = "  ".join(f"{m}:{s}" for m, s in sorted((e.get("markets") or {}).items()))
        extra = ("  (" + "; ".join(e["errors"]) + ")") if e.get("errors") else ""
        print(f"   {sku}: {e.get('status', '?'):<8} {e.get('sale_status', '')}  {mk}{extra}")
    counts = {}
    for e in state.values():
        counts[e.get("status")] = counts.get(e.get("status"), 0) + 1
    print(", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda x: str(x[0]))))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to CDON (merchant API)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="login, categories, shipping configurations, settings")
    c = sub.add_parser("categories", help="list CDON categories")
    c.add_argument("--find", help="text to look for, e.g. book")
    c.add_argument("--market", default="SE", choices=sorted(CURRENCY))
    c.add_argument("--language", default="en-US", choices=["sv-SE", "en-US", "da-DK", "fi-FI", "nb-NO"])
    p = sub.add_parser("push", help="create new articles, update changed ones")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--dry-run", action="store_true", help="write cdon_articles.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new articles this run (0 = all)")
    p.add_argument("--resend", action="store_true", help="update prices / stock that changed; reactivate paused")
    p.add_argument("--zero-sold-out", action="store_true", help="stock 0 for pushed books that are now sold out")
    p.add_argument("--pause-sold-out", action="store_true", help="status 'paused' for pushed sold-out books")
    p.add_argument("--delete-sold-out", action="store_true", help="delete pushed books that are now sold out")
    p.add_argument("--quantity", type=int, default=DEFAULT_STOCK, help="stock when the book has none")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--wait", type=int, default=WAIT_SECONDS, help=f"seconds to wait for processing ({WAIT_SECONDS})")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="per book and market: ok / failed / pending")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run:
        return cmd_push(None, a)
    api = Cdon()
    try:
        {"check": cmd_check, "categories": cmd_categories, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except (ApiError, requests.RequestException) as e:
        sys.exit(f"CDON API error: {e}")


if __name__ == "__main__":
    main()
