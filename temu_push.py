#!/usr/bin/env python3
"""
Push products to Temu as a LOCAL seller in Europe (e.g. temu.com/es) through
the Temu Open Platform "Partner API" (bg.local.goods.* calls,
EU gateway https://openapi-b-eu.temu.com/openapi/router).

Like AliExpress, Temu has no catalog to attach an offer to: every book
becomes its own product (title, description, images, category attributes,
variation, shipping template, weight and size). All books go into one Temu
category (CATEGORY_ID; find it with `categories` or `recommend`). The
category's required attributes are read from Temu (bg.local.goods.template.get):
an ISBN attribute is filled in automatically, others come from PROPERTIES.

ONE-TIME SETUP (see docs/temu_push.md)
  1. partner-eu.temu.com: create a Partner Platform account as a
     "Self-developed applications seller", link your Seller Center account,
     fill in the app information and questionnaires, wait for approval.
     You get an App Key and App Secret, and you register your server's IP.
  2. Seller Center (seller-eu.temu.com) -> Open platform -> Authorization
     management -> authorize the app -> copy the Access Token.
  3. Seller Center: create a shipping template.
  Credentials (PowerShell or Bitwarden):
      $env:TEMU_APP_KEY = "..."
      $env:TEMU_APP_SECRET = "..."
      $env:TEMU_ACCESS_TOKEN = "..."

USAGE
    pip install requests
    python temu_push.py check                       # token, shop, permissions, shipping templates
    python temu_push.py categories --find book      # find the books category
    python temu_push.py recommend "Dragona. Libro para bebes"   # Temu's suggested category
    python temu_push.py attributes <category_id>    # required attributes and variations
    python temu_push.py push --dry-run              # build the products, send nothing
    python temu_push.py push --limit 1              # one real product
    python temu_push.py push                        # everything
    python temu_push.py push --resend --offline-sold-out   # sync price/stock, hide sold-out
    python temu_push.py status                      # on sale / in review / off sale

NOTE: new books in Spain have a legally fixed price (Ley 10/2007, max 5%
discount). Temu reviews prices and may propose a lower one - don't accept a
price below the legal minimum in Seller Center.
"""
import argparse
import hashlib
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

REGION = "eu"                # Temu shop region: eu (Spain, Germany, ...), us or global
CURRENCY = "EUR"             # currency of your Temu site (Spain: EUR)
LANGUAGE = "es"              # language for category/attribute names in `categories` / `attributes`

# Temu leaf category for all books (see `categories --find book` or `recommend`). 0 = not set.
CATEGORY_ID = 0
# Required category attributes that can't be filled automatically. Get the
# values with `attributes <category_id>`; each entry is pasted from there, e.g.
#   {"pid": 1, "templatePid": 937018, "refPid": 12, "vid": 381, "value": "Español"}
PROPERTIES = []
# Variation (every Temu SKU needs one). [] = automatic: a custom value named
# SPEC_NAME is created under the category's first custom variation type.
SPEC_IDS = []
SPEC_NAME = "Standard"

# Temu local listings are new goods unless your shop is approved as second-hand.
ALLOWED_CONDITIONS = {"NewCondition"}

FREIGHT_TEMPLATE_ID = ""     # "" = your first shipping template (see `check`)
SHIPMENT_LIMIT_DAYS = 2      # days from order to shipment (Temu default 1-2)
DEFAULT_WEIGHT_G = 400       # parcel weight when the product has no "weight_kg"
PACKAGE_CM = (24, 17, 3)     # length, width, height in cm
MAX_IMAGES = 10
# ======================================================================

HOSTS = {"eu": "https://openapi-b-eu.temu.com/openapi/router",
         "us": "https://openapi-b-us.temu.com/openapi/router",
         "global": "https://openapi-b-global.temu.com/openapi/router"}
STATE_FILE = Path("temu_posted.json")
PREVIEW_FILE = Path("temu_products.json")
CACHE_FILE = Path("temu_cache.json")          # uploaded images and generated variation ids
RETRY_CODES = {4000004, 4000000, 150010005, 150011021}   # rate limit, system busy, "try again later"
TOKEN_CODES = {3000030, 3000031, 3000033, 3000034, 5000000}
GOODS_NAME_BAD = re.compile(r"[~!*$?_{}#<>|;^¬¦®©™]")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code, self.msg = code, msg


# --------------------------------------------------------------------------- API

def sign(secret, params):
    """Temu Open Platform signature (MD5): app_secret + sorted key+value pairs + app_secret, upper hex.
    Values are compact JSON with surrounding quotes stripped, exactly like Temu's Python example."""
    parts = []
    for k, v in sorted(params.items()):
        parts.append(str(k) + json.dumps(v, ensure_ascii=False, separators=(",", ":")).strip('"'))
    return hashlib.md5((secret + "".join(parts) + secret).encode("utf-8")).hexdigest().upper()


class Temu:
    def __init__(self):
        self.key = os.environ.get("TEMU_APP_KEY")
        self.secret = os.environ.get("TEMU_APP_SECRET")
        self.token = os.environ.get("TEMU_ACCESS_TOKEN")
        missing = [k for k, v in (("TEMU_APP_KEY", self.key), ("TEMU_APP_SECRET", self.secret),
                                  ("TEMU_ACCESS_TOKEN", self.token)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.url = os.environ.get("TEMU_API_URL") or HOSTS[REGION]

    def call(self, api_type, retries=5, **params):
        for attempt in range(retries):
            body = dict(params, type=api_type, app_key=self.key, access_token=self.token,
                        timestamp=int(time.time()), data_type="JSON")
            body["sign"] = sign(self.secret, body)
            r = requests.post(self.url, data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                              headers={"Content-Type": "application/json"}, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                print(f"   (API returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                data = r.json()
            except ValueError:
                raise ApiError(r.status_code, r.text[:300])
            if data.get("success"):
                return data.get("result") or {}
            code = data.get("errorCode")
            msg = data.get("errorMsg") or "error"
            if code in RETRY_CODES:
                wait = 2 ** attempt
                print(f"   (Temu: {msg}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            if code in TOKEN_CODES:
                sys.exit(f"Temu rejected the access token ({code} {msg}). Authorize the app again in Seller Center "
                         "and update TEMU_ACCESS_TOKEN.")
            if code == 3000001:
                sys.exit(f"Temu rejected the signature ({msg}). Check TEMU_APP_SECRET.")
            if code == 5000003:
                sys.exit(f"Temu: {msg}. Add this computer's public IP address to the app in partner-eu.temu.com.")
            raise ApiError(code, msg)
        raise ApiError("retries", "gave up after retries")

    # ---- endpoints
    def token_info(self):
        return self.call("bg.open.accesstoken.info.get")

    def freight_templates(self):
        return self.call("bg.freight.template.list.query").get("templateList") or []

    def categories(self, parent):
        return self.call("bg.local.goods.cats.get", parentCatId=parent, language=LANGUAGE).get("goodsCatsList") or []

    def recommend(self, name, description=""):
        return self.call("bg.local.goods.category.recommend", goodsName=name, description=description[:500])

    def template(self, cat_id):
        return self.call("bg.local.goods.template.get", catId=cat_id, language=LANGUAGE)

    def spec_id(self, cat_id, parent_spec_id, name):
        return self.call("bg.local.goods.spec.id.get", catId=cat_id, parentSpecId=parent_spec_id,
                         childSpecName=name).get("specId")

    def upload_image(self, url):
        res = self.call("bg.local.goods.image.upload", fileUrl=url)
        if not res.get("url"):
            raise ApiError("image", f"no url returned for {url}")
        return res["url"]

    def add(self, goods):
        return self.call("bg.local.goods.add", **goods)

    def set_stock(self, goods_id, sku_id, qty):
        return self.call("bg.local.goods.stock.edit", goodsId=goods_id,
                         skuStockTargetList=[{"skuId": sku_id, "stockTarget": qty}])

    def change_price(self, goods_id, sku_id, amount):
        res = self.call("bg.local.goods.priceorder.change.sku.price", goodsId=goods_id, changeSkuPriceDTOList=[{
            "reason": "price update", "skuChangePriceBaseDTOList": [
                {"skuId": sku_id, "newSupplierPrice": {"amount": amount, "currency": CURRENCY}}]}])
        failed = res.get("failedSkuReasonMap") or {}
        if failed or sku_id in (res.get("failedSkuList") or []):
            raise ApiError("price", "; ".join(f"{k}: {v}" for k, v in failed.items()) or "price change failed")
        return res

    def set_on_sale(self, goods_id, on):
        return self.call("bg.local.goods.sale.status.set", goodsId=goods_id, onsale=1 if on else 0)

    def goods(self, goods_ids):
        out = []
        for i in range(0, len(goods_ids), 50):
            res = self.call("bg.local.goods.list.query", goodsIdList=goods_ids[i:i + 50], pageNo=1, pageSize=50)
            out += res.get("goodsList") or []
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


def clean_name(title):
    """Temu product names may not contain decorative characters (~ ! * $ ? _ { } # < > | ; ^ and ® © ™)."""
    return re.sub(r"\s+", " ", GOODS_NAME_BAD.sub(" ", title)).strip()[:500]


def normalize(raw):
    images = raw.get("images") or ([raw["image"]] if raw.get("image") else [])
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:100],
        "title": (raw.get("title") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "quantity": raw.get("quantity"),
        "weight_g": round(float(raw["weight_kg"]) * 1000, 1) if raw.get("weight_kg") else DEFAULT_WEIGHT_G,
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
        probs.append(f"condition '{p['condition']}' not sold on Temu (ALLOWED_CONDITIONS)")
    if len(clean_name(p["title"])) < 3:
        probs.append("no title")
    if not p["images"]:
        probs.append("no images")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def describe(p):
    text = p["description"] or p["title"]
    return (text + (f"\nISBN: {p['ean']}" if p["ean"] else ""))[:2000]


class CategoryPlan:
    """What a category needs: required attributes (filled from PROPERTIES or the ISBN) and the variation."""

    def __init__(self, api, cat_id, cache, offline=False):
        self.cat_id = cat_id
        self.missing = []                          # names of required attributes we can't fill
        self.isbn_props = []                       # property templates filled with the book's ISBN
        self.spec_ids = list(SPEC_IDS)
        if offline:
            self.spec_ids = self.spec_ids or ["<variation id>"]
            return
        tpl = api.template(cat_id)
        info = tpl.get("templateInfo") or {}
        given = {(x.get("templatePid") or x.get("pid")) for x in PROPERTIES}
        for prop in info.get("goodsProperties") or []:
            if not prop.get("required") or prop.get("isSale"):
                continue
            if (prop.get("templatePid") or prop.get("pid")) in given:
                continue
            if "isbn" in str(prop.get("name", "")).lower() and prop.get("controlType") in (0, 16, 19, None):
                self.isbn_props.append(prop)
            else:
                self.missing.append(f"{prop.get('name')} (templatePid {prop.get('templatePid')})")
        if not self.spec_ids:
            key = f"spec:{cat_id}:{SPEC_NAME}"
            if key in cache:
                self.spec_ids = [cache[key]]
            else:
                parents = tpl.get("userInputParentSpecList") or []
                fixed = [v for prop in info.get("goodsSpecProperties") or [] if prop.get("required")
                         for v in (prop.get("values") or [])[:1] if v.get("specId")]
                if parents:
                    sid = api.spec_id(cat_id, parents[0].get("parentSpecId"), SPEC_NAME)
                    cache[key] = sid
                    save_json(CACHE_FILE, cache)
                    self.spec_ids = [sid]
                elif fixed:
                    self.spec_ids = [fixed[0]["specId"]]
        if not self.spec_ids:
            self.missing.append("variation (set SPEC_IDS, see `attributes`)")

    def properties(self, p):
        props = [dict(x) for x in PROPERTIES]
        for prop in self.isbn_props:
            props.append({"pid": prop.get("pid"), "templatePid": prop.get("templatePid"),
                          "refPid": prop.get("refPid"), "value": p["ean"]})
        return props


def goods_payload(p, a, plan, freight_id, images):
    return {
        "goodsBasic": {"goodsName": clean_name(p["title"]), "catId": plan.cat_id, "outGoodsSn": p["sku"]},
        "goodsServicePromise": {"shipmentLimitDay": a.shipment_days, "fulfillmentType": 1,
                                "costTemplateId": freight_id},
        "goodsProperty": {"goodsProperties": plan.properties(p)},
        "goodsDesc": describe(p),
        "skuList": [{
            "outSkuSn": p["sku"],
            "specIdList": plan.spec_ids,
            "price": {"listPriceType": 1, "basePrice": {"amount": money(p["price"], a.price_factor), "currency": CURRENCY}},
            "quantity": max(0, min(int(p["quantity"] or a.quantity), 999999)),
            "weight": f"{float(p['weight_g']):.1f}", "weightUnit": "g",
            "length": f"{PACKAGE_CM[0]:.1f}", "width": f"{PACKAGE_CM[1]:.1f}", "height": f"{PACKAGE_CM[2]:.1f}",
            "volumeUnit": "cm",
            "images": images,
            "externalProductType": 3,          # 3 = ISBN
            "externalProductId": p["ean"],
        }],
    }


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    info = api.token_info()
    exp = info.get("expiredTime")
    exp_s = time.strftime("%Y-%m-%d", time.localtime(exp / 1000 if exp and exp > 1e11 else exp)) if exp else "?"
    print(f"Token accepted: shop (mallId) {info.get('mallId')}, region {info.get('regionId')}, "
          f"token expires {exp_s}")
    scopes = info.get("apiScopeList") or []
    need = ["bg.local.goods.add", "bg.local.goods.image.upload", "bg.local.goods.stock.edit",
            "bg.local.goods.sale.status.set", "bg.local.goods.list.query"]
    lacking = [s for s in need if scopes and s not in scopes]
    print(f"   {len(scopes)} API permission(s)" + (f"; MISSING: {', '.join(lacking)} - re-authorize with them"
                                                    if lacking else ""))
    tpls = api.freight_templates()
    print("Shipping templates:" + ("" if tpls else " none - create one in Seller Center"))
    for t in tpls:
        print(f"   {t.get('templateId')}  {t.get('templateName')}")
    if not CATEGORY_ID:
        print("CATEGORY_ID is not set yet - see `categories --find book` or `recommend \"<title>\"`.")


def cmd_categories(api, a):
    cats = api.categories(a.parent)
    shown = 0
    for c in cats:
        if a.find and a.find.lower() not in str(c.get("catName", "")).lower():
            continue
        flags = ("(leaf) " if c.get("leaf") else "       ") + ("" if c.get("availableStatus") in (0, None) else "[not sellable] ")
        print(f"   {c.get('catId'):<10} {flags}{c.get('catName')}")
        shown += 1
    if not cats:
        print("No categories returned.")
    elif not shown:
        print(f"Nothing matching '{a.find}' at this level - open a category with --parent <id>.")
    else:
        print("Leaf categories can be used as CATEGORY_ID. Open others with --parent <id>.")


def cmd_recommend(api, a):
    res = api.recommend(a.title)
    ids = res.get("catIdList") or ([res["catId"]] if res.get("catId") else [])
    print(f"Temu suggests category {res.get('catId')}" + (f" (also: {', '.join(map(str, ids[1:]))})" if len(ids) > 1 else ""))
    print("Check it with `attributes <id>` before setting CATEGORY_ID.")


def cmd_attributes(api, a):
    tpl = api.template(a.category_id)
    info = tpl.get("templateInfo") or {}
    props = [x for x in info.get("goodsProperties") or [] if not x.get("isSale")]
    print(f"Attributes of category {a.category_id} (required first):")
    for x in sorted(props, key=lambda x: not x.get("required")):
        base = {"pid": x.get("pid"), "templatePid": x.get("templatePid"), "refPid": x.get("refPid")}
        kind = "REQUIRED" if x.get("required") else "optional"
        auto = "  -> filled with the ISBN automatically" if "isbn" in str(x.get("name", "")).lower() else ""
        print(f"   {kind:<9} {x.get('name')}  (controlType {x.get('controlType')}){auto}")
        vals = x.get("values") or []
        for v in vals[:10]:
            print(f"        {json.dumps(dict(base, vid=v.get('vid'), value=v.get('value')), ensure_ascii=False)}")
        if len(vals) > 10:
            print(f"        ... {len(vals) - 10} more")
        if not vals and not auto:
            print(f"        {json.dumps(dict(base, value='<your text>'), ensure_ascii=False)}")
    parents = tpl.get("userInputParentSpecList") or []
    spec_props = info.get("goodsSpecProperties") or []
    print("Variation: " + (f"custom values allowed under {', '.join(str(s.get('parentSpecName')) for s in parents)} "
                           f"-> SPEC_NAME '{SPEC_NAME}' is created automatically" if parents else
                           "only preset values: " + ", ".join(f"{v.get('value')}={v.get('specId')}" for prop in spec_props
                                                              for v in (prop.get('values') or [])[:5])))
    print("Paste the lines you need into PROPERTIES at the top of temu_push.py.")


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
        if p["sold_out"] and a.offline_sold_out and p["sku"] in state and state[p["sku"]].get("on_sale", True):
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

    plan, freight = None, a.freight_template or "<shipping template>"
    cache = load_json(CACHE_FILE, {})
    if new:
        if not a.category and not a.dry_run:
            sys.exit("CATEGORY_ID is not set. Find the books category with `categories --find book` or "
                     "`recommend \"<title>\"`, then set CATEGORY_ID (or pass --category).")
        if not a.category:
            print("(CATEGORY_ID not set: the preview uses a placeholder category)")
            plan = CategoryPlan(None, "<CATEGORY_ID>", cache, offline=True)
        elif api:
            if not a.freight_template:
                tpls = api.freight_templates()
                if not tpls:
                    sys.exit("No shipping template in Seller Center - create one first (see `check`).")
                freight = tpls[0].get("templateId")
            plan = CategoryPlan(api, a.category, cache)
            if plan.missing:
                sys.exit(f"Category {a.category} needs: {'; '.join(plan.missing)}.\n"
                         f"Run `attributes {a.category}` and add them to PROPERTIES / SPEC_IDS.")
        else:
            print("(dry run without credentials: category template, shipping template and images not looked up)")
            plan = CategoryPlan(None, a.category, cache, offline=True)

    print(f"{len(new)} new product(s)" + (f", {len(resend)} to update" if resend else "")
          + (f", {len(hide)} sold-out to take off sale" if hide else "") + ".")
    if not (new or resend or hide):
        return
    save_json(PREVIEW_FILE, [goods_payload(p, a, plan, freight, p["images"]) for p in new])
    if a.dry_run:
        print(f"Dry run: products written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(new) + len(resend) + len(hide)} LIVE product(s) on Temu. "
                 "Type 'yes': ").strip().lower() != "yes":
            return
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")

    created = 0
    for p in new:
        try:
            images = []
            for url in p["images"]:
                if url not in cache:
                    cache[url] = api.upload_image(url)
                    save_json(CACHE_FILE, cache)
                images.append(cache[url])
            body = goods_payload(p, a, plan, freight, images)
            res = api.add(body)
        except ApiError as e:
            print(f"   ! {p['sku']}: {e}")
            continue
        sku_info = (res.get("skuInfoList") or [{}])[0]
        sku = body["skuList"][0]
        state[p["sku"]] = {"goods_id": res.get("goodsId"), "sku_id": sku_info.get("skuId"), "ean": p["ean"],
                           "price": sku["price"]["basePrice"]["amount"], "quantity": sku["quantity"],
                           "category": plan.cat_id, "on_sale": True, "pushed_at": now()}
        save_json(STATE_FILE, state)
        created += 1
        warn = "; ".join(w.get("message", "") for w in res.get("warnings") or [] if w.get("message"))
        print(f"   + {p['sku']}: goods {res.get('goodsId')}  {sku['price']['basePrice']['amount']} {CURRENCY} "
              f"x{sku['quantity']}" + (f"  (warning: {warn})" if warn else ""))

    for p in resend:
        info = state[p["sku"]]
        price, qty = money(p["price"], a.price_factor), max(0, int(p["quantity"] or a.quantity))
        try:
            if qty != info.get("quantity"):
                api.set_stock(info["goods_id"], info["sku_id"], qty)
                info["quantity"] = qty
            if price != info.get("price"):
                api.change_price(info["goods_id"], info["sku_id"], price)
                info["price"] = price          # Temu reviews the new price before it shows
            if not info.get("on_sale", True):
                api.set_on_sale(info["goods_id"], True)
                info["on_sale"] = True
                print(f"   ^ {p['sku']}: back on sale")
        except ApiError as e:
            print(f"   ! {p['sku']}: update failed: {e}")
            continue
        info["updated_at"] = now()
        save_json(STATE_FILE, state)
        print(f"   ~ {p['sku']}: {price} {CURRENCY} x{qty}")

    for p in hide:
        info = state[p["sku"]]
        try:
            api.set_on_sale(info["goods_id"], False)
        except ApiError as e:
            print(f"   ! {p['sku']}: could not take off sale: {e}")
            continue
        info.update(on_sale=False, updated_at=now())
        save_json(STATE_FILE, state)
        print(f"   - {p['sku']}: sold out, taken off sale")
    print(f"Done: {created} created. Temu reviews new products and prices first - run `status`.")


STATUS = {2001: "ON SALE", 2002: "ON SALE (needs more information)", 2005: "ON SALE (category to fix)",
          2006: "ON SALE (info to fix)", 2101: "ON SALE (low traffic: price)", 3001: "off sale (penalty)",
          3002: "off sale", 3003: "sold out", 3004: "off sale (price too high)", 4001: "price review",
          4002: "in review", 4003: "PRICE REJECTED", 4004: "NEEDS COMPLETING", 5001: "draft",
          6001: "deleted", 6002: "deleted (price rejected)"}


def cmd_status(api, a):
    state = load_json(STATE_FILE, {})
    ids = [v["goods_id"] for v in state.values() if v.get("goods_id")]
    if not ids:
        print("No products yet.")
        return
    found = {g.get("goodsId"): g for g in api.goods(ids)}
    for sku, info in sorted(state.items()):
        g = found.get(info.get("goods_id"))
        if not g:
            print(f"{sku}: goods {info.get('goods_id')} not found on Temu")
            continue
        sub = g.get("goodsShowSubStatus") or g.get("goodsSubStatus")
        stock = sum(int(s.get("stock") or 0) for s in g.get("skuInfoList") or [])
        print(f"{sku}: {STATUS.get(sub, f'status {sub}')}  goods={g.get('goodsId')}  price={g.get('price')} "
              f"{g.get('currency') or CURRENCY}  stock={stock}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push products to Temu (local seller, Partner API)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="token, permissions and shipping templates")
    s = sub.add_parser("categories", help="browse Temu's categories")
    s.add_argument("--parent", type=int, default=0, help="parent category id (0 = top level)")
    s.add_argument("--find", help="only show categories whose name contains this text")
    s = sub.add_parser("recommend", help="ask Temu which category fits a title")
    s.add_argument("title")
    s = sub.add_parser("attributes", help="required attributes and variations of a category")
    s.add_argument("category_id", type=int)
    p = sub.add_parser("push", help="create products from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per product (per-item 'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--category", type=int, default=CATEGORY_ID, help="Temu leaf category for the books")
    p.add_argument("--freight-template", default=FREIGHT_TEMPLATE_ID, help="shipping template id (default: first)")
    p.add_argument("--shipment-days", type=int, default=SHIPMENT_LIMIT_DAYS, help="days from order to shipment")
    p.add_argument("--resend", action="store_true", help="update stock and price of already pushed products")
    p.add_argument("--offline-sold-out", action="store_true", help="take pushed products that are now sold out off sale")
    p.add_argument("--dry-run", action="store_true", help="write temu_products.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new products this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="review / sale status of your products")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run and not all(os.environ.get(k) for k in ("TEMU_APP_KEY", "TEMU_APP_SECRET",
                                                                              "TEMU_ACCESS_TOKEN")):
        return cmd_push(None, a)                 # no credentials needed to build the preview
    api = Temu()
    try:
        {"check": cmd_check, "categories": cmd_categories, "recommend": cmd_recommend,
         "attributes": cmd_attributes, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except ApiError as e:
        sys.exit(f"Temu API error {e}")


if __name__ == "__main__":
    main()
