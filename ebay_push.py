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
    python ebay_push.py books.json --sandbox          # test against eBay's sandbox

Markets: EBAY_ES, EBAY_DE, EBAY_FR, EBAY_IT, EBAY_NL, EBAY_AT, EBAY_BE,
         EBAY_IE, EBAY_GB, EBAY_CH, EBAY_PL

Notes
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
SCOPES = "https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account"
# Schema.org condition (scraped JSON) -> eBay inventory condition enum.
# An eBay enum value (e.g. "USED_GOOD") in the JSON is passed through as-is.
CONDITION_MAP = {
    "NewCondition": "NEW",
    "UsedCondition": "USED_VERY_GOOD",
    "RefurbishedCondition": "SELLER_REFURBISHED",
    "DamagedCondition": "FOR_PARTS_OR_NOT_WORKING",
}
LANG_NAMES = {"es": "Español", "de": "Deutsch", "fr": "Français", "it": "Italiano", "nl": "Nederlands", "pl": "Polski", "en": "English"}


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
    product = inventory_item_body(p, eb, a.language)
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
        "pricingSummary": {"price": {"value": f"{float(p['price']):.2f}", "currency": eb.currency}},
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
        return {"offerId": offer_id, "published": False}
    res = eb.call("POST", f"/sell/inventory/v1/offer/{offer_id}/publish")
    return {"offerId": offer_id, "listingId": res.get("listingId"), "published": True}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # accents on Windows consoles
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file")
    ap.add_argument("--market", default="EBAY_ES", choices=sorted(MARKETS))
    ap.add_argument("--category-id", help="eBay leaf category id (else suggested from title)")
    ap.add_argument("--quantity", type=int, default=1)
    ap.add_argument("--language", default=None, help="book language aspect, default from market")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-publish", action="store_true", help="create unpublished offers only")
    ap.add_argument("--dry-run", action="store_true", help="show payloads, call nothing but lookups")
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    a = ap.parse_args()

    raw = json.loads(Path(a.json_file).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    todo = []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        key = f"{a.market}:{p['sku']}"
        if key in state and state[key].get("published") == (not a.no_publish):
            print(f"[{i}] already done: {p['title']}")
            continue
        probs = validate(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        todo.append((i, key, p))
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(todo)} item(s) for {a.market}.")
    if not todo:
        return

    eb = Ebay(a.sandbox, a.market)
    a.language = a.language or LANG_NAMES.get(eb.lang[:2], "English")
    if a.dry_run and not all(os.environ.get(k) for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN")):
        print("(dry run without credentials: showing item payload only)")
        for i, key, p in todo[:3]:
            print(f"[{i}]", json.dumps(inventory_item_body(p, eb, a.language), ensure_ascii=False)[:600])
        return

    eb.login()
    ctx = {
        "location": eb.location_key(),
        "policies": {
            "fulfillmentPolicyId": eb.first_policy("fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId", "EBAY_FULFILLMENT_POLICY_ID"),
            "paymentPolicyId": eb.first_policy("payment_policy", "paymentPolicies", "paymentPolicyId", "EBAY_PAYMENT_POLICY_ID"),
            "returnPolicyId": eb.first_policy("return_policy", "returnPolicies", "returnPolicyId", "EBAY_RETURN_POLICY_ID"),
        },
    }
    if not ctx["location"]:
        sys.exit("No inventory location found. Create one in Seller Hub or set EBAY_LOCATION_KEY.")
    if not ctx["policies"]["fulfillmentPolicyId"] or not ctx["policies"]["returnPolicyId"]:
        sys.exit(f"Missing shipping/return business policy for {a.market}. Create them in Seller Hub.")

    if not (a.dry_run or a.no_publish or a.yes):
        if input(f"This will publish {len(todo)} LIVE listing(s) on {a.market}. Type 'yes': ").strip().lower() != "yes":
            return

    for n, (i, key, p) in enumerate(todo, 1):
        print(f"[{i}] ({n}/{len(todo)}) {p['title']}")
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
