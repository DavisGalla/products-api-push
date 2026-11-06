#!/usr/bin/env python3
"""
Push products to TikTok Shop (Spain and the other EU shops) through the
official TikTok Shop Partner API (https://partner.tiktokshop.com,
gateway https://open-api.tiktokglobalshop.com, API version 202309+).

Like AliExpress and Temu, TikTok Shop has no catalog to attach an offer to:
every book becomes its own product (title, description, photos, category
attributes, warehouse stock, weight and size). All books go into one TikTok
category (CATEGORY_ID; find it with `categories` or `recommend`). The
category's required attributes are read from TikTok: an ISBN attribute is
filled in automatically, others come from PRODUCT_ATTRIBUTES. TikTok reviews
every new product before it goes live.

ONE-TIME SETUP (see docs/tiktok_push.md)
  1. partner.tiktokshop.com: create a Partner Center account and an app
     (custom app for your own shop), request the Product, Logistics and
     Authorization API scopes. You get an App Key, App Secret and Service ID.
  2. Authorize the app for your shop (the token lasts a limited time):
        python tiktok_push.py auth-url --service-id <service id>
        (open the printed URL, log in with the SELLER account, copy ?code=...)
        python tiktok_push.py token <code>
     The tokens are saved to tiktok_token.json (not in git) and refreshed
     automatically. On a server, put the refresh_token in Bitwarden as
     TIKTOK_REFRESH_TOKEN instead.
  3. Seller Center: a warehouse, and an EU responsible person + manufacturer
     (GPSR) if your category requires them (see `check`).
  Credentials (PowerShell or Bitwarden):
      $env:TIKTOK_APP_KEY = "..."
      $env:TIKTOK_APP_SECRET = "..."

USAGE
    pip install requests
    python tiktok_push.py check                       # shop, warehouses, GPSR
    python tiktok_push.py categories --find libro     # find the books category
    python tiktok_push.py recommend "Dragona. Libro para bebes"   # TikTok's suggestion
    python tiktok_push.py attributes <category_id>    # required attributes and rules
    python tiktok_push.py push --dry-run              # build the products, send nothing
    python tiktok_push.py push --limit 1              # one real product
    python tiktok_push.py push                        # everything
    python tiktok_push.py push --resend --offline-sold-out   # sync price/stock, hide sold-out
    python tiktok_push.py status                      # live / in review / rejected

NOTE: new books in Spain have a legally fixed price (Ley 10/2007, max 5%
discount). Don't use --price-factor to sell above the cover price, and keep
TikTok's own promotions within the legal discount.
"""
import argparse
import hashlib
import hmac
import html
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

CURRENCY = "EUR"             # currency of your TikTok shop (Spain: EUR)
LOCALE = "es-ES"             # language for category/attribute names

# TikTok leaf category for all books (see `categories --find libro` or `recommend`). "" = not set.
CATEGORY_ID = ""
# Required category attributes that can't be filled automatically. Get them
# with `attributes <category_id>`; each entry is pasted from there, e.g.
#   {"id": "100392", "values": [{"id": "1000123"}]}     (a listed value)
#   {"id": "100107", "values": [{"name": "Español"}]}   (free text, if customizable)
PRODUCT_ATTRIBUTES = []

# TikTok Shop EU listings are new goods.
ALLOWED_CONDITIONS = {"NewCondition"}

WAREHOUSE_ID = ""            # "" = your default sales warehouse (see `check`)
RESPONSIBLE_PERSON_ID = ""   # GPSR, when the category requires it. "" = the first one in Seller Center
MANUFACTURER_ID = ""         # GPSR, when the category requires it. "" = the first one in Seller Center
DEFAULT_WEIGHT_KG = 0.4      # package weight when the product has no "weight_kg"
PACKAGE_CM = (24, 17, 3)     # length, width, height of the parcel in cm (whole numbers)
MAX_IMAGES = 9
# ======================================================================

API_URL = "https://open-api.tiktokglobalshop.com"
AUTH_URL = "https://auth.tiktok-shops.com"
AUTHORIZE_URL = "https://services.tiktokshop.com/open/authorize"
STATE_FILE = Path("tiktok_posted.json")
PREVIEW_FILE = Path("tiktok_products.json")
TOKEN_FILE = Path("tiktok_token.json")
CACHE_FILE = Path("tiktok_cache.json")       # uploaded photos
STATUS = {"ACTIVATE": "LIVE", "PENDING": "in review", "FAILED": "REJECTED", "DRAFT": "draft",
          "SELLER_DEACTIVATED": "deactivated", "PLATFORM_DEACTIVATED": "DEACTIVATED BY TIKTOK",
          "FREEZE": "FROZEN", "DELETED": "deleted"}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code, self.msg = code, msg


# --------------------------------------------------------------------------- API

def sign(secret, path, query, body=""):
    """TikTok Shop signature: HMAC-SHA256(secret, secret + path + sorted key+value + body + secret), hex.
    `sign` and `access_token` are excluded; the body is left out for multipart uploads (body="")."""
    params = "".join(f"{k}{v}" for k, v in sorted(query.items())
                     if k not in ("sign", "access_token") and not isinstance(v, (list, dict)))
    text = secret + path + params + (body or "") + secret
    return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def find_name(x):
    """First 'name' string in a (possibly localized) object."""
    if isinstance(x, dict):
        if isinstance(x.get("name"), str):
            return x["name"]
        for v in x.values():
            n = find_name(v)
            if n:
                return n
    if isinstance(x, list):
        for v in x:
            n = find_name(v)
            if n:
                return n
    return None


class TikTok:
    def __init__(self):
        self.key = os.environ.get("TIKTOK_APP_KEY")
        self.secret = os.environ.get("TIKTOK_APP_SECRET")
        missing = [k for k, v in (("TIKTOK_APP_KEY", self.key), ("TIKTOK_APP_SECRET", self.secret)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.base = (os.environ.get("TIKTOK_API_URL") or API_URL).rstrip("/")
        self.auth = (os.environ.get("TIKTOK_AUTH_URL") or AUTH_URL).rstrip("/")
        self.tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8")) if TOKEN_FILE.exists() else {}

    # ---- tokens
    def token_call(self, path, **params):
        r = requests.get(f"{self.auth}{path}", params=dict(params, app_key=self.key, app_secret=self.secret), timeout=60)
        try:
            data = r.json()
        except ValueError:
            raise ApiError(r.status_code, r.text[:200])
        if data.get("code") != 0 or not (data.get("data") or {}).get("access_token"):
            raise ApiError(data.get("code", r.status_code), data.get("message") or data)
        return data["data"]

    def save_token(self, data):
        keep = {k: self.tok.get(k) for k in ("shop_cipher", "shop_name") if self.tok.get(k)}
        self.tok = dict(keep, access_token=data["access_token"], refresh_token=data.get("refresh_token"),
                        access_token_expire_in=int(data.get("access_token_expire_in") or 0),
                        refresh_token_expire_in=int(data.get("refresh_token_expire_in") or 0),
                        seller_name=data.get("seller_name"), saved_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        TOKEN_FILE.write_text(json.dumps(self.tok, indent=2), encoding="utf-8")
        return self.tok

    def refresh(self):
        rt = self.tok.get("refresh_token") or os.environ.get("TIKTOK_REFRESH_TOKEN")
        if not rt:
            sys.exit("No TikTok Shop authorization yet. Run `auth-url` and `token <code>` first "
                     "(or set TIKTOK_REFRESH_TOKEN).")
        try:
            self.save_token(self.token_call("/api/v2/token/refresh", refresh_token=rt, grant_type="refresh_token"))
        except ApiError as e:
            sys.exit(f"Could not refresh the TikTok Shop token ({e}). Authorize again: `auth-url`, then `token <code>`.")

    def access_token(self):
        if not self.tok.get("access_token") or self.tok.get("access_token_expire_in", 0) - time.time() < 30 * 60:
            self.refresh()
        return self.tok["access_token"]

    # ---- business calls
    def call(self, method, path, query=None, body=None, files=None, shop=True, retries=5, _refreshed=False):
        for attempt in range(retries):
            q = dict(query or {}, app_key=self.key, timestamp=str(int(time.time())))
            if shop:
                q["shop_cipher"] = self.shop_cipher()
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else ""
            q["sign"] = sign(self.secret, path, q, "" if files else raw)
            headers = {"x-tts-access-token": self.access_token()}
            if not files:
                headers["Content-Type"] = "application/json"
            r = requests.request(method, f"{self.base}{path}", params=q, headers=headers, files=files,
                                 data=None if files else (raw.encode("utf-8") if body is not None else None), timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                print(f"   (API returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                data = r.json()
            except ValueError:
                raise ApiError(r.status_code, r.text[:300])
            code, msg = data.get("code"), data.get("message") or "error"
            if code == 0:
                return data.get("data") or {}
            low = str(msg).lower()
            if "too many" in low or "rate limit" in low or "frequency" in low:
                wait = 2 ** attempt
                print(f"   (TikTok: {msg}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            if "access token" in low or "access_token" in low or code in (105001, 105002, 105003, 105005):
                if not _refreshed:
                    self.tok["access_token_expire_in"] = 0
                    self.refresh()
                    return self.call(method, path, query, body, files, shop, retries, _refreshed=True)
                sys.exit(f"TikTok Shop rejected the access token ({code} {msg}). Authorize again: `auth-url`, then `token <code>`.")
            if "sign" in low and "invalid" in low:
                sys.exit(f"TikTok Shop rejected the signature ({code} {msg}). Check TIKTOK_APP_SECRET.")
            raise ApiError(code, msg)
        raise ApiError("retries", "gave up after retries")

    def shop_cipher(self):
        cipher = os.environ.get("TIKTOK_SHOP_CIPHER") or self.tok.get("shop_cipher")
        if cipher:
            return cipher
        shops = self.call("GET", "/authorization/202309/shops", shop=False).get("shops") or []
        if not shops:
            sys.exit("No shop is authorized for this app. Run `auth-url` and `token <code>` with the seller account.")
        self.tok.update(shop_cipher=shops[0].get("cipher"), shop_name=shops[0].get("name"))
        TOKEN_FILE.write_text(json.dumps(self.tok, indent=2), encoding="utf-8")
        return self.tok["shop_cipher"]

    # ---- endpoints
    def shops(self):
        return self.call("GET", "/authorization/202309/shops", shop=False).get("shops") or []

    def warehouses(self):
        return self.call("GET", "/logistics/202309/warehouses").get("warehouses") or []

    def responsible_persons(self):
        return self.call("POST", "/product/202501/compliance/responsible_persons/search", {"page_size": 50},
                         body={}).get("responsible_persons") or []

    def manufacturers(self):
        return self.call("POST", "/product/202501/compliance/manufacturers/search", {"page_size": 50},
                         body={}).get("manufacturers") or []

    def categories(self, keyword=None):
        q = {"locale": LOCALE}
        if keyword:
            q["keyword"] = keyword
        return self.call("GET", "/product/202309/categories", q).get("categories") or []

    def recommend(self, title, description=""):
        return self.call("POST", "/product/202309/categories/recommend",
                         body={"product_title": title[:255], "description": description[:2000], "locale": LOCALE})

    def attributes(self, cat):
        return self.call("GET", f"/product/202309/categories/{cat}/attributes", {"locale": LOCALE}).get("attributes") or []

    def rules(self, cat):
        return self.call("GET", f"/product/202309/categories/{cat}/rules", {"locale": LOCALE})

    def upload_image(self, name, content):
        res = self.call("POST", "/product/202309/images/upload", shop=False,
                        files={"data": (name, content, "application/octet-stream"), "use_case": (None, "MAIN_IMAGE")})
        if not res.get("uri"):
            raise ApiError("image", "no uri returned")
        return res["uri"]

    def create(self, product):
        return self.call("POST", "/product/202309/products", body=product)

    def update_inventory(self, product_id, sku_id, warehouse_id, qty):
        res = self.call("POST", f"/product/202309/products/{product_id}/inventory/update", body={
            "skus": [{"id": sku_id, "inventory": [{"warehouse_id": warehouse_id, "quantity": qty}]}]})
        if res.get("errors"):
            raise ApiError("inventory", json.dumps(res["errors"], ensure_ascii=False)[:300])

    def update_price(self, product_id, sku_id, amount):
        self.call("POST", f"/product/202309/products/{product_id}/prices/update", body={
            "skus": [{"id": sku_id, "price": {"amount": amount, "currency": CURRENCY}}]})

    def set_active(self, product_ids, active):
        failed = {}
        for i in range(0, len(product_ids), 20):
            res = self.call("POST", "/product/202309/products/" + ("activate" if active else "deactivate"),
                            body={"product_ids": product_ids[i:i + 20]})
            for e in res.get("errors") or []:
                pid = (e.get("detail") or {}).get("product_id")
                if pid:
                    failed[pid] = e.get("message") or e.get("code")
        return failed

    def search(self, seller_skus):
        out = []
        for i in range(0, len(seller_skus), 50):
            token = None
            while True:
                q = {"page_size": 100}
                if token:
                    q["page_token"] = token
                res = self.call("POST", "/product/202502/products/search", q,
                                body={"status": "ALL", "seller_skus": seller_skus[i:i + 50]})
                out += res.get("products") or []
                token = res.get("next_page_token")
                if not token:
                    break
        return out


# ---------------------------------------------------------------------- helpers

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
    """price * factor as a 2-decimal string, rounded half-up (8.075 -> '8.08')."""
    v = Decimal(str(price)) * Decimal(str(factor))
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def seller_sku(sku):
    """TikTok's EU seller SKU must be 6-50 characters."""
    return sku if 6 <= len(sku) <= 50 else ("SKU-" + sku)[:50]


def normalize(raw):
    images = raw.get("images") or ([raw["image"]] if raw.get("image") else [])
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": (raw.get("title") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "quantity": raw.get("quantity"),
        "weight": raw.get("weight_kg"),
        "images": [u for u in images if str(u).startswith("http")][:MAX_IMAGES],
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in ALLOWED_CONDITIONS:
        probs.append(f"condition '{p['condition']}' not sold on TikTok Shop (ALLOWED_CONDITIONS)")
    if not p["title"]:
        probs.append("no title")
    if not p["images"]:
        probs.append("no images")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def description_html(p):
    paras = [html.escape(x.strip()) for x in (p["description"] or p["title"]).split("\n") if x.strip()]
    paras.append(f"ISBN: {p['ean']}")
    return "".join(f"<p>{x}</p>" for x in paras)[:10000]


def is_required(attr):
    return bool(attr.get("is_requried", attr.get("is_required")))   # the API spells it "is_requried"


class Plan:
    """What the category needs: attributes (PRODUCT_ATTRIBUTES + ISBN), warehouse and GPSR ids."""

    def __init__(self, api, a, offline=False):
        self.cat = a.category or "<CATEGORY_ID>"
        self.missing, self.isbn_attrs = [], []
        self.warehouse = a.warehouse or None
        self.rp, self.man = RESPONSIBLE_PERSON_ID or None, MANUFACTURER_ID or None
        if offline:
            self.warehouse = self.warehouse or "<warehouse>"
            return
        given = {str(x.get("id")) for x in PRODUCT_ATTRIBUTES}
        for attr in api.attributes(self.cat):
            if attr.get("type") != "PRODUCT_PROPERTY" or not is_required(attr) or str(attr.get("id")) in given:
                continue
            if "isbn" in str(attr.get("name", "")).lower() and attr.get("is_customizable", True):
                self.isbn_attrs.append(str(attr["id"]))
            else:
                self.missing.append(f"{attr.get('name')} (id {attr.get('id')})")
        rules = api.rules(self.cat)
        if not self.warehouse:
            whs = [w for w in api.warehouses() if w.get("type", "SALES_WAREHOUSE") == "SALES_WAREHOUSE"
                   and w.get("effect_status", "ENABLED") == "ENABLED"]
            pick = next((w for w in whs if w.get("is_default")), whs[0] if whs else None)
            if pick:
                self.warehouse = pick.get("id")
            else:
                self.missing.append("an enabled sales warehouse (create one in Seller Center)")
        if (rules.get("responsible_person") or {}).get("is_required") and not self.rp:
            rps = api.responsible_persons()
            self.rp = rps[0].get("id") if rps else None
            if not self.rp:
                self.missing.append("an EU responsible person (GPSR) - add one in Seller Center")
        if (rules.get("manufacturer") or {}).get("is_required") and not self.man:
            mans = api.manufacturers()
            self.man = mans[0].get("id") if mans else None
            if not self.man:
                self.missing.append("a manufacturer (GPSR) - add one in Seller Center")

    def attributes(self, p):
        return [dict(x) for x in PRODUCT_ATTRIBUTES] + [{"id": i, "values": [{"name": p["ean"]}]} for i in self.isbn_attrs]


def product_payload(p, a, plan, images):
    body = {
        "save_mode": "LISTING",
        "title": p["title"][:255],
        "description": description_html(p),
        "category_id": str(plan.cat),
        "main_images": [{"uri": u} for u in images],
        "product_attributes": plan.attributes(p),
        "skus": [{
            "seller_sku": seller_sku(p["sku"]),
            "external_sku_id": p["sku"],
            "price": {"amount": money(p["price"], a.price_factor), "currency": CURRENCY},
            "inventory": [{"warehouse_id": plan.warehouse,
                           "quantity": max(1, min(int(p["quantity"] or a.quantity), 99999))}],
            "identifier_code": {"code": p["ean"], "type": "ISBN"},
        }],
        "package_weight": {"value": f"{float(p['weight'] or DEFAULT_WEIGHT_KG):.3f}", "unit": "KILOGRAM"},
        "package_dimensions": {"length": str(int(PACKAGE_CM[0])), "width": str(int(PACKAGE_CM[1])),
                               "height": str(int(PACKAGE_CM[2])), "unit": "CENTIMETER"},
        "external_product_id": p["sku"],
        "idempotency_key": f"pp-{p['sku']}-{p['ean']}"[:64],
    }
    if plan.rp:
        body["responsible_person_ids"] = [plan.rp]
    if plan.man:
        body["manufacturer_ids"] = [plan.man]
    return body


# --------------------------------------------------------------------- commands

def cmd_auth_url(a):
    print("Open this URL, log in with the TikTok Shop SELLER account and authorize the app:\n")
    print(f"{AUTHORIZE_URL}?{urlencode({'service_id': a.service_id, 'state': 'product-pushers'})}\n")
    print("You'll be sent to the app's redirect URL with ?code=... Then run:\n"
          "    python tiktok_push.py token <code>")


def cmd_token(api, a):
    try:
        tok = api.save_token(api.token_call("/api/v2/token/get", auth_code=a.code, grant_type="authorized_code"))
    except ApiError as e:
        sys.exit(f"Could not create the token: {e}")
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"
    print(f"Authorized{' for ' + tok['seller_name'] if tok.get('seller_name') else ''}. Tokens saved to {TOKEN_FILE.resolve()}")
    print(f"   access token valid until {fmt(tok['access_token_expire_in'])}, refresh token until "
          f"{fmt(tok['refresh_token_expire_in'])} (refreshed automatically).")
    print("   On a server: store refresh_token from that file in Bitwarden as TIKTOK_REFRESH_TOKEN.")


def cmd_check(api, a):
    shops = api.shops()
    print("Authorized shops:" + ("" if shops else " none"))
    for s in shops:
        print(f"   {s.get('name')}  region {s.get('region')}  ({s.get('seller_type')}, code {s.get('code')})")
    whs = api.warehouses()
    print("Warehouses:" + ("" if whs else " none - create one in Seller Center"))
    for w in whs:
        print(f"   {w.get('id')}  {w.get('name')}  {w.get('type')} {w.get('effect_status')}"
              + ("  (default)" if w.get("is_default") else ""))
    rps, mans = api.responsible_persons(), api.manufacturers()
    print("EU responsible persons (GPSR): " + (", ".join(f"{r.get('id')} {find_name(r) or ''}" for r in rps) or "none"))
    print("Manufacturers (GPSR): " + (", ".join(f"{m.get('id')} {find_name(m) or ''}" for m in mans) or "none"))
    if not CATEGORY_ID:
        print("CATEGORY_ID is not set yet - see `categories --find libro` or `recommend \"<title>\"`.")


def cmd_categories(api, a):
    cats = api.categories(a.find)
    for c in cats:
        perm = ",".join(c.get("permission_statuses") or [])
        print(f"   {c.get('id'):<10} {'(leaf) ' if c.get('is_leaf') else '       '}{c.get('local_name')}"
              + (f"  [{perm}]" if perm and perm != "AVAILABLE" else ""))
    print("Use a leaf category without restrictions as CATEGORY_ID." if cats else "No categories found.")


def cmd_recommend(api, a):
    res = api.recommend(a.title)
    path = " > ".join(str(c.get("name")) for c in res.get("categories") or [])
    perms = ",".join((res.get("categories") or [{}])[-1].get("permission_statuses") or [])
    print(f"TikTok suggests category {res.get('leaf_category_id')}: {path}" + (f"  [{perms}]" if perms else ""))
    print("Check it with `attributes <id>` before setting CATEGORY_ID.")


def cmd_attributes(api, a):
    attrs = [x for x in api.attributes(a.category_id) if x.get("type") == "PRODUCT_PROPERTY"]
    print(f"Product attributes of category {a.category_id} (required first):")
    for x in sorted(attrs, key=lambda x: not is_required(x)):
        auto = "  -> filled with the ISBN automatically" if "isbn" in str(x.get("name", "")).lower() else ""
        print(f"   {'REQUIRED' if is_required(x) else 'optional':<9} {x.get('name')}" + auto)
        vals = x.get("values") or []
        for v in vals[:10]:
            print(f"        {json.dumps({'id': x.get('id'), 'values': [{'id': v.get('id')}]})}   # {v.get('name')}")
        if len(vals) > 10:
            print(f"        ... {len(vals) - 10} more")
        if not auto and (x.get("is_customizable") or not vals):
            print(f"        {json.dumps({'id': x.get('id'), 'values': [{'name': '<your text>'}]})}")
    rules = api.rules(a.category_id)
    needs = [k for k in ("responsible_person", "manufacturer", "package_dimension", "seller_sku")
             if (rules.get(k) or {}).get("is_required")]
    certs = [c.get("name") for c in rules.get("product_certifications") or [] if c.get("is_required")]
    print("Category rules: requires " + (", ".join(needs) or "nothing extra")
          + (f"; certifications: {', '.join(certs)}" if certs else ""))
    print("Paste the lines you need into PRODUCT_ATTRIBUTES at the top of tiktok_push.py.")


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

    new, resend, hide = [], [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and a.offline_sold_out and p["sku"] in state and state[p["sku"]].get("active", True):
            hide.append(p)
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

    plan = None
    if new:
        if not a.category and not a.dry_run:
            sys.exit("CATEGORY_ID is not set. Find the books category with `categories --find libro` or "
                     "`recommend \"<title>\"`, then set CATEGORY_ID (or pass --category).")
        if api and a.category:
            plan = Plan(api, a)
            if plan.missing:
                sys.exit(f"Category {a.category} needs: {'; '.join(plan.missing)}.\n"
                         f"Run `attributes {a.category}` and `check`, then fill in PRODUCT_ATTRIBUTES / Seller Center.")
        else:
            print("(dry run: category attributes, warehouse and GPSR not looked up - placeholders used)")
            plan = Plan(None, a, offline=True)

    print(f"{len(new)} new product(s)" + (f", {len(resend)} to update" if resend else "")
          + (f", {len(hide)} sold-out to deactivate" if hide else "") + ".")
    if not (new or resend or hide):
        return
    save_json(PREVIEW_FILE, [product_payload(p, a, plan, p["images"]) for p in new])
    if a.dry_run:
        print(f"Dry run: products written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(new) + len(resend) + len(hide)} LIVE product(s) on TikTok Shop. "
                 "Type 'yes': ").strip().lower() != "yes":
            return
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")
    cache = load_json(CACHE_FILE, {})

    created = 0
    for p in new:
        try:
            uris = []
            for url in p["images"]:
                if url not in cache:
                    r = requests.get(url, timeout=60)
                    r.raise_for_status()
                    cache[url] = api.upload_image((url.rsplit("/", 1)[-1].split("?")[0] or "image.jpg")[-100:], r.content)
                    save_json(CACHE_FILE, cache)
                uris.append(cache[url])
            body = product_payload(p, a, plan, uris)
            res = api.create(body)
        except (ApiError, requests.RequestException) as e:
            print(f"   ! {p['sku']}: {e}")
            continue
        sku = body["skus"][0]
        sku_id = next((s.get("id") for s in res.get("skus") or [] if s.get("seller_sku") == sku["seller_sku"]),
                      (res.get("skus") or [{}])[0].get("id"))
        state[p["sku"]] = {"product_id": res.get("product_id"), "sku_id": sku_id, "seller_sku": sku["seller_sku"],
                           "warehouse_id": plan.warehouse, "ean": p["ean"], "price": sku["price"]["amount"],
                           "quantity": sku["inventory"][0]["quantity"], "category": plan.cat, "active": True,
                           "pushed_at": now()}
        save_json(STATE_FILE, state)
        created += 1
        warn = "; ".join(w.get("message", "") for w in res.get("warnings") or [] if w.get("message"))
        print(f"   + {p['sku']}: product {res.get('product_id')}  {sku['price']['amount']} {CURRENCY} "
              f"x{sku['inventory'][0]['quantity']}" + (f"  (warning: {warn})" if warn else ""))

    back = []
    for p in resend:
        info = state[p["sku"]]
        price, qty = money(p["price"], a.price_factor), max(1, min(int(p["quantity"] or a.quantity), 99999))
        try:
            if qty != info.get("quantity"):
                api.update_inventory(info["product_id"], info["sku_id"], info["warehouse_id"], qty)
                info["quantity"] = qty
            if price != info.get("price"):
                api.update_price(info["product_id"], info["sku_id"], price)
                info["price"] = price
        except ApiError as e:
            print(f"   ! {p['sku']}: update failed: {e}")
            continue
        if not info.get("active", True):
            back.append(p["sku"])
        info["updated_at"] = now()
        save_json(STATE_FILE, state)
        print(f"   ~ {p['sku']}: {price} {CURRENCY} x{qty}")
    if back:
        failed = api.set_active([state[s]["product_id"] for s in back], True)
        for s in back:
            if state[s]["product_id"] in failed:
                print(f"   ! {s}: could not reactivate: {failed[state[s]['product_id']]}")
            else:
                state[s]["active"] = True
                print(f"   ^ {s}: reactivated")
        save_json(STATE_FILE, state)

    if hide:
        failed = api.set_active([state[p["sku"]]["product_id"] for p in hide], False)
        for p in hide:
            pid = state[p["sku"]]["product_id"]
            if pid in failed:
                print(f"   ! {p['sku']}: could not deactivate: {failed[pid]}")
            else:
                state[p["sku"]].update(active=False, updated_at=now())
                print(f"   - {p['sku']}: sold out, deactivated")
        save_json(STATE_FILE, state)
    print(f"Done: {created} created. TikTok reviews new products first - run `status`.")


def cmd_status(api, a):
    state = load_json(STATE_FILE, {})
    if not state:
        print("No products yet.")
        return
    found = {}
    for prod in api.search([v["seller_sku"] for v in state.values() if v.get("seller_sku")]):
        found[str(prod.get("id"))] = prod
    for sku, info in sorted(state.items()):
        prod = found.get(str(info.get("product_id")))
        if not prod:
            print(f"{sku}: product {info.get('product_id')} not found on TikTok Shop")
            continue
        s = (prod.get("skus") or [{}])[0]
        qty = sum(int(i.get("quantity") or 0) for i in s.get("inventory") or [])
        price = (s.get("price") or {}).get("tax_exclusive_price") or (s.get("price") or {}).get("sale_price") or info.get("price")
        print(f"{sku}: {STATUS.get(prod.get('status'), prod.get('status'))}  product={prod.get('id')}  "
              f"price={price}  stock={qty}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push products to TikTok Shop (Partner API)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("auth-url", help="print the URL the seller opens to authorize this app")
    s.add_argument("--service-id", required=True, help="your app's Service ID from Partner Center")
    s = sub.add_parser("token", help="exchange the authorization code for tokens")
    s.add_argument("code")
    sub.add_parser("check", help="shops, warehouses and GPSR information")
    s = sub.add_parser("categories", help="search TikTok's categories")
    s.add_argument("--find", help="keyword in the category name")
    s = sub.add_parser("recommend", help="ask TikTok which category fits a title")
    s.add_argument("title")
    s = sub.add_parser("attributes", help="attributes and rules of a category")
    s.add_argument("category_id")
    p = sub.add_parser("push", help="create products from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per product (per-item 'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--category", default=CATEGORY_ID, help="TikTok leaf category for the books")
    p.add_argument("--warehouse", default=WAREHOUSE_ID, help="warehouse id (default: your default sales warehouse)")
    p.add_argument("--resend", action="store_true", help="update stock and price of already pushed products")
    p.add_argument("--offline-sold-out", action="store_true", help="deactivate pushed products that are now sold out")
    p.add_argument("--dry-run", action="store_true", help="write tiktok_products.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new products this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="review / live status of your products")
    a = ap.parse_args()
    if a.cmd == "auth-url":
        return cmd_auth_url(a)
    if a.cmd == "push" and a.dry_run:
        creds = all(os.environ.get(k) for k in ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET"))
        if not (creds and (TOKEN_FILE.exists() or os.environ.get("TIKTOK_REFRESH_TOKEN"))):
            return cmd_push(None, a)             # no credentials needed to build the preview
    api = TikTok()
    try:
        {"token": cmd_token, "check": cmd_check, "categories": cmd_categories, "recommend": cmd_recommend,
         "attributes": cmd_attributes, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except ApiError as e:
        sys.exit(f"TikTok Shop API error {e}")


if __name__ == "__main__":
    main()
