#!/usr/bin/env python3
"""
Push products from a JSON file to eBay EU marketplaces via the official
eBay Sell Inventory API (create inventory item -> create offer -> publish).

ONE-TIME SETUP
  1. Register at https://developer.ebay.com, create a Production keyset.
  2. In the developer portal: "User Tokens" -> sign in as your seller account
     -> get a *refresh token* with scopes sell.inventory and sell.account.
  3. In eBay Seller Hub: opt in to Business Policies (shipping, returns,
     payment) and add at least one inventory location.
  4. Set environment variables (PowerShell):
        $env:EBAY_CLIENT_ID     = "..."
        $env:EBAY_CLIENT_SECRET = "..."
        $env:EBAY_REFRESH_TOKEN = "v^1.1#..."
     Optional overrides: EBAY_FULFILLMENT_POLICY_ID, EBAY_PAYMENT_POLICY_ID,
     EBAY_RETURN_POLICY_ID, EBAY_LOCATION_KEY (otherwise your first one is used).

USAGE
    pip install requests
    python ebay_push.py C:/programming/scraper/books.json --dry-run
    python ebay_push.py C:/programming/scraper/books.json --market EBAY_ES --limit 2
    python ebay_push.py books.json --market EBAY_DE --category-id 261186 --quantity 3
    python ebay_push.py books.json --markets EBAY_ES,EBAY_DE,EBAY_FR   # several sites in one run
    python ebay_push.py books.json --markets eur                       # all euro sites (or: all)
    python ebay_push.py books.json --sandbox          # test against eBay's sandbox

Markets: EBAY_ES, EBAY_DE, EBAY_FR, EBAY_IT, EBAY_NL, EBAY_AT, EBAY_BE,
         EBAY_IE, EBAY_GB, EBAY_CH, EBAY_PL

Notes
  * Each site needs its own shipping, payment and return business policy
    (Seller Hub). A site without them is skipped with a message.
  * Prices are EUR in the JSON; GBP / CHF / PLN sites get them converted (EUR_RATE).
  * Images must be publicly reachable URLs; the scraped "images" URLs are used as-is.
  * Books usually require an Author aspect. Add an "author" key to an item (and
    optionally "language") or eBay will reject the publish with a clear message.
  * Without --no-publish, offers go LIVE (fees may apply). You'll be asked to confirm.
"""
import argparse
import base64
import html
import json
import os
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

STATE_FILE = Path("ebay_posted.json")

# marketplace -> (taxonomy tree id, Content-Language, currency)
MARKETS = {
    "EBAY_ES": ("186", "es-ES", "EUR"),
    "EBAY_DE": ("77", "de-DE", "EUR"),
    "EBAY_FR": ("71", "fr-FR", "EUR"),
    "EBAY_IT": ("101", "it-IT", "EUR"),
    "EBAY_NL": ("146", "nl-NL", "EUR"),
    "EBAY_AT": ("16", "de-AT", "EUR"),
    "EBAY_BE": ("23", "fr-BE", "EUR"),
    "EBAY_IE": ("205", "en-IE", "EUR"),
    "EBAY_GB": ("3", "en-GB", "GBP"),
    "EBAY_CH": ("193", "de-CH", "CHF"),
    "EBAY_PL": ("212", "pl-PL", "PLN"),
}
EURO_MARKETS = [m for m, v in MARKETS.items() if v[2] == "EUR"]
# Source prices are EUR. Rates for the non-euro sites; update them regularly.
EUR_RATE = {"EUR": "1", "GBP": "0.85", "CHF": "0.94", "PLN": "4.25"}
# Language of the books, used for the "Language" aspect, in each site's own words.
BOOK_LANGUAGE = "es"
LANGUAGE_WORDS = {
    "es": {"es": "Español", "de": "Spanisch", "fr": "Espagnol", "it": "Spagnolo", "nl": "Spaans",
           "pl": "Hiszpański", "en": "Spanish"},
}
SCOPES = "https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account"
# Schema.org condition (scraped JSON) -> eBay inventory condition enum.
# An eBay enum value (e.g. "USED_GOOD") in the JSON is passed through as-is.
CONDITION_MAP = {
    "NewCondition": "NEW",
    "UsedCondition": "USED_VERY_GOOD",
    "RefurbishedCondition": "SELLER_REFURBISHED",
    "DamagedCondition": "FOR_PARTS_OR_NOT_WORKING",
}


def book_language(site_lang):
    """The books' language as the site writes it (e.g. 'Spanisch' on ebay.de)."""
    words = LANGUAGE_WORDS.get(BOOK_LANGUAGE, {})
    return words.get(site_lang[:2]) or words.get("en") or BOOK_LANGUAGE


def market_price(price_eur, currency):
    """EUR source price -> the site's currency, 2 decimals, half up."""
    v = Decimal(str(price_eur)) * Decimal(EUR_RATE[currency])
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Ebay:
    def __init__(self, sandbox, market):
        self.host = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
        self.market = market
        self.tree, self.lang, self.currency = MARKETS[market]
        self.token = None
        self.app_token = None

    def _basic(self):
        cid, sec = os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]
        return "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()

    def _token_call(self, data):
        r = requests.post(
            f"{self.host}/identity/v1/oauth2/token",
            headers={"Authorization": self._basic(), "Content-Type": "application/x-www-form-urlencoded"},
            data=data, timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"token error {r.status_code}: {r.text}")
        return r.json()["access_token"]

    def login(self):
        self.token = self._token_call({
            "grant_type": "refresh_token",
            "refresh_token": os.environ["EBAY_REFRESH_TOKEN"],
            "scope": SCOPES,
        })
        self.app_token = self._token_call({
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        })

    def call(self, method, path, body=None, params=None, app=False, ok=(200, 201, 204)):
        headers = {
            "Authorization": f"Bearer {self.app_token if app else self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Language": self.lang,
            "X-EBAY-C-MARKETPLACE-ID": self.market,
        }
        r = requests.request(method, self.host + path, headers=headers, json=body, params=params, timeout=60)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.content else {}

    # ---- lookups -------------------------------------------------------
    def first_policy(self, kind, list_key, id_key, env):
        if os.environ.get(env):
            return os.environ[env]
        data = self.call("GET", f"/sell/account/v1/{kind}", params={"marketplace_id": self.market})
        items = data.get(list_key) or []
        return items[0][id_key] if items else None

    def location_key(self):
        if os.environ.get("EBAY_LOCATION_KEY"):
            return os.environ["EBAY_LOCATION_KEY"]
        locs = self.call("GET", "/sell/inventory/v1/location").get("locations") or []
        return locs[0]["merchantLocationKey"] if locs else None

    def suggest_category(self, query):
        data = self.call(
            "GET", f"/commerce/taxonomy/v1/category_tree/{self.tree}/get_category_suggestions",
            params={"q": query[:350]}, app=True,
        )
        sug = data.get("categorySuggestions") or []
        return (sug[0]["category"]["categoryId"], sug[0]["category"]["categoryName"]) if sug else (None, None)


def normalize(raw):
    """Accept the scraper's JSON (Correos Market) or an already simple format."""
    desc_html = raw.get("description_html")
    desc = raw.get("description", "")
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:50],
        "title": (raw.get("title") or "")[:80],  # eBay title limit
        "description_html": desc_html or "<br>".join(html.escape(l) for l in desc.splitlines()),
        "price": raw.get("price_eur", raw.get("price")),
        "brand": raw.get("brand"),
        "isbn": raw.get("isbn"),
        "author": raw.get("author"),
        "language": raw.get("language"),
        "images": (raw.get("images") or raw.get("image_urls") or raw.get("photo_urls")
                   or ([raw["image"]] if raw.get("image") else [])),
        "condition": CONDITION_MAP.get(raw.get("condition"), raw.get("condition") or "NEW"),
        "sold_out": raw.get("sold_out", False),
    }


def validate(p):
    problems = []
    if not p["sku"]:
        problems.append("no id/sku")
    if not p["title"]:
        problems.append("no title")
    if not p["price"]:
        problems.append("no price")
    if not p["images"]:
        problems.append("no image URLs")
    if p["sold_out"]:
        problems.append("sold out at source")
    return problems


def inventory_item_body(p, eb, default_lang):
    aspects = {"Book Title": [p["title"]]}
    if p["isbn"]:
        aspects["ISBN"] = [p["isbn"].replace("-", "")]
    if p["author"]:
        aspects["Author"] = [p["author"]]
    if p["brand"]:
        aspects["Publisher"] = [p["brand"]]
    aspects["Language"] = [p["language"] or default_lang]
    product = {"title": p["title"], "description": p["description_html"], "aspects": aspects, "imageUrls": p["images"][:24]}
    if p["isbn"]:
        product["isbn"] = [p["isbn"].replace("-", "")]
    return product


def push_one(eb, p, a, ctx):
    sku = p["sku"]
    product = inventory_item_body(p, eb, a.language_now)
    item = {
        "availability": {"shipToLocationAvailability": {"quantity": a.quantity}},
        "condition": p["condition"],
        "product": product,
    }
    cat_id = a.category_id
    if not cat_id:
        cat_id, cat_name = eb.suggest_category(p["title"])
        if not cat_id:
            raise RuntimeError("no category suggestion; pass --category-id")
        print(f"   category: {cat_id} ({cat_name})")

    offer = {
        "sku": sku,
        "marketplaceId": eb.market,
        "format": "FIXED_PRICE",
        "availableQuantity": a.quantity,
        "categoryId": str(cat_id),
        "listingDescription": p["description_html"],
        "pricingSummary": {"price": {"value": market_price(p["price"], eb.currency), "currency": eb.currency}},
        "listingPolicies": {k: v for k, v in ctx["policies"].items() if v},
        "merchantLocationKey": ctx["location"],
        "includeCatalogProductDetails": True,
    }

    if a.dry_run:
        print(json.dumps({"inventory_item": item, "offer": offer}, ensure_ascii=False, indent=2)[:1500])
        return None

    eb.call("PUT", f"/sell/inventory/v1/inventory_item/{sku}", item, ok=(200, 201, 204))
    existing = eb.call("GET", "/sell/inventory/v1/offer", params={"sku": sku, "marketplace_id": eb.market}, ok=(200, 404))
    offers = existing.get("offers") or []
    if offers:
        offer_id = offers[0]["offerId"]
        eb.call("PUT", f"/sell/inventory/v1/offer/{offer_id}", offer, ok=(200, 204))
    else:
        offer_id = eb.call("POST", "/sell/inventory/v1/offer", offer)["offerId"]
    if a.no_publish:
        return {"offerId": offer_id, "published": False, "quantity": a.quantity}
    res = eb.call("POST", f"/sell/inventory/v1/offer/{offer_id}/publish")
    return {"offerId": offer_id, "listingId": res.get("listingId"), "published": True, "quantity": a.quantity}


def sold_out_updates(raw, state, markets, quantity):
    """Stock changes for books already listed: sold out -> 0, back in stock -> quantity.
    Returns {sku: (new_quantity, [(state_key, offerId), ...])}."""
    out = {}
    for r in raw:
        p = normalize(r)
        if not p["sku"]:
            continue
        for market in markets:
            key = f"{market}:{p['sku']}"
            e = state.get(key)
            if not e or not e.get("offerId"):
                continue
            current = e.get("quantity", quantity)
            want = 0 if p["sold_out"] else (current if current else quantity)
            if want != current:
                out.setdefault(p["sku"], (want, []))[1].append((key, e["offerId"]))
    return out


def apply_stock_updates(eb, updates, state):
    """POST bulk_update_price_quantity, 25 SKUs per call; records the new quantity per site."""
    items = list(updates.items())
    done = 0
    for i in range(0, len(items), 25):
        part = items[i:i + 25]
        body = {"requests": [{"sku": sku, "shipToLocationAvailability": {"quantity": q},
                              "offers": [{"offerId": oid, "availableQuantity": q} for _, oid in offers]}
                             for sku, (q, offers) in part]}
        try:
            res = eb.call("POST", "/sell/inventory/v1/bulk_update_price_quantity", body, ok=(200, 207))
        except RuntimeError as e:
            print(f"   ! stock update failed: {e}")
            continue
        failed = {}
        for r in res.get("responses") or []:
            if r.get("statusCode") not in (200, None) or r.get("errors"):
                failed[(r.get("sku"), r.get("offerId"))] = "; ".join(x.get("message", "") for x in r.get("errors") or [])
        for sku, (q, offers) in part:
            for key, oid in offers:
                if (sku, oid) in failed or (sku, None) in failed:
                    print(f"   ! {key}: stock update rejected: {failed.get((sku, oid)) or failed.get((sku, None))}")
                    continue
                state[key]["quantity"] = q
                done += 1
                print(f"   {key}: stock {q}" + ("  (sold out)" if q == 0 else "  (back in stock)"))
        STATE_FILE.write_text(json.dumps(state, indent=2))
    return done


def parse_markets(a):
    if a.markets:
        v = a.markets.strip().lower()
        if v == "all":
            ms = list(MARKETS)
        elif v == "eur":
            ms = list(EURO_MARKETS)
        else:
            ms = [m.strip().upper() for m in a.markets.split(",") if m.strip()]
    else:
        ms = [a.market or "EBAY_ES"]
    bad = [m for m in ms if m not in MARKETS]
    if bad or not ms:
        sys.exit(f"Unknown eBay site(s): {', '.join(bad) or '(none)'}. Known: {', '.join(MARKETS)}, or 'eur' / 'all'.")
    return list(dict.fromkeys(ms))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # accents on Windows consoles
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file")
    ap.add_argument("--market", default=None, choices=sorted(MARKETS), help="one site (default EBAY_ES)")
    ap.add_argument("--markets", help="several sites, comma separated (EBAY_ES,EBAY_DE,...), or 'eur' / 'all'")
    ap.add_argument("--category-id", help="eBay leaf category id (else suggested from title, per site)")
    ap.add_argument("--quantity", type=int, default=1)
    ap.add_argument("--language", default=None, help="book language aspect (default: BOOK_LANGUAGE in each site's words)")
    ap.add_argument("--limit", type=int, default=0, help="max new listings per site")
    ap.add_argument("--no-publish", action="store_true", help="create unpublished offers only")
    ap.add_argument("--dry-run", action="store_true", help="show payloads, call nothing but lookups")
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--zero-sold-out", action="store_true",
                    help="stock 0 on every site for listed books now sold out; restore stock when they are back")
    a = ap.parse_args()
    markets = parse_markets(a)

    raw = json.loads(Path(a.json_file).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    products = []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        probs = validate(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        products.append((i, p))

    def done(key):
        return key in state and state[key].get("published") == (not a.no_publish)

    todo = {}
    for market in markets:
        items = [(i, f"{market}:{p['sku']}", p) for i, p in products if not done(f"{market}:{p['sku']}")]
        todo[market] = items[: a.limit] if a.limit else items
        already = len(products) - len(items)
        print(f"{market}: {len(todo[market])} item(s) to list" + (f", {already} already done" if already else ""))
    updates = sold_out_updates(raw, state, markets, a.quantity) if a.zero_sold_out else {}
    if updates:
        print(f"{sum(len(o) for _, o in updates.values())} listing(s) need a stock change (sold out / back in stock).")
    if not any(todo.values()) and not updates:
        return

    eb0 = Ebay(a.sandbox, markets[0])
    if a.dry_run and not all(os.environ.get(k) for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN")):
        print("(dry run without credentials: showing item payload only)")
        for market in markets:
            eb = Ebay(a.sandbox, market)
            lang = a.language or book_language(eb.lang)
            for i, key, p in todo[market][:3]:
                body = inventory_item_body(p, eb, lang)
                print(f"[{i}] {market} {market_price(p['price'], eb.currency)} {eb.currency}",
                      json.dumps(body, ensure_ascii=False)[:600])
        return

    eb0.login()
    if updates:
        if a.dry_run:
            for sku, (q, offers) in updates.items():
                print(f"   (dry run) {sku}: stock {q} on {', '.join(k.split(':')[0] for k, _ in offers)}")
        else:
            apply_stock_updates(eb0, updates, state)
    if not any(todo.values()):
        return
    location = eb0.location_key()
    if not location:
        sys.exit("No inventory location found. Create one in Seller Hub or set EBAY_LOCATION_KEY.")
    sites = []
    for market in markets:
        if not todo[market]:
            continue
        eb = Ebay(a.sandbox, market)
        eb.token, eb.app_token = eb0.token, eb0.app_token
        try:
            policies = {
                "fulfillmentPolicyId": eb.first_policy("fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId", "EBAY_FULFILLMENT_POLICY_ID"),
                "paymentPolicyId": eb.first_policy("payment_policy", "paymentPolicies", "paymentPolicyId", "EBAY_PAYMENT_POLICY_ID"),
                "returnPolicyId": eb.first_policy("return_policy", "returnPolicies", "returnPolicyId", "EBAY_RETURN_POLICY_ID"),
            }
        except RuntimeError as e:
            print(f"! {market}: could not read business policies ({e}); site skipped")
            continue
        if not policies["fulfillmentPolicyId"] or not policies["returnPolicyId"]:
            print(f"! {market}: missing shipping/return business policy for this site. "
                  f"Create them in Seller Hub; site skipped.")
            continue
        sites.append((market, eb, {"location": location, "policies": policies}))
    if not sites:
        sys.exit("No eBay site is ready (business policies missing). Create them in Seller Hub.")

    total = sum(len(todo[m]) for m, _, _ in sites)
    if not (a.dry_run or a.no_publish or a.yes):
        names = ", ".join(m for m, _, _ in sites)
        if input(f"This will publish {total} LIVE listing(s) on {names}. Type 'yes': ").strip().lower() != "yes":
            return

    for market, eb, ctx in sites:
        a.language_now = a.language or book_language(eb.lang)
        print(f"--- {market} ({eb.currency}) ---")
        for n, (i, key, p) in enumerate(todo[market], 1):
            print(f"[{i}] ({n}/{len(todo[market])}) {p['title']}")
            try:
                res = push_one(eb, p, a, ctx)
                if res:
                    state[key] = res
                    STATE_FILE.write_text(json.dumps(state, indent=2))
                    print("   ok:", res)
            except Exception as e:
                print(f"   ! failed: {e}")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
