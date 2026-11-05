#!/usr/bin/env python3
"""
Push products to AliExpress (as a local/POP seller, e.g. on aliexpress.es)
through the official AliExpress Open Platform API
(https://openservice.aliexpress.com, gateway https://api-sg.aliexpress.com).

Unlike the Mirakl/Kaufland marketplaces, AliExpress has no catalog to attach
an offer to: every book becomes its OWN product (title, description, images,
category attributes, shipping template, weight). To make that easier, the
script first asks AliExpress's EAN database (aliexpress.product.eancode.info.query)
about the ISBN: when AliExpress knows the book, its category, attributes and
images are used. Otherwise CATEGORY_ID (see `categories`) and your own photos
(uploaded to your AliExpress photo bank) are used.

ONE-TIME SETUP (see docs/aliexpress_push.md)
  1. Register as a developer at openservice.aliexpress.com, create an app and
     request the product API permissions. You get an App Key and App Secret.
  2. In AliExpress Seller Center: a shipping (freight) template, and an EU
     responsible person (GPSR) - AliExpress rejects EU listings without one.
  3. Authorize the app for your shop (the token lasts a limited time):
        python aliexpress_push.py auth-url --redirect https://your.callback/url
        (open the printed URL, log in with the seller account, copy ?code=...)
        python aliexpress_push.py token <code>
     The tokens are saved to aliexpress_token.json (not in git) and refreshed
     automatically. On a server, put the refresh_token in Bitwarden as
     ALIEXPRESS_REFRESH_TOKEN instead.
  Credentials (PowerShell or Bitwarden):
      $env:ALIEXPRESS_APP_KEY = "..."
      $env:ALIEXPRESS_APP_SECRET = "..."

USAGE
    pip install requests
    python aliexpress_push.py check                    # shop, shipping templates, GPSR
    python aliexpress_push.py ean 9788409591923        # does AliExpress know this ISBN?
    python aliexpress_push.py categories --find libro  # find a books category id
    python aliexpress_push.py attributes <category_id> # required attributes of a category
    python aliexpress_push.py push --dry-run           # build the products, send nothing
    python aliexpress_push.py push --limit 1           # one real product
    python aliexpress_push.py push                     # everything
    python aliexpress_push.py push --resend --offline-sold-out   # sync price/stock, hide sold-out
    python aliexpress_push.py status                   # online / offline / in review

NOTE: new books in Spain have a legally fixed price (Ley 10/2007, max 5%
discount). Don't use --price-factor to sell above the cover price.
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

LANGUAGE = "es"              # language of your titles/descriptions (AliExpress translates the rest)

# Category for books AliExpress's EAN database doesn't know. 0 = skip those books.
# Find a leaf category with `categories --find ...`, then its required
# attributes with `attributes <id>`, and add them to EXTRA_ATTRIBUTES.
CATEGORY_ID = 0
EXTRA_ATTRIBUTES = [
    # {"aliexpress_attribute_name_id": 2, "aliexpress_attribute_value_id": 201512802},  # e.g. Brand: none
    # {"attribute_name": "Idioma", "attribute_value": "Español"},                      # custom attribute
]

# AliExpress listings are new goods: only these source conditions are pushed.
ALLOWED_CONDITIONS = {"NewCondition"}

FREIGHT_TEMPLATE_ID = 0      # 0 = your default shipping template (see `check`)
MSR_EU_ID = 0                # EU responsible person (GPSR). 0 = the one set up in Seller Center (see `check`)
MANUFACTURER_ID = 0          # optional manufacturer id. 0 = the one in Seller Center, if exactly one
SHIPPING_LEAD_DAYS = 3       # days until you ship
DEFAULT_WEIGHT_KG = 0.4      # package weight when the product has no "weight_kg"
PACKAGE_CM = (24, 17, 3)     # length, width, height of the parcel in cm
MAX_IMAGES = 6
# ======================================================================

API_URL = "https://api-sg.aliexpress.com"
AUTH_URL = "https://api-sg.aliexpress.com/oauth/authorize"
STATE_FILE = Path("aliexpress_posted.json")
PREVIEW_FILE = Path("aliexpress_products.json")
TOKEN_FILE = Path("aliexpress_token.json")
IMAGE_CACHE = Path("aliexpress_images.json")
RETRY_CODES = {"ApiCallLimit", "AppCallLimit", "SessionCallLimit", "isp.call-limited"}
STATUSES = ["onSelling", "offline", "auditing", "editingRequired"]


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg = str(code), msg


# --------------------------------------------------------------------------- API

def sign(secret, params, api_path=""):
    """AliExpress Open Platform signature: HMAC-SHA256 over api_path + sorted key+value pairs, upper hex.
    Business APIs (/sync) sign with an empty api_path, system APIs (/rest/auth/...) with their path."""
    text = api_path + "".join(f"{k}{params[k]}" for k in sorted(params) if params[k] not in (None, ""))
    return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def as_list(x):
    """AliExpress wraps lists in an extra object ({"aeop_x_list": {"aeop_x": [...]}}); unwrap it."""
    while isinstance(x, dict) and len(x) == 1:
        x = next(iter(x.values()))
    return x if isinstance(x, list) else ([] if x in (None, "", {}) else [x])


class AliExpress:
    def __init__(self):
        self.key = os.environ.get("ALIEXPRESS_APP_KEY")
        self.secret = os.environ.get("ALIEXPRESS_APP_SECRET")
        missing = [k for k, v in (("ALIEXPRESS_APP_KEY", self.key), ("ALIEXPRESS_APP_SECRET", self.secret)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.base = (os.environ.get("ALIEXPRESS_API_URL") or API_URL).rstrip("/")
        self._token = None

    # ---- system calls (tokens)
    def system(self, path, **params):
        p = dict(params, app_key=self.key, timestamp=str(int(time.time() * 1000)), sign_method="sha256")
        p["sign"] = sign(self.secret, p, path)
        r = requests.post(f"{self.base}/rest{path}", data=p, timeout=60)
        try:
            data = r.json()
        except ValueError:
            raise ApiError(r.status_code, r.text[:200])
        if str(data.get("code", "0")) != "0" or "access_token" not in data:
            raise ApiError(data.get("code", r.status_code), data.get("message") or data.get("msg") or data)
        return data

    def save_token(self, data):
        now = int(time.time() * 1000)
        tok = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expire_time": int(data.get("expire_time") or now + int(data.get("expires_in", 0)) * 1000),
            "refresh_token_valid_time": int(data.get("refresh_token_valid_time")
                                            or now + int(data.get("refresh_expires_in", 0)) * 1000),
            "seller": data.get("user_nick") or data.get("seller_id") or data.get("account"),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        TOKEN_FILE.write_text(json.dumps(tok, indent=2), encoding="utf-8")
        self._token = tok
        return tok

    def access_token(self):
        if self._token is None and TOKEN_FILE.exists():
            self._token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        tok = self._token or {}
        if tok.get("access_token") and tok.get("expire_time", 0) - time.time() * 1000 > 30 * 60 * 1000:
            return tok["access_token"]
        refresh = tok.get("refresh_token") or os.environ.get("ALIEXPRESS_REFRESH_TOKEN")
        if not refresh:
            sys.exit("No AliExpress authorization yet. Run `auth-url` and `token <code>` first "
                     "(or set ALIEXPRESS_REFRESH_TOKEN).")
        try:
            return self.save_token(self.system("/auth/token/refresh", refresh_token=refresh))["access_token"]
        except ApiError as e:
            sys.exit(f"Could not refresh the AliExpress token ({e}). Authorize again: `auth-url`, then `token <code>`.")

    # ---- business calls
    def call(self, method, files=None, retries=5, **params):
        biz = {k: (json.dumps(v, ensure_ascii=False, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v))
               for k, v in params.items() if v is not None}
        for attempt in range(retries):
            p = dict(biz, method=method, app_key=self.key, access_token=self.access_token(),
                     timestamp=str(int(time.time() * 1000)), sign_method="sha256")
            p["sign"] = sign(self.secret, p)
            r = requests.post(f"{self.base}/sync", params={"method": method},
                              data={k: v for k, v in p.items() if k != "method"}, files=files, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                print(f"   (API returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                data = r.json()
            except ValueError:
                raise ApiError(r.status_code, r.text[:300])
            err = data.get("error_response")
            if err is None and "code" in data and str(data["code"]) != "0" and not any(k.endswith("_response") for k in data):
                err = data
            if err:
                code = err.get("code")
                msg = err.get("sub_msg") or err.get("msg") or err.get("message") or "error"
                if str(code) in RETRY_CODES or str(err.get("sub_code")) in RETRY_CODES:
                    wait = 2 ** attempt
                    print(f"   (API rate limit, retrying in {wait}s)")
                    time.sleep(wait)
                    continue
                if str(code) in ("IllegalAccessToken", "27") or "token" in str(err.get("sub_code", "")).lower():
                    sys.exit(f"AliExpress rejected the access token ({msg}). Authorize again: `auth-url`, then `token <code>`.")
                raise ApiError(err.get("sub_code") or code, msg)
            body = data.get(method.replace(".", "_") + "_response")
            if body is None:
                body = next((v for k, v in data.items() if k.endswith("_response")), data)
            return body
        raise ApiError("retries", "gave up after retries")

    # ---- endpoints
    def profile(self):
        return self.call("aliexpress.solution.merchant.profile.get")

    def freight_templates(self):
        return as_list(self.call("aliexpress.freight.redefining.listfreighttemplate").get("aeop_freight_template_d_t_o_list"))

    def msr_list(self):
        return as_list((self.call("aliexpress.merchant.msr.list").get("result") or {}).get("data"))

    def manufacturers(self):
        return as_list((self.call("aliexpress.merchant.manufacture.list").get("result") or {}).get("data"))

    def ean_info(self, ean):
        res = self.call("aliexpress.product.eancode.info.query", ean_code=ean).get("result") or {}
        return res if res.get("success") and res.get("leaf_cate_id") else None

    def categories(self, parent):
        res = self.call("aliexpress.solution.seller.category.tree.query", category_id=parent, filter_no_permission="true")
        return as_list(res.get("children_category_list"))

    def attributes(self, category_id):
        res = self.call("aliexpress.category.redefining.getchildattributesresultbypostcateidandpath",
                        param1=category_id, locale="es_ES").get("result") or {}
        return as_list(res.get("attributes"))

    def upload_image(self, name, content):
        res = self.call("aliexpress.photobank.redefining.uploadimageforsdk", file_name=name,
                        files={"image_bytes": (name, content, "application/octet-stream")}).get("result") or {}
        if not res.get("photobank_url"):
            raise ApiError(res.get("error_code", "upload"), res.get("error_message") or "image upload failed")
        return res["photobank_url"]

    def post_product(self, product):
        res = self.call("aliexpress.solution.product.post", post_product_request=product).get("result") or {}
        if not res.get("product_id"):
            raise ApiError(res.get("error_code", "post"), res.get("error_message") or f"no product_id returned: {res}")
        return res["product_id"]

    def set_online(self, ids, online=True):
        method = "aliexpress.postproduct.redefining." + ("onlineaeproduct" if online else "offlineaeproduct")
        failed = {}
        for i in range(0, len(ids), 50):
            res = self.call(method, product_ids=";".join(map(str, ids[i:i + 50]))).get("result") or {}
            for d in as_list(res.get("error_details")):
                for pid in as_list(d.get("product_ids")):
                    failed[int(pid)] = d.get("error_message") or d.get("error_code")
            if res.get("success") is False and not failed:
                for pid in ids[i:i + 50]:
                    failed[int(pid)] = res.get("error_message") or res.get("error_code")
        return failed

    def batch_update(self, kind, items):
        """kind = 'price' or 'inventory'; items = [(product_id, sku_code, value)] -> {product_id: error}"""
        method = f"aliexpress.solution.batch.product.{kind}.update"
        failed = {}
        for i in range(0, len(items), 20):
            chunk = [{"product_id": pid, "multiple_sku_update_list": [{"sku_code": sku, kind: val}]}
                     for pid, sku, val in items[i:i + 20]]
            res = self.call(method, mutiple_product_update_list=chunk)
            for f in as_list(res.get("update_failed_list")):
                failed[int(f.get("product_id"))] = f.get("error_message") or f.get("error_code")
            if res.get("update_success") is False and not res.get("update_failed_list"):
                for pid, _, _ in items[i:i + 20]:
                    failed[int(pid)] = res.get("update_error_message") or res.get("update_error_code")
        return failed

    def products(self, status):
        out, page = [], 1
        while True:
            res = (self.call("aliexpress.solution.product.list.get",
                             aeop_a_e_product_list_query={"product_status_type": status, "current_page": page,
                                                          "page_size": 50}).get("result") or {})
            out += as_list(res.get("aeop_a_e_product_display_d_t_o_list"))
            if page >= int(res.get("total_page") or 1):
                return out
            page += 1


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


def normalize(raw):
    images = raw.get("images") or ([raw["image"]] if raw.get("image") else [])
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:50],
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
        probs.append(f"condition '{p['condition']}' not sold on AliExpress (ALLOWED_CONDITIONS)")
    if len(p["title"]) < 3:
        probs.append("no title")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def description_html(p):
    paras = [html.escape(x.strip()) for x in (p["description"] or p["title"]).split("\n") if x.strip()]
    if p["ean"]:
        paras.append(f"ISBN: {p['ean']}")
    return "".join(f"<p>{x}</p>" for x in paras)


def product_payload(p, a, ctx, match, images):
    """Build the aliexpress.solution.product.post request for one book."""
    attrs = []
    if match:
        attrs = [{"aliexpress_attribute_name_id": x.get("property_id"),
                  "aliexpress_attribute_value_id": x.get("property_value_id")}
                 for x in as_list(match.get("product_properties")) if x.get("property_id")]
    have = {x.get("aliexpress_attribute_name_id") for x in attrs}
    attrs += [x for x in a.extra_attributes if x.get("aliexpress_attribute_name_id") not in have or
              not x.get("aliexpress_attribute_name_id")]
    title = p["title"] or next((t.get("text") for t in as_list((match or {}).get("title_list"))
                                if t.get("language") == LANGUAGE), "")
    body = {
        "language": LANGUAGE,
        "aliexpress_category_id": int(match["leaf_cate_id"]) if match else (a.category or None),
        "multi_language_subject_list": [{"language": LANGUAGE, "subject": title[:218]}],
        "multi_language_description_list": [{
            "language": LANGUAGE,
            "web_detail": {"version": "2.0.0", "moduleList": [{"type": "html", "html": {"content": description_html(p)}}]},
        }],
        "main_image_urls_list": images,
        "attribute_list": attrs,
        "sku_info_list": [{
            "sku_code": p["sku"],
            "price": money(p["price"], a.price_factor),
            "inventory": max(1, min(int(p["quantity"] or a.quantity), 999999)),
            "ean_code": p["ean"],
        }],
        "inventory_deduction_strategy": "payment_success_deduct",
        "freight_template_id": ctx.get("freight_template_id"),
        "shipping_lead_time": a.lead_days,
        "weight": f"{float(p['weight'] or DEFAULT_WEIGHT_KG):.3f}",
        "package_length": PACKAGE_CM[0],
        "package_width": PACKAGE_CM[1],
        "package_height": PACKAGE_CM[2],
        "product_unit": 100000015,                       # piece/pieces
    }
    if ctx.get("msr_eu_id"):
        body["msr_eu_id"] = ctx["msr_eu_id"]
    if ctx.get("manufacturer_id"):
        body["manufacturer_id"] = ctx["manufacturer_id"]
    return body


def shop_context(api, a, quiet=False):
    """Freight template, EU responsible person and manufacturer to use for new products."""
    ctx = {"freight_template_id": a.freight_template or None, "msr_eu_id": MSR_EU_ID or None,
           "manufacturer_id": MANUFACTURER_ID or None}
    if api is None:
        return ctx
    if not ctx["freight_template_id"]:
        tpls = api.freight_templates()
        pick = next((t for t in tpls if t.get("is_default")), tpls[0] if tpls else None)
        if pick:
            ctx["freight_template_id"] = pick.get("template_id")
        elif not quiet:
            print("   ! no shipping template found - create one in Seller Center (freighttemplate.aliexpress.com)")
    if not ctx["msr_eu_id"]:
        msr = api.msr_list()
        if msr:
            ctx["msr_eu_id"] = msr[0].get("id")
            if len(msr) > 1 and not quiet:
                print(f"   (several EU responsible persons - using '{msr[0].get('name')}'; set MSR_EU_ID to choose)")
        elif not quiet:
            print("   ! no EU responsible person (GPSR) set up in Seller Center - AliExpress will reject EU listings")
    if not ctx["manufacturer_id"]:
        man = api.manufacturers()
        if len(man) == 1:
            ctx["manufacturer_id"] = man[0].get("id")
    return ctx


def fetch_images(api, p, cache):
    """Upload the book's own photos to the AliExpress photo bank (cached by source URL)."""
    out = []
    for url in p["images"]:
        if url not in cache:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            name = (url.rsplit("/", 1)[-1].split("?")[0] or "image.jpg")[-100:]
            cache[url] = api.upload_image(name, r.content)
            save_json(IMAGE_CACHE, cache)
        out.append(cache[url])
    return out


# --------------------------------------------------------------------- commands

def cmd_auth_url(a):
    key = os.environ.get("ALIEXPRESS_APP_KEY")
    if not key:
        sys.exit("Missing environment variables: ALIEXPRESS_APP_KEY")
    print("Open this URL, log in with the AliExpress SELLER account and authorize the app:\n")
    print(f"{AUTH_URL}?{urlencode({'response_type': 'code', 'force_auth': 'true', 'redirect_uri': a.redirect, 'client_id': key})}\n")
    print("You'll be sent to your redirect URL with ?code=... (valid 30 minutes). Then run:\n"
          "    python aliexpress_push.py token <code>")


def cmd_token(api, a):
    try:
        tok = api.save_token(api.system("/auth/token/create", code=a.code))
    except ApiError as e:
        sys.exit(f"Could not create the token: {e}")
    exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(tok["expire_time"] / 1000))
    rexp = time.strftime("%Y-%m-%d %H:%M", time.localtime(tok["refresh_token_valid_time"] / 1000))
    print(f"Authorized{' for ' + str(tok['seller']) if tok.get('seller') else ''}. Tokens saved to {TOKEN_FILE.resolve()}")
    print(f"   access token valid until {exp}, refresh token until {rexp} (refreshed automatically).")
    print("   On a server: store refresh_token from that file in Bitwarden as ALIEXPRESS_REFRESH_TOKEN.")


def cmd_check(api, a):
    prof = api.profile()
    print(f"Connected to shop '{prof.get('shop_name')}' (id {prof.get('shop_id')}, country {prof.get('country_code')}, "
          f"type {prof.get('shop_type')})")
    if prof.get("product_posting_forbidden") in (True, "true"):
        print("   ! this shop is NOT allowed to post products - check Seller Center")
    tpls = api.freight_templates()
    print("Shipping templates:" + ("" if tpls else " none - create one in Seller Center"))
    for t in tpls:
        print(f"   {t.get('template_id'):<12} {t.get('template_name')}" + ("  (default)" if t.get("is_default") else ""))
    msr = api.msr_list()
    print("EU responsible persons (GPSR):" + ("" if msr else " none - required for EU listings, add one in Seller Center"))
    for m in msr:
        print(f"   {m.get('id'):<12} {m.get('name')} ({m.get('country')})")
    man = api.manufacturers()
    if man:
        print("Manufacturers: " + ", ".join(f"{m.get('id')} {m.get('name')}" for m in man))


def cmd_ean(api, a):
    ean = to_ean(a.isbn)
    if not ean:
        sys.exit(f"Not a valid ISBN/EAN: {a.isbn}")
    m = api.ean_info(ean)
    if not m:
        print(f"{ean}: not in AliExpress's EAN database (CATEGORY_ID and your own photos would be used)")
        return
    title = next((t.get("text") for t in as_list(m.get("title_list")) if t.get("language") == LANGUAGE),
                 next((t.get("text") for t in as_list(m.get("title_list"))), ""))
    print(f"{ean}: category {m.get('leaf_cate_id')}  title: {title}")
    print(f"   {len(as_list(m.get('main_images')))} image(s), attributes: "
          + ", ".join(f"{x.get('property_name')}={x.get('value_name')}" for x in as_list(m.get("product_properties"))))


def cmd_categories(api, a):
    cats = api.categories(a.parent)
    for c in cats:
        names = c.get("multi_language_names")
        try:
            names = json.loads(names) if isinstance(names, str) else (names or {})
        except ValueError:
            names = {"en": names}
        label = names.get(LANGUAGE) or names.get("en") or str(names)
        if a.find and a.find.lower() not in json.dumps(names, ensure_ascii=False).lower():
            continue
        print(f"   {c.get('children_category_id'):<12} {'(leaf) ' if c.get('is_leaf_category') else '       '}{label}")
    if not cats:
        print("No categories (check that the seller has category permissions).")
    else:
        print("Leaf categories can be used as CATEGORY_ID. Open others with --parent <id>.")


def cmd_attributes(api, a):
    attrs = [x for x in api.attributes(a.category_id) if x.get("visible") is not False]
    if not attrs:
        print("No attributes returned for that category.")
    for x in sorted(attrs, key=lambda x: (not x.get("required"), not x.get("key_attribute"))):
        flag = "REQUIRED" if x.get("required") else ("key" if x.get("key_attribute") else "")
        vals = as_list(x.get("values"))
        print(f"   {x.get('id'):<10} {flag:<9} {x.get('names')}" + ("  [SKU attribute]" if x.get("sku") else "")
              + (f"  ({x.get('attribute_show_type_value')})" if not vals else ""))
        for v in vals[:12]:
            print(f"        {v.get('id'):<12} {v.get('names')}")
        if len(vals) > 12:
            print(f"        ... {len(vals) - 12} more")
    print('Add required ones to EXTRA_ATTRIBUTES as {"aliexpress_attribute_name_id": <id>, '
          '"aliexpress_attribute_value_id": <value id>}, or pass --attribute <id>=<value id>.')


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
        if p["sold_out"] and a.offline_sold_out and p["sku"] in state and state[p["sku"]].get("status") != "offline":
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

    ctx = shop_context(api, a) if new else {}
    matches = {}
    if new and api and not a.no_ean_lookup:
        print(f"Looking up {len(new)} ISBN(s) in AliExpress's EAN database...")
        for p in new:
            try:
                matches[p["sku"]] = api.ean_info(p["ean"])
            except ApiError as e:
                print(f"   ! {p['sku']}: EAN lookup failed ({e})")
                matches[p["sku"]] = None
    elif new and not api:
        print("(dry run without credentials: EAN lookup, shipping template and GPSR lookup skipped)")

    ready = []
    for p in new:
        m = matches.get(p["sku"])
        if not m and not a.category:
            print(f"   SKIP '{p['title']}': EAN {p['ean']} not in AliExpress's EAN database and no CATEGORY_ID set "
                  f"(see `categories`)" if api and not a.no_ean_lookup else
                  f"   SKIP '{p['title']}': no CATEGORY_ID set (see `categories`, or use the EAN lookup)")
            continue
        if not m and not p["images"]:
            print(f"   SKIP '{p['title']}': no images (AliExpress doesn't know this ISBN, so your own photos are needed)")
            continue
        ready.append((p, m))

    print(f"{len(ready)} new product(s)" + (f", {len(resend)} to update" if resend else "")
          + (f", {len(hide)} sold-out to take offline" if hide else "") + ".")
    preview = [product_payload(p, a, ctx, m, as_list((m or {}).get("main_images"))[:MAX_IMAGES] or p["images"])
               for p, m in ready]
    if not (ready or resend or hide):
        return
    save_json(PREVIEW_FILE, preview)
    if a.dry_run:
        print(f"Dry run: products written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(ready) + len(resend) + len(hide)} LIVE product(s) on AliExpress. "
                 "Type 'yes': ").strip().lower() != "yes":
            return
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")

    # --- new products
    cache = load_json(IMAGE_CACHE, {})
    created = []
    for p, m in ready:
        try:
            images = as_list(m.get("main_images"))[:MAX_IMAGES] if m else []
            if not images:
                images = fetch_images(api, p, cache)
            body = product_payload(p, a, ctx, m, images)
            pid = api.post_product(body)
        except (ApiError, requests.RequestException) as e:
            print(f"   ! {p['sku']}: {e}")
            continue
        sku = body["sku_info_list"][0]
        state[p["sku"]] = {"product_id": pid, "ean": p["ean"], "price": sku["price"], "inventory": sku["inventory"],
                           "category": body["aliexpress_category_id"], "status": "offline", "pushed_at": now()}
        save_json(STATE_FILE, state)
        created.append(p["sku"])
        print(f"   + {p['sku']}: product {pid}  {sku['price']} EUR x{sku['inventory']}"
              + ("  (category/attributes from AliExpress EAN data)" if m else ""))
    if created and not a.no_online:
        failed = api.set_online([state[s]["product_id"] for s in created])
        for s in created:
            pid = state[s]["product_id"]
            if pid in failed:
                print(f"   ! {s}: created but not put online: {failed[pid]} (fix it in Seller Center or run `status`)")
            else:
                state[s]["status"] = "online"
        save_json(STATE_FILE, state)

    # --- price / stock sync
    if resend:
        prices = [(state[p["sku"]]["product_id"], p["sku"], money(p["price"], a.price_factor)) for p in resend]
        stock = [(state[p["sku"]]["product_id"], p["sku"], max(1, int(p["quantity"] or a.quantity))) for p in resend]
        bad = {**api.batch_update("price", prices), **api.batch_update("inventory", stock)}
        back = []
        for (pid, sku, price), (_, _, qty) in zip(prices, stock):
            if pid in bad:
                print(f"   ! {sku}: update failed: {bad[pid]}")
                continue
            state[sku].update(price=price, inventory=qty, updated_at=now())
            if state[sku].get("status") == "offline":
                back.append(sku)
            print(f"   ~ {sku}: {price} EUR x{qty}")
        if back:
            failed = api.set_online([state[s]["product_id"] for s in back])
            for s in back:
                if state[s]["product_id"] not in failed:
                    state[s]["status"] = "online"
                    print(f"   ^ {s}: back online")
        save_json(STATE_FILE, state)

    # --- sold out -> offline
    if hide:
        failed = api.set_online([state[p["sku"]]["product_id"] for p in hide], online=False)
        for p in hide:
            pid = state[p["sku"]]["product_id"]
            if pid in failed:
                print(f"   ! {p['sku']}: could not take offline: {failed[pid]}")
            else:
                state[p["sku"]].update(status="offline", updated_at=now())
                print(f"   - {p['sku']}: sold out, taken offline")
        save_json(STATE_FILE, state)
    print(f"Done: {len(created)} created. New products may be reviewed by AliExpress first - run `status`.")


def cmd_status(api, a):
    state = load_json(STATE_FILE, {})
    seen = {}
    for st in STATUSES:
        for prod in api.products(st):
            seen[int(prod.get("product_id"))] = (st, prod)
    if not state and not seen:
        print("No products yet.")
        return
    mine = set()
    for sku, info in sorted(state.items()):
        pid = int(info["product_id"])
        mine.add(pid)
        if pid not in seen:
            print(f"{sku}: product {pid} not found on AliExpress (deleted?)")
            continue
        st, prod = seen[pid]
        label = {"onSelling": "ONLINE", "offline": "offline", "auditing": "in review",
                 "editingRequired": "NEEDS EDITING"}[st]
        reason = f"  reason={prod.get('ws_display')}" if st == "offline" and prod.get("ws_display") else ""
        print(f"{sku}: {label}  product={pid}  price={prod.get('product_min_price')} {prod.get('currency_code') or ''}{reason}")
    for pid, (st, prod) in seen.items():
        if pid not in mine:
            print(f"(product {pid} '{(prod.get('subject') or '')[:40]}': {st}, not pushed by this script)")


def parse_attribute(s):
    try:
        name, value = s.split("=", 1)
        return {"aliexpress_attribute_name_id": int(name), "aliexpress_attribute_value_id": int(value)}
    except ValueError:
        raise argparse.ArgumentTypeError("use --attribute <attribute id>=<value id>, e.g. 2=201512802")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push products to AliExpress (Open Platform API)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("auth-url", help="print the URL the seller opens to authorize this app")
    s.add_argument("--redirect", required=True, help="your app's callback URL (as registered in the app console)")
    s = sub.add_parser("token", help="exchange the authorization code for tokens")
    s.add_argument("code")
    sub.add_parser("check", help="shop info, shipping templates, GPSR responsible persons")
    s = sub.add_parser("ean", help="look up an ISBN in AliExpress's EAN database")
    s.add_argument("isbn")
    s = sub.add_parser("categories", help="browse the category tree")
    s.add_argument("--parent", type=int, default=0, help="parent category id (0 = top level)")
    s.add_argument("--find", help="only show categories whose name contains this text")
    s = sub.add_parser("attributes", help="list the attributes of a leaf category")
    s.add_argument("category_id", type=int)
    p = sub.add_parser("push", help="create products from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per product (per-item 'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--category", type=int, default=CATEGORY_ID, help="category for books the EAN database doesn't know")
    p.add_argument("--attribute", type=parse_attribute, action="append", default=[], metavar="ID=VALUE_ID",
                   help="extra category attribute (repeatable), added to EXTRA_ATTRIBUTES")
    p.add_argument("--freight-template", type=int, default=FREIGHT_TEMPLATE_ID, help="shipping template id (0 = default)")
    p.add_argument("--lead-days", type=int, default=SHIPPING_LEAD_DAYS, help="days until you ship")
    p.add_argument("--resend", action="store_true", help="update price and stock of already pushed products")
    p.add_argument("--offline-sold-out", action="store_true", help="take pushed products that are now sold out offline")
    p.add_argument("--no-ean-lookup", action="store_true", help="don't use AliExpress's EAN database")
    p.add_argument("--no-online", action="store_true", help="create products but leave them offline")
    p.add_argument("--dry-run", action="store_true", help="write aliexpress_products.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new products this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="list your products and whether they are online")
    a = ap.parse_args()
    if a.cmd == "auth-url":
        return cmd_auth_url(a)
    if a.cmd == "push":
        a.extra_attributes = list(EXTRA_ATTRIBUTES) + a.attribute
        creds = all(os.environ.get(k) for k in ("ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET"))
        authorized = TOKEN_FILE.exists() or os.environ.get("ALIEXPRESS_REFRESH_TOKEN")
        if a.dry_run and not (creds and authorized):
            return cmd_push(None, a)             # no credentials needed to build the preview
    api = AliExpress()
    try:
        {"token": cmd_token, "check": cmd_check, "ean": cmd_ean, "categories": cmd_categories,
         "attributes": cmd_attributes, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except ApiError as e:
        sys.exit(f"AliExpress API error {e}")


if __name__ == "__main__":
    main()
