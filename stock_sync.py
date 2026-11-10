#!/usr/bin/env python3
"""
Stop double sales: one stock for every marketplace.

The same book is listed on many marketplaces (eBay x11, Kaufland x9, Amazon,
the Mirakl shops, ...), but the source shop has a limited stock (usually 1).
This script:

  1. reads new orders from the marketplaces' order APIs (since the last run):
       Mirakl (Casa del Libro, El Corte Ingles, Carrefour, Worten, IBS.it,
       Empik, E.Leclerc), Amazon (Orders API v2026-01-01), eBay (Fulfillment
       API), Kaufland (order units, every storefront), bol.com, Cdiscount
       (Octopia), Allegro, OTTO, eMAG, CDON, OnBuy, booklooker, Fruugo,
       Vinted Pro, Temu and TikTok Shop;
  2. keeps a ledger of these sales (stock_ledger.json). A cancelled or refused
     order gives its stock back;
  3. computes the stock left per book: the source stock from books.json minus
     our sales that the source shop doesn't reflect yet. A sale counts until a
     scraper refresh that is at least SETTLE_HOURS newer than the sale;
  4. writes books_synced.json (books.json with that stock; sold_out = true at 0);
  5. runs every pusher enabled in push_all_config.json with its sold-out
     option (stock 0, delete, offline, ...) for the books whose stock changed,
     and restores stock when a book is available again.

Credentials are the pushers' own (Bitwarden / .env, like push_all). A channel
without credentials is skipped.

USAGE (with Bitwarden: put "bws run --" in front)
    python stock_sync.py sync                 # read orders, update stock everywhere
    python stock_sync.py sync --dry-run       # read orders, show what would change, change nothing
    python stock_sync.py sync --no-apply      # read orders + write books_synced.json only
    python stock_sync.py status               # sales in the ledger, books with reduced stock

Run it often (e.g. every 15 minutes, see docs/stock_sync.md). push_all.py runs
it first when "stock_sync": {"enabled": true} is set in push_all_config.json.
"""
import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# ============================== SETTINGS ==============================
SETTLE_HOURS = 24          # a sale counts until books.json is this much newer than the sale
LOOKBACK_HOURS = 72        # first run: read orders from this far back
OVERLAP_MINUTES = 60       # re-read this window on every run (late updates, clock skew)
KEEP_DAYS = 60             # forget sales that no longer count after this many days
DEFAULT_STOCK = 1          # stock of a book without a "stock"/"quantity" field
TIMEOUT_MINUTES = 30       # per pusher run
AUTO_ACCEPT = True         # accept orders that must be accepted (Mirakl, Cdiscount, eMAG) when stock covers them
# ======================================================================

DATA_DIR = Path(os.environ.get("STOCK_SYNC_DIR") or BASE)     # tests point this at a temporary folder
LEDGER_FILE = DATA_DIR / "stock_ledger.json"
SYNCED_FILE = DATA_DIR / "books_synced.json"
CHANGES_FILE = DATA_DIR / "books_sync_changes.json"
ORDERS_FILE = DATA_DIR / "orders_report.json"           # for the "New orders" section of push_all's email
MIRAKL_SHOPS = ["casadellibro", "elcorteingles", "carrefour", "worten", "ibs", "empik", "leclerc"]
EBAY_FULFILLMENT_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"

# How each pusher takes a book down when it is sold out (and puts it back with --resend).
SOLD_OUT_FLAGS = {
    **{s: ["--zero-sold-out"] for s in MIRAKL_SHOPS},
    "amazon": ["--zero-sold-out"], "ebay": ["--zero-sold-out"], "kaufland": ["--zero-sold-out"],
    "bol": ["--zero-sold-out"], "cdiscount": ["--zero-sold-out"], "otto": ["--zero-sold-out"],
    "emag": ["--zero-sold-out"], "cdon": ["--zero-sold-out"], "onbuy": ["--zero-sold-out"],
    "abebooks": ["--delete-sold-out"], "booklooker": ["--delete-sold-out"], "allegro": ["--end-sold-out"],
    "aliexpress": ["--offline-sold-out"], "temu": ["--offline-sold-out"], "tiktok": ["--offline-sold-out"],
    "galaxus": [], "fruugo": [],
}
# These restore stock / send changed quantities with --resend.
RESEND = {"abebooks", "aliexpress", "allegro", "bol", "booklooker", "cdiscount", "cdon", "emag", "kaufland",
          "onbuy", "otto", "temu", "tiktok", *MIRAKL_SHOPS}
# Feeds are the full assortment: they get the whole synced file, never a subset.
FULL_FILE = {"galaxus", "fruugo"}


# ----------------------------------------------------------------------- helpers

def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    """ISO 8601 with Z / offset / fractions -> aware datetime (UTC), or None."""
    if not text:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?", str(text))
    if not m:
        return None
    tz = m.group(4) or "Z"
    tz = "+00:00" if tz == "Z" else (tz if ":" in tz else tz[:3] + ":" + tz[3:])
    return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{tz}").astimezone(timezone.utc)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def has_env(*names):
    return all(os.environ.get(n) for n in names)


def sale(channel, key, sku, qty, cancelled, at, ean=None, order=None, needs_accept=False):
    """One order line. qty <= 0 (everything cancelled) is stored as cancelled.
    order: the marketplace's order id; needs_accept: the order waits for the seller's acceptance."""
    try:
        qty = int(qty if qty not in (None, "") else 1)
    except (TypeError, ValueError):
        qty = 1
    s = {"key": key, "channel": channel, "sku": str(sku or ""), "qty": max(qty, 1),
         "cancelled": bool(cancelled) or qty <= 0, "at": iso(parse_time(at) or utcnow())}
    if ean:
        s["ean"] = str(ean)
    if order is not None:
        s["order"] = str(order)
    s["needs_accept"] = bool(needs_accept) and not s["cancelled"]
    return s


def to_gtin(code):
    """ISBN-10/13 or EAN -> 13-digit GTIN, or None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    digit = lambda core: str((10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10)
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        return "978" + c[:9] + digit("978" + c[:9])
    if len(c) == 13 and c.isdigit() and digit(c[:12]) == c[12]:
        return c
    return None


def state_file(name):
    """A pusher's state file (they live in the folder the pushers run in)."""
    return load_json(Path(name), {})


LEDGER = {}        # the ledger of the current run, for readers that page by order id (Vinted)


# --------------------------------------------------------------------- readers
# Each reader returns a list of sale lines; it raises on API errors (the
# channel's cursor then stays where it was and the window is read again).

def read_mirakl(shop, since):
    mod = importlib.import_module(f"{shop}_push")
    api = mod.Mirakl()
    out, offset = [], 0
    while True:
        code, data = api.call("GET", "/orders", params={"start_update_date": iso(since), "max": 100,
                                                         "offset": offset})
        if code != 200:
            raise RuntimeError(f"OR11 orders failed ({code}): {str(data)[:200]}")
        orders = data.get("orders") or []
        for o in orders:
            dead_order = o.get("order_state") in ("REFUSED", "CANCELED")
            for line in o.get("order_lines") or []:
                dead = dead_order or line.get("order_line_state") in ("REFUSED", "CANCELED")
                s = sale(shop, f"{shop}:{o.get('order_id')}:{line.get('order_line_id')}", line.get("offer_sku"),
                         line.get("quantity"), dead, o.get("created_date"), order=o.get("order_id"),
                         needs_accept=line.get("order_line_state") == "WAITING_ACCEPTANCE")
                s["line_id"] = str(line.get("order_line_id"))
                out.append(s)
        offset += len(orders)
        if not orders or offset >= int(data.get("total_count") or 0):
            return out


def read_amazon(since):
    mod = importlib.import_module("amazon_push")
    api = mod.SPAPI()
    ids = ",".join(v[0] for v in mod.MARKETS.values())
    out, token = [], None
    while True:
        params = {"marketplaceIds": ids, "lastUpdatedAfter": iso(since), "maxResultsPerPage": 100,
                  "includedData": "FULFILLMENT"}
        if token:
            params = {"paginationToken": token, "marketplaceIds": ids, "includedData": "FULFILLMENT"}
        code, data = api.call("GET", "/orders/2026-01-01/orders", params=params)
        if code != 200:
            raise RuntimeError(f"searchOrders failed ({code}): {str(data)[:200]}")
        for o in data.get("orders") or []:
            dead = (o.get("fulfillment") or {}).get("fulfillmentStatus") == "CANCELLED"
            for item in o.get("orderItems") or []:
                out.append(sale("amazon", f"amazon:{o.get('orderId')}:{item.get('orderItemId')}",
                                (item.get("product") or {}).get("sellerSku"), item.get("quantityOrdered"),
                                dead, o.get("createdTime")))
        token = (data.get("pagination") or {}).get("nextToken")
        if not token:
            return out


def read_ebay(since):
    mod = importlib.import_module("ebay_push")
    eb = mod.Ebay(False, "EBAY_ES")
    eb.token = eb._token_call({"grant_type": "refresh_token", "refresh_token": os.environ["EBAY_REFRESH_TOKEN"],
                               "scope": EBAY_FULFILLMENT_SCOPE})
    out, offset = [], 0
    while True:
        data = eb.call("GET", "/sell/fulfillment/v1/order",
                       params={"filter": f"lastmodifieddate:[{iso(since)}..]", "limit": 200, "offset": offset})
        orders = data.get("orders") or []
        for o in orders:
            dead = (o.get("cancelStatus") or {}).get("cancelState") == "CANCELED"
            for li in o.get("lineItems") or []:
                out.append(sale("ebay", f"ebay:{o.get('orderId')}:{li.get('lineItemId')}", li.get("sku"),
                                li.get("quantity"), dead, o.get("creationDate")))
        offset += len(orders)
        if not orders or offset >= int(data.get("total") or 0):
            return out


def read_kaufland(since):
    mod = importlib.import_module("kaufland_push")
    api = mod.Kaufland()
    out = []
    for sf in mod.ALL_STOREFRONTS:
        offset = 0
        while True:
            code, data = api.call("GET", "/order-units", {"storefront": sf, "ts_updated_from_iso": iso(since),
                                                          "limit": 100, "offset": offset})
            if code == 400 and "storefront" in str(data).lower():
                break                                   # storefront not activated for this account
            if code != 200:
                raise RuntimeError(f"order-units ({sf}) failed ({code}): {str(data)[:200]}")
            units = data.get("data") or []
            for u in units:                             # one order unit = one copy
                out.append(sale("kaufland", f"kaufland:{u.get('id_order_unit')}", u.get("id_offer"), 1,
                                "cancel" in str(u.get("status") or "").lower(), u.get("ts_created_iso")))
            offset += len(units)
            total = (data.get("pagination") or {}).get("total")
            if not units or (total is not None and offset >= int(total)):
                break
    return out


def read_bol(since):
    mod = importlib.import_module("bol_push")
    api = mod.Bol()
    recent = utcnow() - since < timedelta(minutes=55)
    # bol: change-interval-minute (max 60) for frequent runs; latest-change-date (a day) after a gap
    base = {"status": "ALL", "fulfilment-method": "ALL"}
    base.update({"change-interval-minute": 60} if recent else {"latest-change-date": since.strftime("%Y-%m-%d")})
    out, page = [], 1
    while True:
        code, data = api.call("GET", "/retailer/orders", media=mod.RETAILER_V10, params=dict(base, page=page))
        if code != 200:
            raise RuntimeError(f"orders failed ({code}): {str(data)[:200]}")
        orders = data.get("orders") or []
        for o in orders:
            for it in o.get("orderItems") or []:
                qty = int(it.get("quantity") or 1) - int(it.get("quantityCancelled") or 0)
                out.append(sale("bol", f"bol:{o.get('orderId')}:{it.get('orderItemId')}", None, qty, qty <= 0,
                                o.get("orderPlacedDateTime"), ean=it.get("ean")))
        if len(orders) < 50:
            return out
        page += 1


def read_cdiscount(since):
    mod = importlib.import_module("cdiscount_push")
    api = mod.Octopia()
    out, page = [], 1
    while True:
        r, data = api.call("GET", "/orders", params={"updatedAtMin": iso(since), "pageSize": 100, "pageIndex": page})
        if r.status_code != 200:
            raise RuntimeError(f"orders failed ({r.status_code}): {str(data)[:200]}")
        orders = data.get("items") or []
        for o in orders:
            dead_order = o.get("status") in ("Cancelled", "Refused", "Rejected")
            for line in o.get("lines") or []:
                offer = line.get("offer") or {}
                out.append(sale("cdiscount", f"cdiscount:{o.get('orderId')}:{line.get('orderLineId')}",
                                offer.get("sellerProductId") or offer.get("sellerExternalReference"),
                                line.get("quantity"), dead_order or line.get("status") in ("Cancelled", "Refused"),
                                o.get("createdAt") or o.get("purchasedAt"), ean=offer.get("productGtin"),
                                order=o.get("orderId"), needs_accept="WaitingAcceptance" in (o.get("status"), line.get("status"))))
        if len(orders) < 100:
            return out
        page += 1


def read_allegro(since):
    mod = importlib.import_module("allegro_push")
    api = mod.Allegro()
    out, offset = [], 0
    while offset < 10000:                               # Allegro: offset + limit <= 10 000
        data = api.get("/order/checkout-forms", [("updatedAt.gte", iso(since)), ("limit", 100), ("offset", offset)])
        forms = data.get("checkoutForms") or []
        for f in forms:
            dead = f.get("status") == "CANCELLED"
            for li in f.get("lineItems") or []:
                offer = li.get("offer") or {}
                out.append(sale("allegro", f"allegro:{f.get('id')}:{li.get('id')}",
                                (offer.get("external") or {}).get("id"), li.get("quantity"), dead,
                                li.get("boughtAt") or f.get("updatedAt")))
        if len(forms) < 100:
            return out
        offset += 100
    return out


def read_otto(since):
    mod = importlib.import_module("otto_push")
    mod.SCOPES = "orders"                                # the Orders interface needs its own token scope
    api = mod.Otto()
    out, path, params = [], "/v4/orders", {"fromDate": iso(since), "limit": 128}
    while path:
        code, data = api.call("GET", path, params=params)
        if code != 200:
            raise RuntimeError(f"orders failed ({code}): {str(data)[:200]}")
        for o in data.get("resources") or []:
            for item in o.get("positionItems") or []:     # one position item = one copy
                out.append(sale("otto", f"otto:{item.get('positionItemId')}", (item.get("product") or {}).get("sku"),
                                1, str(item.get("fulfillmentStatus") or "").startswith("CANCELLED"),
                                o.get("orderDate")))
        nxt = next((l.get("href") for l in data.get("links") or [] if l.get("rel") == "next"), None)
        path, params = nxt, None
    return out


def read_emag(since):
    mod = importlib.import_module("emag_push")
    api = mod.Emag()
    by_id = {str(v): k for k, v in state_file("emag_ids.json").items()}
    # eMAG times are local (Bucharest) without a zone: read a wider window; max one month
    start = max(since - timedelta(hours=3), utcnow() - timedelta(days=28))
    fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for o in api.read_all("order/read", {"modifiedAfter": fmt(start), "modifiedBefore": fmt(utcnow() + timedelta(hours=3))}):
        dead = str(o.get("status")) == "0"
        for line in o.get("products") or []:
            pid = str(line.get("product_id"))
            out.append(sale("emag", f"emag:{o.get('id')}:{line.get('id')}", by_id.get(pid) or line.get("ext_part_number"),
                            line.get("quantity"), dead or str(line.get("status")) == "0", o.get("date"),
                            order=o.get("id"), needs_accept=str(o.get("status")) == "1"))
    return out


def read_cdon(since):
    mod = importlib.import_module("cdon_push")
    api = mod.Cdon()
    out = []
    for state in ("CREATED", "ACCEPTED", "FULFILLED", "NOT_FULFILLED"):
        page = 1
        while page <= 20:
            code, data = api.call("GET", "v1/orders", params={"state": state, "limit": 1000, "page": page})
            if code != 200:
                raise RuntimeError(f"orders ({state}) failed ({code}): {str(data)[:200]}")
            orders = data if isinstance(data, list) else (data or {}).get("orders") or []
            fresh = [o for o in orders if state in ("CREATED", "ACCEPTED")
                     or (parse_time(o.get("updated_at")) or since) >= since]
            for o in fresh:                              # one order = one SKU (CDON)
                out.append(sale("cdon", f"cdon:{o.get('id')}", o.get("article_sku"), o.get("quantity"),
                                o.get("state") == "NOT_FULFILLED", o.get("created_at"), order=o.get("id"),
                                needs_accept=o.get("state") == "CREATED"))
            # CDON has no date filter: stop when a page holds nothing new or is the last one
            if len(orders) < 1000 or (state not in ("CREATED", "ACCEPTED") and not fresh):
                break
            page += 1
    return out


def read_onbuy(since):
    mod = importlib.import_module("onbuy_push")
    api = mod.OnBuy()
    codes = {k.split(":", 1)[0] for k in state_file("onbuy_posted.json")} | {c.lower() for c in mod.SITES}
    out = []
    for code in sorted(c for c in codes if c in mod.SITE_TABLE):
        params = {"site_id": mod.SITE_TABLE[code][0], "filter[status]": "all",
                  "filter[modified_since]": (since - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")}
        for o in api.pages("orders", params):
            dead = "cancel" in str(o.get("status") or "").lower()
            for n, prod in enumerate(o.get("products") or []):
                ref = prod.get("onbuy_internal_reference") or n
                out.append(sale("onbuy", f"onbuy:{o.get('order_id')}:{ref}", prod.get("sku"), prod.get("quantity"),
                                dead, o.get("date")))
    return out


def read_booklooker(since):
    mod = importlib.import_module("booklooker_push")
    api = mod.Booklooker()
    start = max(since.date(), (utcnow() - timedelta(days=28)).date())   # one month at most per call
    orders = api.call("GET", "order", {"dateFrom": start.isoformat(), "dateTo": utcnow().date().isoformat()}) or []
    out = []
    for o in orders if isinstance(orders, list) else []:
        dead_order = str(o.get("status") or "").upper().startswith("CANCEL")
        for it in o.get("orderItems") or []:
            out.append(sale("booklooker", f"booklooker:{o.get('orderId')}:{it.get('orderItemId')}", it.get("orderNo"),
                            it.get("amount"), dead_order or str(it.get("status")) == "Canceled",
                            f"{o.get('orderDate', '')}T{o.get('orderTime') or '00:00:00'}Z"))
    return out


FRUUGO_ORDERS_URL = "https://www.fruugo.com/orders/download"


def read_fruugo(since):
    import base64
    import urllib.request
    import xml.etree.ElementTree as ET
    url = (os.environ.get("FRUUGO_ORDERS_URL") or FRUUGO_ORDERS_URL) + "?from=" + iso(since)
    auth = base64.b64encode(f"{os.environ['FRUUGO_USERNAME']}:{os.environ['FRUUGO_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}", "User-Agent": "product-pushers/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        root = ET.fromstring(r.read())
    ns = {"o": "https://www.fruugo.com/orders/schema"}
    out = []
    for o in root.findall("o:order", ns):
        oid, at = o.findtext("o:orderId", "", ns), o.findtext("o:orderDate", "", ns)
        for line in o.findall("o:orderLines/o:orderLine", ns):
            total = int(line.findtext("o:totalNumberOfItems", "1", ns) or 1)
            cancelled = int(line.findtext("o:cancelledItems", "0", ns) or 0)
            out.append(sale("fruugo", f"fruugo:{oid}:{line.findtext('o:fruugoSkuId', '', ns)}",
                            line.findtext("o:skuId", "", ns) or line.findtext("o:productId", "", ns),
                            total - cancelled, total - cancelled <= 0, at))
    return out


def read_vinted(since):
    mod = importlib.import_module("vinted_api_push")
    api = mod.VintedPro(os.environ["VINTED_PRO_TOKEN"])
    known = {k.split(":")[1]: s for k, s in LEDGER.get("sales", {}).items() if k.startswith("vinted:")}
    recent = [int(i) for i, s in known.items() if parse_time(s["at"]) > utcnow() - timedelta(days=14) and i.isdigit()]
    after = min(recent) - 1 if recent else int(LEDGER.get("vinted_after_id") or 0)
    out, newest = [], after
    while True:
        code, data = api.request("GET", f"/api/v1/orders?after-id={after}")
        if code != 200:
            raise RuntimeError(f"orders failed ({code}): {str(data)[:200]}")
        orders = (data or {}).get("orders") or []
        for o in orders:
            oid = str(o.get("id"))
            dead = o.get("status") == "CANCELED"
            mine = [s for k, s in LEDGER.get("sales", {}).items() if k.startswith(f"vinted:{oid}:")]
            if mine:                                     # known order: only its status can change
                out += [dict(s, cancelled=dead) for s in mine]
                continue
            dcode, detail = api.request("GET", f"/api/v1/orders/{oid}")
            if dcode != 200:
                raise RuntimeError(f"order {oid} failed ({dcode}): {str(detail)[:200]}")
            for n, item in enumerate((detail or {}).get("items") or []):
                out.append(sale("vinted", f"vinted:{oid}:{item.get('id') or n}", item.get("item_reference"), 1,
                                dead, o.get("created_at") or (detail or {}).get("created_at")))
        if not orders:
            break
        after = max(int(o.get("id")) for o in orders)
        newest = max(newest, after)
    LEDGER["vinted_after_id"] = max(int(LEDGER.get("vinted_after_id") or 0), newest)
    return out


def read_temu(since):
    mod = importlib.import_module("temu_push")
    api = mod.Temu()
    by_sku_id = {str(v.get("sku_id")): k for k, v in state_file("temu_posted.json").items() if v.get("sku_id")}
    out, page = [], 1
    while True:
        res = api.call("bg.order.list.v2.get", pageNumber=page, pageSize=100,
                       updateAtStart=int(since.timestamp()), updateAtEnd=int(utcnow().timestamp()))
        items = res.get("pageItems") or []
        for it in items:
            parent = (it.get("parentOrderMap") or {}).get("parentOrderSn")
            for o in it.get("orderList") or []:
                ext = next((p.get("extCode") for p in o.get("productList") or [] if p.get("extCode")), None)
                qty = int(o.get("quantity") or 1) - int(o.get("canceledQuantityBeforeShipment") or 0)
                ts = o.get("orderCreateTime")
                at = iso(datetime.fromtimestamp(int(ts) / (1000 if int(ts) > 10 ** 11 else 1), timezone.utc)) if ts else None
                out.append(sale("temu", f"temu:{parent}:{o.get('orderSn')}", ext or by_sku_id.get(str(o.get("skuId"))),
                                qty, qty <= 0 or bool(o.get("isCancelledDuringPending")), at))
        if len(items) < 100:
            return out
        page += 1


def read_tiktok(since):
    mod = importlib.import_module("tiktok_push")
    api = mod.TikTok()
    by_seller = {v.get("seller_sku"): k for k, v in state_file("tiktok_posted.json").items() if v.get("seller_sku")}
    out, token = [], None
    while True:
        query = {"page_size": 100, "sort_field": "update_time"}
        if token:
            query["page_token"] = token
        data = api.call("POST", "/order/202309/orders/search", query=query,
                        body={"update_time_ge": int(since.timestamp())})
        for o in data.get("orders") or []:
            dead = o.get("status") == "CANCELLED"
            at = iso(datetime.fromtimestamp(int(o.get("create_time") or 0), timezone.utc)) if o.get("create_time") else None
            for li in o.get("line_items") or []:        # TikTok: one line item per copy
                ss = li.get("seller_sku")
                out.append(sale("tiktok", f"tiktok:{o.get('id')}:{li.get('id')}", by_seller.get(ss, ss), 1,
                                dead or li.get("display_status") == "CANCELLED", at))
        token = data.get("next_page_token")
        if not token:
            return out


def readers():
    """(channel, reader function, required env vars)."""
    out = [(shop, (lambda s: lambda since: read_mirakl(s, since))(shop),
            [f"{shop.upper()}_API_URL", f"{shop.upper()}_API_KEY"]) for shop in MIRAKL_SHOPS]
    out += [("amazon", read_amazon, ["AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET", "AMAZON_REFRESH_TOKEN",
                                     "AMAZON_SELLER_ID"]),
            ("ebay", read_ebay, ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN"]),
            ("kaufland", read_kaufland, ["KAUFLAND_CLIENT_KEY", "KAUFLAND_SECRET_KEY"])]
    import push_all
    more = [("bol", read_bol), ("cdiscount", read_cdiscount), ("allegro", read_allegro), ("otto", read_otto),
            ("emag", read_emag), ("cdon", read_cdon), ("onbuy", read_onbuy), ("booklooker", read_booklooker),
            ("vinted", read_vinted), ("temu", read_temu), ("tiktok", read_tiktok)]
    out += [(name, fn, list(push_all.PUSHERS[name]["env"])) for name, fn in more]
    out.append(("fruugo", read_fruugo, ["FRUUGO_USERNAME", "FRUUGO_PASSWORD"]))
    return out


# ----------------------------------------------------------------------- stock

def source_stock(product):
    if product.get("sold_out"):
        return 0
    for k in ("stock", "quantity"):
        v = product.get(k)
        if v not in (None, ""):
            try:
                return max(int(v), 0)
            except (TypeError, ValueError):
                pass
    return DEFAULT_STOCK


def sku_of(product):
    return str(product.get("sku") or product.get("id") or "")


def counts(sale_, snapshot):
    """Does this sale still reduce the source stock? Until books.json is SETTLE_HOURS newer than it."""
    return not sale_["cancelled"] and snapshot < parse_time(sale_["at"]) + timedelta(hours=SETTLE_HOURS)


def compute(products, sales, snapshot):
    """Returns (synced products, {sku: (source, sold, available)})."""
    sold = {}
    for s in sales.values():
        if counts(s, snapshot):
            sold[s["sku"]] = sold.get(s["sku"], 0) + s["qty"]
    synced, info = [], {}
    for p in products:
        sku = sku_of(p)
        src = source_stock(p)
        n = sold.get(sku, 0)
        avail = max(src - n, 0)
        q = dict(p)
        if n:
            q["stock"] = q["quantity"] = avail
            if avail == 0:
                q["sold_out"] = True
        synced.append(q)
        if sku:
            info[sku] = (src, n, avail)
    return synced, info


# -------------------------------------------------------------------- orders

def accept_order(channel, order_id, lines):
    """Accept one order on its marketplace. Returns None (accepted) or an error text."""
    import requests
    if channel in MIRAKL_SHOPS:                          # Mirakl OR21: every waiting line of the order
        api = importlib.import_module(f"{channel}_push").Mirakl()
        body = {"order_lines": [{"accepted": True, "id": s["line_id"]} for s in lines]}
        r = requests.put(f"{api.base}/api/orders/{order_id}/accept", json=body, timeout=60,
                         headers=dict(api.headers, **{"Content-Type": "application/json"}),
                         params={"shop_id": api.shop_id} if api.shop_id else None)
        return None if r.status_code in (200, 204) else f"OR21 {r.status_code}: {r.text[:200]}"
    if channel == "cdiscount":                           # Octopia: approval-status (Cdiscount channel)
        api = importlib.import_module("cdiscount_push").Octopia()
        r, data = api.call("POST", f"/orders/{order_id}/approval-status", body={"approval_status": "Accepted"})
        return None if r.status_code in (200, 201, 204) else f"{r.status_code}: {str(data)[:200]}"
    if channel == "emag":                                # eMAG: acknowledge = accept ("in progress")
        api = importlib.import_module("emag_push").Emag()
        ok, _, msgs = api.call(f"order/acknowledge/{order_id}", {})
        return None if ok else "; ".join(msgs) or "acknowledge refused"
    return "accept it in the marketplace's seller portal (no automatic accept for this marketplace)"


def handle_orders(ledger, info, titles, snapshot, a):
    """Which sales the stock covers (first come, first served), accept covered orders, flag double sales."""
    sales = ledger["sales"]
    by_sku = {}
    for s in sales.values():
        if not s["cancelled"] and counts(s, snapshot):
            by_sku.setdefault(s["sku"], []).append(s)
    for sku, rows in by_sku.items():
        src = info.get(sku, (None,))[0]
        used = 0
        for s in sorted(rows, key=lambda x: (x["at"], x["key"])):
            used += s["qty"]
            s["oversold"] = src is None or used > src
            if src is None:
                s["note"] = "book not in the products file"
    for s in sales.values():
        if s["cancelled"]:
            s.pop("oversold", None)
    orders = {}
    for s in sales.values():
        if s.get("needs_accept") and not s.get("accepted_at") and not s["cancelled"] and s.get("order"):
            orders.setdefault((s["channel"], s["order"]), []).append(s)
    accepted = 0
    for (channel, order_id), lines in sorted(orders.items()):
        if any(s.get("oversold") for s in lines):
            for s in lines:
                print(f"   ! {channel} order {order_id}: {s['sku']} is a DOUBLE SALE (stock already sold) - "
                      f"not accepted, decide by hand")
            continue
        if not AUTO_ACCEPT or channel not in MIRAKL_SHOPS + ["cdiscount", "emag"]:
            for s in lines:
                s["accept_error"] = "accept it in the marketplace's seller portal"
            continue
        if a.dry_run:
            print(f"   (dry run) would accept {channel} order {order_id}")
            continue
        try:
            err = accept_order(channel, order_id, lines)
        except (Exception, SystemExit) as e:
            err = str(e)
        if err:
            print(f"   ! {channel} order {order_id}: automatic accept failed: {err}")
            for s in lines:
                s["accept_error"] = err
            continue
        accepted += 1
        for s in lines:
            s["accepted_at"] = iso(utcnow())
            s.pop("accept_error", None)
        print(f"   accepted {channel} order {order_id} ({', '.join(s['sku'] for s in lines)})")
    write_orders_report(ledger, titles)
    return accepted


def order_status(s):
    if s["cancelled"]:
        return "cancelled"
    if s.get("oversold"):
        return "DOUBLE SALE - stock already sold elsewhere; cancel or find a copy (not accepted)"
    if s.get("accepted_at"):
        return "accepted automatically"
    if s.get("needs_accept"):
        return "ACCEPT BY HAND: " + (s.get("accept_error") or "waiting for acceptance")
    return "ok (no acceptance needed)"


def write_orders_report(ledger, titles):
    """New orders (not yet in an email) + everything that still needs a person."""
    rows = []
    for s in sorted(ledger["sales"].values(), key=lambda x: x["at"]):
        needs_you = not s["cancelled"] and (s.get("oversold") or (s.get("needs_accept") and not s.get("accepted_at")))
        if s.get("reported") and not needs_you:
            continue
        rows.append({"key": s["key"], "channel": s["channel"], "order": s.get("order") or s["key"].split(":")[1],
                     "sku": s["sku"], "title": titles.get(s["sku"], ""), "qty": s["qty"], "at": s["at"],
                     "status": order_status(s), "needs_you": bool(needs_you), "new": not s.get("reported")})
    save_json(ORDERS_FILE, rows)


def mark_reported(keys):
    """Called by push_all after the email went out: these orders are no longer 'new'."""
    import push_all
    ledger = load_json(LEDGER_FILE, {})
    for k in keys:
        if k in ledger.get("sales", {}):
            ledger["sales"][k]["reported"] = True
    save_json(LEDGER_FILE, ledger)


# ------------------------------------------------------------------- pushers

def run_pushers(changed, synced, a):
    """Run every enabled, configured pusher on the changed books with its sold-out option."""
    import push_all
    cfg = push_all.load_config()
    subset = [p for p in synced if sku_of(p) in changed]
    save_json(CHANGES_FILE, subset)
    restock = any(changed[s] > 0 for s in changed)
    results = []
    for name, spec in push_all.PUSHERS.items():
        settings = cfg.get("pushers", {}).get(name, {})
        if not settings.get("enabled"):
            continue
        if name not in SOLD_OUT_FLAGS:
            results.append((name, "CANNOT SYNC", "no sold-out option in this pusher yet"))
            continue
        missing = [k for k in push_all.required_env(name, spec, settings) if not os.environ.get(k)]
        if missing:
            results.append((name, "NOT CONFIGURED", "missing " + ", ".join(missing)))
            continue
        source = SYNCED_FILE if name in FULL_FILE else CHANGES_FILE
        cmd = push_all.build_command(spec, settings, source, a.dry_run)
        cmd += [f for f in SOLD_OUT_FLAGS[name] if f not in cmd]
        if restock and name in RESEND and "--resend" not in cmd:
            cmd.append("--resend")
        try:
            p = subprocess.run(cmd, cwd=BASE, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=TIMEOUT_MINUTES * 60, encoding="utf-8", errors="replace")
            out, rc = p.stdout, p.returncode
        except subprocess.TimeoutExpired:
            out, rc = "", "timeout"
        problems = [l.strip() for l in out.splitlines() if push_all.RE_PROBLEM.search(l)]
        status = "FAILED" if rc != 0 else ("PROBLEMS" if problems else "OK")
        results.append((name, status, (problems[0] if problems else "") if rc == 0 else f"exit {rc}"))
        if a.verbose or status != "OK":
            print(f"   --- {spec['label']}: {' '.join(cmd[1:])}\n" + "\n".join("      " + l for l in out.splitlines()[-15:]))
    return results


# ------------------------------------------------------------------- commands

def products_file(a):
    if a.source:
        return Path(a.source)
    import push_all
    cfg = load_json(push_all.CONFIG_FILE, {})
    return Path(cfg.get("json_file") or r"C:\programming\scraper\books.json")


def cmd_sync(a):
    path = products_file(a)
    if not path.exists():
        sys.exit(f"Products file not found: {path}")
    products = load_json(path, [])
    if isinstance(products, dict):
        products = products.get("products", [])
    snapshot = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0)
    ledger = load_json(LEDGER_FILE, {})
    ledger.setdefault("cursors", {})
    ledger.setdefault("sales", {})
    ledger.setdefault("sent", {})          # sku -> stock the marketplaces have (last sent or seen)
    LEDGER.clear()
    LEDGER.update(ledger)
    by_ean = {}
    for prod in products:
        g = to_gtin(prod.get("isbn") or prod.get("ean"))
        if g and sku_of(prod):
            by_ean.setdefault(g, sku_of(prod))
    start = utcnow()
    print(f"Stock sync {iso(start)}: source {path.name} (scraped {iso(snapshot)}), {len(products)} book(s)")

    new_sales = cancelled = 0
    channels = []
    for channel, reader, env in readers():
        if not has_env(*env):
            channels.append(f"{channel}: not configured")
            continue
        cur = parse_time(ledger["cursors"].get(channel))
        since = (cur - timedelta(minutes=OVERLAP_MINUTES)) if cur else start - timedelta(hours=LOOKBACK_HOURS)
        try:
            lines = reader(since)
        except (Exception, SystemExit) as e:          # one channel failing must not stop the others
            print(f"   ! {channel}: could not read orders: {e}")
            channels.append(f"{channel}: ERROR")
            continue
        n_new = 0
        for s in lines:
            if not s["sku"] and s.get("ean"):           # bol.com & co. report the EAN, not our SKU
                s["sku"] = by_ean.get(to_gtin(s["ean"]) or s["ean"], "")
            if not s["sku"]:
                continue
            old = ledger["sales"].get(s["key"])
            if old is None:
                if not s["cancelled"]:
                    n_new += 1
                    print(f"   + {channel}: sold {s['qty']} x {s['sku']} ({s['key']})")
            elif not old["cancelled"] and s["cancelled"]:
                cancelled += 1
                print(f"   - {channel}: cancelled {s['sku']} ({s['key']}) -> stock back")
            if old is None or any(old.get(k) != v for k, v in s.items() if k != "at"):
                # keep our own flags (accepted_at, reported, oversold) and the first-seen time
                ledger["sales"][s["key"]] = s if old is None else dict(old, **dict(s, at=old["at"]))
        new_sales += n_new
        ledger["cursors"][channel] = iso(start)
        if channel == "vinted":
            ledger["vinted_after_id"] = LEDGER.get("vinted_after_id")
        channels.append(f"{channel}: {len(lines)} line(s), {n_new} new")
    print("   channels: " + "; ".join(channels))

    keep_after = start - timedelta(days=KEEP_DAYS)
    ledger["sales"] = {k: s for k, s in ledger["sales"].items()
                       if counts(s, snapshot) or parse_time(s["at"]) > keep_after}

    synced, info = compute(products, ledger["sales"], snapshot)
    save_json(SYNCED_FILE, synced)
    titles = {sku_of(p): p.get("title") or "" for p in products}
    handle_orders(ledger, info, titles, snapshot, a)
    changed = {}
    for sku, (src, sold, avail) in info.items():
        before = ledger["sent"].get(sku, src)     # a book seen for the first time was pushed with its source stock
        if avail != before:
            changed[sku] = avail
    reduced = sum(1 for _, (src, sold, avail) in info.items() if sold)
    sold_out = [s for s, (src, sold, avail) in info.items() if sold and avail == 0]
    print(f"   {new_sales} new sale(s), {cancelled} cancelled; {reduced} book(s) with our sales counted, "
          f"{len(sold_out)} sold out by our sales; {len(changed)} stock change(s) to send")
    for sku, avail in sorted(changed.items()):
        src, sold, _ = info[sku]
        print(f"   {sku}: stock {ledger['sent'].get(sku, src)} -> {avail}" + ("  (sold out)" if avail == 0 else ""))

    def remember(include_changed):
        ledger["sent"] = {sku: (avail if (include_changed or sku not in changed) else ledger["sent"].get(sku, src))
                          for sku, (src, sold, avail) in info.items()}

    if a.no_apply or not changed:
        if not a.dry_run:
            remember(include_changed=False)
            save_json(LEDGER_FILE, ledger)
        print(f"Wrote {SYNCED_FILE.name}." + (" Stock changes are not sent (--no-apply)." if changed else ""))
        return
    results = run_pushers(changed, synced, a)
    bad = [r for r in results if r[1] in ("FAILED",)]
    for name, status, note in results:
        print(f"   {name:<14} {status:<15} {note}")
    if not a.dry_run:
        remember(include_changed=not bad)             # failed: send the same changes again next run
        save_json(LEDGER_FILE, ledger)
    if bad:
        print(f"! {len(bad)} pusher(s) failed; the stock changes are sent again on the next run.")
        sys.exit(1)
    print("Stock synced." if not a.dry_run else "Dry run: nothing changed.")


def cmd_status(a):
    ledger = load_json(LEDGER_FILE, {})
    sales = ledger.get("sales") or {}
    path = products_file(a)
    if not path.exists():
        sys.exit(f"Products file not found: {path}")
    snapshot = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    print(f"Ledger: {len(sales)} sale line(s); source {path.name} scraped {iso(snapshot)}")
    for ch, cur in sorted((ledger.get("cursors") or {}).items()):
        print(f"   {ch:<14} orders read up to {cur}")
    live = [s for s in sales.values() if counts(s, snapshot)]
    for s in sorted(live, key=lambda s: s["at"]):
        print(f"   {s['at']}  {s['channel']:<12} {s['sku']}  x{s['qty']}")
    reduced = {s for s in (x["sku"] for x in live)}
    for sku, q in sorted((ledger.get("sent") or {}).items()):
        if sku in reduced or q == 0:
            print(f"   {sku}: stock on the marketplaces = {q}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="One stock for every marketplace (stop double sales)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="read orders, update stock on every marketplace")
    s.add_argument("--source", help="products JSON (default: json_file from push_all_config.json)")
    s.add_argument("--dry-run", action="store_true", help="pushers run in their safe mode; ledger not saved")
    s.add_argument("--no-apply", action="store_true", help="only read orders and write books_synced.json")
    s.add_argument("--verbose", action="store_true", help="show every pusher's output")
    st = sub.add_parser("status", help="sales in the ledger and stock sent")
    st.add_argument("--source", help="products JSON (default: json_file from push_all_config.json)")
    a = ap.parse_args()
    if a.cmd == "status":
        return cmd_status(a)
    import push_all
    push_all.load_env_file(push_all.ENV_FILE)
    if not os.environ.get("PUSH_ALL_RUNNING"):     # push_all already holds the lock when it runs us
        push_all.take_lock()
        try:
            cmd_sync(a)
        finally:
            push_all.LOCK_FILE.unlink(missing_ok=True)
    else:
        cmd_sync(a)


if __name__ == "__main__":
    main()
