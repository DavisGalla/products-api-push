#!/usr/bin/env python3
"""
Stop double sales: one stock for every marketplace.

The same book is listed on many marketplaces (eBay x11, Kaufland x9, Amazon,
the Mirakl shops, ...), but the source shop has a limited stock (usually 1).
This script:

  1. reads new orders from the marketplaces' order APIs (since the last run):
       Mirakl (Casa del Libro, El Corte Ingles, Carrefour, Worten, IBS.it,
       Empik, E.Leclerc), Amazon (Orders API v2026-01-01), eBay (Fulfillment
       API) and Kaufland (order units, every storefront);
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
# ======================================================================

DATA_DIR = Path(os.environ.get("STOCK_SYNC_DIR") or BASE)     # tests point this at a temporary folder
LEDGER_FILE = DATA_DIR / "stock_ledger.json"
SYNCED_FILE = DATA_DIR / "books_synced.json"
CHANGES_FILE = DATA_DIR / "books_sync_changes.json"
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


def sale(channel, key, sku, qty, cancelled, at):
    return {"key": key, "channel": channel, "sku": str(sku or ""), "qty": int(qty or 1),
            "cancelled": bool(cancelled), "at": iso(parse_time(at) or utcnow())}


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
                out.append(sale(shop, f"{shop}:{o.get('order_id')}:{line.get('order_line_id')}",
                                line.get("offer_sku"), line.get("quantity"), dead, o.get("created_date")))
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


def readers():
    """(channel, reader function, required env vars)."""
    out = [(shop, (lambda s: lambda since: read_mirakl(s, since))(shop),
            [f"{shop.upper()}_API_URL", f"{shop.upper()}_API_KEY"]) for shop in MIRAKL_SHOPS]
    out += [("amazon", read_amazon, ["AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET", "AMAZON_REFRESH_TOKEN",
                                     "AMAZON_SELLER_ID"]),
            ("ebay", read_ebay, ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN"]),
            ("kaufland", read_kaufland, ["KAUFLAND_CLIENT_KEY", "KAUFLAND_SECRET_KEY"])]
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
            if not s["sku"]:
                continue
            old = ledger["sales"].get(s["key"])
            if old is None:
                if not s["cancelled"]:
                    n_new += 1
                    print(f"   + {channel}: sold {s['qty']} x {s['sku']} ({s['key']})")
            elif old["cancelled"] != s["cancelled"] and s["cancelled"]:
                cancelled += 1
                print(f"   - {channel}: cancelled {s['sku']} ({s['key']}) -> stock back")
            if old is None or old != s:
                ledger["sales"][s["key"]] = s if old is None else dict(s, at=old["at"])
        new_sales += n_new
        ledger["cursors"][channel] = iso(start)
        channels.append(f"{channel}: {len(lines)} line(s), {n_new} new")
    print("   channels: " + "; ".join(channels))

    keep_after = start - timedelta(days=KEEP_DAYS)
    ledger["sales"] = {k: s for k, s in ledger["sales"].items()
                       if counts(s, snapshot) or parse_time(s["at"]) > keep_after}

    synced, info = compute(products, ledger["sales"], snapshot)
    save_json(SYNCED_FILE, synced)
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
