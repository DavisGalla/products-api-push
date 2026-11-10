#!/usr/bin/env python3
"""
Test for stock_sync.py (stop double sales), against LOCAL MOCKS of the order
APIs it reads:
  * Mirakl OR11 GET /api/orders (Casa del Libro), offset pagination,
    order_lines with offer_sku / quantity / order_line_state
  * Amazon Orders API v2026-01-01 GET /orders/2026-01-01/orders (LWA token,
    lastUpdatedAfter, includedData, nextToken pagination, sellerSku,
    quantityOrdered, fulfillmentStatus CANCELLED)
  * eBay Fulfillment API GET /sell/fulfillment/v1/order (token with the
    sell.fulfillment scope, lastmodifieddate filter, lineItems, cancelState)
  * Kaufland GET /v2/order-units per storefront (id_offer, one unit = one copy)
The pushers themselves are not started: their command lines are recorded and
checked (the pushers' own --zero-sold-out handling has its own tests).
Nothing is sent anywhere, and the real ledger / books files are not touched.

    python tests/test_stock_sync.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
RESULTS = []


def ok(cond, name):
    RESULTS.append((bool(cond), name))
    print(("PASS " if cond else "FAIL ") + name)


def now_iso(delta=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta))


class S:
    @classmethod
    def reset(cls):
        cls.mirakl_orders = []
        cls.amazon_orders = []
        cls.ebay_orders = []
        cls.kaufland_units = {}       # storefront -> units
        cls.requests = []             # (api, params)
        cls.mirakl_fail = False


S.reset()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        u = urlparse(self.path)
        if u.path == "/amazon/auth/o2/token":
            return self.reply(200, {"access_token": "Atza|T", "expires_in": 3600})
        if u.path == "/ebay/identity/v1/oauth2/token":
            S.requests.append(("ebay-token", form))
            return self.reply(200, {"access_token": "EBAY-USER", "expires_in": 7200})
        self.reply(404, {})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/mirakl/api/orders":
            S.requests.append(("mirakl", q))
            if self.headers.get("Authorization") != "mirakl-key":
                return self.reply(401, {})
            if S.mirakl_fail:
                return self.reply(400, {"message": "bad request"})
            if int(q.get("max", 10)) > 100:
                return self.reply(400, {"message": "max 100"})
            off, mx = int(q.get("offset", 0)), int(q.get("max", 10))
            return self.reply(200, {"orders": S.mirakl_orders[off:off + mx], "total_count": len(S.mirakl_orders)})
        if u.path == "/amazon/orders/2026-01-01/orders":
            S.requests.append(("amazon", q))
            if self.headers.get("x-amz-access-token") != "Atza|T":
                return self.reply(403, {})
            if "lastUpdatedAfter" not in q and "paginationToken" not in q:
                return self.reply(400, {"errors": [{"message": "createdAfter or lastUpdatedAfter required"}]})
            page = int(q.get("paginationToken", "0"))
            chunk = S.amazon_orders[page * 2:page * 2 + 2]          # 2 per page -> exercises nextToken
            more = len(S.amazon_orders) > page * 2 + 2
            return self.reply(200, {"orders": chunk, "pagination": {"nextToken": str(page + 1)} if more else {}})
        if u.path == "/ebay/sell/fulfillment/v1/order":
            S.requests.append(("ebay", q))
            if self.headers.get("Authorization") != "Bearer EBAY-USER":
                return self.reply(401, {})
            if not q.get("filter", "").startswith("lastmodifieddate:["):
                return self.reply(400, {"errors": [{"message": "filter"}]})
            off = int(q.get("offset", 0))
            return self.reply(200, {"orders": S.ebay_orders[off:off + 200], "total": len(S.ebay_orders)})
        if u.path == "/kaufland/v2/order-units":
            S.requests.append(("kaufland", q))
            sf = q.get("storefront")
            if sf not in ("es", "de"):
                return self.reply(400, {"type": "storefront-not-configured", "title": "storefront not configured"})
            units = S.kaufland_units.get(sf, [])
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 30))
            return self.reply(200, {"data": units[off:off + lim], "pagination": {"offset": off, "limit": lim,
                                                                                  "total": len(units)}})
        self.reply(404, {})


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"

ENV = {
    "CASADELLIBRO_API_URL": BASE + "/mirakl", "CASADELLIBRO_API_KEY": "mirakl-key",
    "AMAZON_LWA_CLIENT_ID": "c", "AMAZON_LWA_CLIENT_SECRET": "s", "AMAZON_REFRESH_TOKEN": "r",
    "AMAZON_SELLER_ID": "SELLER",
    "EBAY_CLIENT_ID": "cid", "EBAY_CLIENT_SECRET": "cs", "EBAY_REFRESH_TOKEN": "rt",
    "KAUFLAND_API_URL": BASE + "/kaufland/v2", "KAUFLAND_CLIENT_KEY": "k" * 32, "KAUFLAND_SECRET_KEY": "s" * 64,
    "GALAXUS_SFTP_USER": "u", "GALAXUS_SFTP_PASSWORD": "p", "GALAXUS_PROVIDER": "Shop", "FNAC_API_URL": "x", "FNAC_PARTNER_ID": "x",
    "FNAC_SHOP_ID": "x", "FNAC_KEY": "x", "PUSH_ALL_RUNNING": "1",
}
BOOKS = [{"id": "B1", "title": "Uno", "price_eur": 10},
         {"id": "B2", "title": "Dos", "price_eur": 10, "stock": 2},
         {"id": "B3", "title": "Tres", "price_eur": 10},
         {"id": "B4", "title": "Cuatro", "price_eur": 10, "sold_out": True},
         {"id": "B5", "title": "Cinco", "price_eur": 10},
         {"id": "B6", "title": "Seis", "price_eur": 10}]


class Fake:
    """Stands in for subprocess.run in stock_sync: records pusher command lines."""
    def __init__(self):
        self.cmds, self.fail = [], set()

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        script = Path(cmd[1]).name.replace("_push.py", "")
        rc = 1 if script in self.fail else 0
        return type("P", (), {"stdout": f"fake {script} run\n", "returncode": rc})()


def by_script(fake):
    return {Path(c[1]).name.replace("_push.py", ""): c for c in fake.cmds}


def run_sync(*args):
    import stock_sync
    sys.argv = ["stock_sync.py", *args]
    buf, code = io.StringIO(), None
    with contextlib.redirect_stdout(buf):
        try:
            stock_sync.main()
        except SystemExit as e:
            code = e.code
    return buf.getvalue(), code


def tests(tmp):
    import amazon_push
    import ebay_push
    import push_all
    import stock_sync
    amazon_push.SPAPI_HOST = BASE + "/amazon"
    amazon_push.LWA_URL = BASE + "/amazon/auth/o2/token"
    orig = ebay_push.Ebay.__init__

    def ebay_init(self, *a, **k):
        orig(self, *a, **k)
        self.host = BASE + "/ebay"
    ebay_push.Ebay.__init__ = ebay_init
    stock_sync.time.sleep = lambda s: None

    books = Path(tmp) / "books.json"
    books.write_text(json.dumps(BOOKS), encoding="utf-8")
    old = time.time() - 3 * 3600                     # scraped 3 hours ago
    os.utime(books, (old, old))
    cfg = {"json_file": str(books), "pushers": {
        "casadellibro": {"enabled": True, "args": []}, "amazon": {"enabled": True, "args": ["--markets", "ES,DE"]},
        "ebay": {"enabled": True, "args": ["--markets", "all"]}, "kaufland": {"enabled": True, "args": ["--storefronts", "all"]},
        "galaxus": {"enabled": True, "args": []}, "fnac": {"enabled": True, "args": []},
        "vinted": {"enabled": True, "args": []}, "bol": {"enabled": False, "args": []}}}
    cfg_file = Path(tmp) / "push_all_config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    push_all.CONFIG_FILE = cfg_file
    fake = Fake()
    stock_sync.subprocess.run = fake

    sold = now_iso(-3600)                            # an hour ago: after the scrape
    S.mirakl_orders = [{"order_id": "M1", "order_state": "WAITING_ACCEPTANCE", "created_date": sold,
                        "order_lines": [{"order_line_id": "M1-1", "offer_sku": "B1", "quantity": 1,
                                         "order_line_state": "WAITING_ACCEPTANCE"}]},
                       {"order_id": "M2", "order_state": "REFUSED", "created_date": sold,
                        "order_lines": [{"order_line_id": "M2-1", "offer_sku": "B6", "quantity": 1,
                                         "order_line_state": "REFUSED"}]}]
    S.amazon_orders = [{"orderId": "A1", "createdTime": sold, "fulfillment": {"fulfillmentStatus": "UNSHIPPED"},
                        "orderItems": [{"orderItemId": "A1-1", "quantityOrdered": 1, "product": {"sellerSku": "B2"}}]},
                       {"orderId": "A2", "createdTime": sold, "fulfillment": {"fulfillmentStatus": "SHIPPED"},
                        "orderItems": [{"orderItemId": "A2-1", "quantityOrdered": 1, "product": {"sellerSku": "OTHER"}}]},
                       {"orderId": "A3", "createdTime": sold, "fulfillment": {"fulfillmentStatus": "CANCELLED"},
                        "orderItems": [{"orderItemId": "A3-1", "quantityOrdered": 1, "product": {"sellerSku": "B6"}}]}]
    S.ebay_orders = [{"orderId": "E1", "creationDate": sold, "cancelStatus": {"cancelState": "NONE_REQUESTED"},
                      "lineItems": [{"lineItemId": "E1-1", "sku": "B3", "quantity": 1}]}]
    S.kaufland_units = {"de": [{"id_order_unit": 77, "id_offer": "B5", "status": "need_to_be_sent",
                                "ts_created_iso": sold}]}

    out, code = run_sync("sync", "--no-apply")
    synced = {b["id"]: b for b in json.loads((Path(tmp) / "books_synced.json").read_text(encoding="utf-8"))}
    ok(code is None and "5 new sale(s)" in out, "orders read from Mirakl, Amazon (2 pages), eBay and Kaufland: 5 sales")
    ok(synced["B1"]["sold_out"] and synced["B1"]["stock"] == 0 and synced["B3"]["sold_out"] and synced["B5"]["sold_out"],
       "books sold once (stock 1) are sold out in books_synced.json")
    ok(synced["B2"]["stock"] == 1 and not synced["B2"].get("sold_out"), "stock 2, one sold: stock 1 left, still for sale")
    ok("stock" not in synced["B6"] and not synced["B6"].get("sold_out"),
       "refused (Mirakl) and cancelled (Amazon) orders don't count")
    ok(synced["B4"]["sold_out"] and "stock" not in synced["B4"], "sold out at the source stays as it is")
    ok(not fake.cmds and "not sent (--no-apply)" in out, "--no-apply: no pusher started")
    amz = [q for api, q in S.requests if api == "amazon"]
    ok(amz[0]["marketplaceIds"].count(",") >= 9 and amz[0]["includedData"] == "FULFILLMENT"
       and any("paginationToken" in q for q in amz), "Amazon: all EU marketplaces, FULFILLMENT data, next page read")
    tok = [f for api, f in S.requests if api == "ebay-token"]
    ok(tok and "sell.fulfillment" in tok[-1]["scope"][0], "eBay: token requested with the sell.fulfillment scope")
    kf = {q["storefront"] for api, q in S.requests if api == "kaufland"}
    ok(kf == {"es", "de", "at", "fr", "it", "nl", "sk", "cz", "pl"}, "Kaufland: every storefront read")
    ok("worten: not configured" in out, "channels without credentials are skipped")

    S.requests = []
    out, code = run_sync("sync")
    cmds = by_script(fake)
    ok(code is None and "0 new sale(s)" in out, "second run: overlapping window, no sale counted twice")
    mq = [q for api, q in S.requests if api == "mirakl"]
    ok(mq and mq[0]["start_update_date"] < now_iso(-3500), "next read starts at the last cursor minus the overlap")
    ok(set(cmds) == {"casadellibro", "amazon", "ebay", "kaufland", "galaxus"},
       "every enabled, configured pusher with a sold-out option is run")
    changes = json.loads((Path(tmp) / "books_sync_changes.json").read_text(encoding="utf-8"))
    ok(sorted(b["id"] for b in changes) == ["B1", "B2", "B3", "B5"], "pushers get only the 4 changed books")
    ok(cmds["kaufland"][3].endswith("books_sync_changes.json") and "--zero-sold-out" in cmds["kaufland"]
       and "--storefronts" in cmds["kaufland"] and "--resend" in cmds["kaufland"] and "--yes" in cmds["kaufland"],
       "Kaufland: changed books, its push_all args, --zero-sold-out, --resend (stock 2 -> 1), --yes")
    ok("--zero-sold-out" in cmds["amazon"] and "--resend" not in cmds["amazon"] and "--zero-sold-out" in cmds["ebay"]
       and "--zero-sold-out" in cmds["casadellibro"], "Amazon, eBay, Mirakl: --zero-sold-out")
    ok(cmds["galaxus"][3].endswith("books_synced.json"), "feed pusher (Galaxus) gets the full synced file")
    ok("fnac" in out and "CANNOT SYNC" in out and "vinted" in out, "pushers without a sold-out option are reported")

    fake.cmds = []
    out, code = run_sync("sync")
    ok(not fake.cmds and "0 stock change(s)" in out, "nothing changed: no pusher started")

    S.ebay_orders[0]["cancelStatus"] = {"cancelState": "CANCELED"}
    out, code = run_sync("sync")
    changes = json.loads((Path(tmp) / "books_sync_changes.json").read_text(encoding="utf-8"))
    ok("cancelled B3" in out and [b["id"] for b in changes] == ["B3"] and not changes[0].get("sold_out")
       and "--resend" in by_script(fake)["kaufland"], "eBay order cancelled: stock back on every marketplace")

    fake.cmds, fake.fail = [], {"amazon"}
    S.kaufland_units["es"] = [{"id_order_unit": 78, "id_offer": "B2", "status": "need_to_be_sent", "ts_created_iso": now_iso()}]
    out, code = run_sync("sync")
    ok(code == 1 and "sent again on the next run" in out, "a pusher fails: run reports it")
    fake.cmds, fake.fail = [], set()
    out, code = run_sync("sync")
    changes = json.loads((Path(tmp) / "books_sync_changes.json").read_text(encoding="utf-8"))
    ok(code is None and [b["id"] for b in changes] == ["B2"] and changes[0]["sold_out"],
       "... and the same change is sent again on the next run")

    S.mirakl_fail = True
    S.requests = []
    out, code = run_sync("sync")
    led = json.loads((Path(tmp) / "stock_ledger.json").read_text(encoding="utf-8"))
    ok("casadellibro: could not read orders" in out and "amazon:" in out and code is None,
       "one channel failing doesn't stop the others")
    before = led["cursors"]["casadellibro"]
    S.mirakl_fail = False

    future = time.time() + 30 * 3600                # the scraper ran again, more than SETTLE_HOURS later
    os.utime(books, (future, future))
    fake.cmds = []
    out, code = run_sync("sync")
    synced = {b["id"]: b for b in json.loads((Path(tmp) / "books_synced.json").read_text(encoding="utf-8"))}
    ok("stock" not in synced["B1"] and not synced["B1"].get("sold_out") and fake.cmds,
       "a newer scrape (> SETTLE_HOURS after the sale) replaces our sales: source stock is used again")

    out, code = run_sync("status")
    ok("orders read up to" in out and "kaufland" in out, "status shows the channels and cursors")

    # push_all runs the sync first and lists the synced stock
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if Path(cmd[1]).name == "stock_sync.py":
            return type("P", (), {"stdout": "   2 new sale(s), 0 cancelled; ...\n", "returncode": 0})()
        return type("P", (), {"stdout": "ok\n", "returncode": 0})()
    push_all.subprocess.run = fake_run
    push_all.LOG_DIR = Path(tmp) / "logs"
    push_all.LOCK_FILE = Path(tmp) / "push_all.lock"
    cfg2 = dict(cfg, stock_sync={"enabled": True},
                pushers={"kaufland": {"enabled": True, "args": []}})
    cfg_file.write_text(json.dumps(cfg2), encoding="utf-8")
    sys.argv = ["push_all.py", "--no-email"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            push_all.main()
        except SystemExit:
            pass
    report = buf.getvalue()
    ok(Path(calls[0][1]).name == "stock_sync.py" and "--source" in calls[0]
       and Path(calls[1][1]).name == "kaufland_push.py" and calls[1][3].endswith("books_synced.json")
       and "Stock sync" in report and "2 new sale(s)" in report,
       "push_all: stock sync first (in the report), then pushers list books_synced.json")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory() as tmp:
        for k in [k for k in os.environ if k.split("_")[0] in ("CASADELLIBRO", "AMAZON", "EBAY", "KAUFLAND",
                                                               "WORTEN", "EMPIK", "IBS", "CARREFOUR", "LECLERC",
                                                               "ELCORTEINGLES", "GALAXUS", "FNAC", "BOL")]:
            del os.environ[k]
        os.environ.update(ENV, STOCK_SYNC_DIR=tmp)
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            tests(tmp)
        finally:
            os.chdir(cwd)
    failed = [n for good, n in RESULTS if not good]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
