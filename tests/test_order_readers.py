#!/usr/bin/env python3
"""
Test for the order readers in stock_sync.py that were added for the other
marketplaces: bol.com, Cdiscount (Octopia), Allegro, OTTO, eMAG, CDON, OnBuy,
booklooker, Fruugo, Vinted Pro, Temu and TikTok Shop.

Each marketplace client (the pushers' own API classes, which have their own
HTTP-level tests) is replaced by a fake that answers like the marketplace's
documented order API. The test checks what the reader asks for (filters,
paging) and what it makes of the answer: our SKU (or the EAN for bol.com),
quantities, cancellations and partial cancellations. Fruugo is read over
HTTP from a local mock (XML with the o: namespace).

    python tests/test_order_readers.py
"""
import json
import os
import sys
import tempfile
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import stock_sync as ss            # noqa: E402

RESULTS = []
CALLS = []


def ok(cond, name):
    RESULTS.append((bool(cond), name))
    print(("PASS " if cond else "FAIL ") + name)


def lines(out):
    return {(s["sku"] or s.get("ean"), s["qty"], s["cancelled"]) for s in out}


# ------------------------------------------------------------------ fakes

class FakeBol:
    def call(self, method, path, media=None, params=None, body=None):
        CALLS.append(("bol", path, dict(params or {})))
        if params["page"] == 1:
            orders = [{"orderId": f"O{i}", "orderPlacedDateTime": "2026-11-10T08:00:00+01:00",
                       "orderItems": [{"orderItemId": f"I{i}", "ean": "9788409591923", "quantity": 1,
                                       "quantityCancelled": 0}]} for i in range(49)]
            orders.append({"orderId": "OX", "orderPlacedDateTime": "2026-11-10T08:00:00+01:00",
                           "orderItems": [{"orderItemId": "IX", "ean": "9788410413290", "quantity": 2,
                                           "quantityCancelled": 2}]})
            return 200, {"orders": orders}
        return 200, {"orders": [{"orderId": "O99", "orderPlacedDateTime": "2026-11-10T09:00:00Z",
                                 "orderItems": [{"orderItemId": "I99", "ean": "9780306406157", "quantity": 3,
                                                 "quantityCancelled": 1}]}]}


class FakeOctopia:
    def call(self, method, path, params=None, body=None, channel=False):
        CALLS.append(("cdiscount", path, dict(params or {})))
        r = type("R", (), {"status_code": 200})()
        return r, {"itemsPerPage": 100, "items": [
            {"orderId": "C1", "status": "Accepted", "createdAt": "2026-11-10T08:00:00Z",
             "lines": [{"orderLineId": "L1", "status": "Accepted", "quantity": 1,
                        "offer": {"sellerProductId": "SKU-C1", "productGtin": "9788409591923"}},
                       {"orderLineId": "L2", "status": "Cancelled", "quantity": 1,
                        "offer": {"sellerProductId": "SKU-C2"}}]},
            {"orderId": "C2", "status": "Refused", "createdAt": "2026-11-10T08:00:00Z",
             "lines": [{"orderLineId": "L3", "status": "Refused", "quantity": 1, "offer": {"productGtin": "9788410413290"}}]}]}


class FakeAllegro:
    def get(self, path, params=None):
        CALLS.append(("allegro", path, list(params or [])))
        return {"checkoutForms": [
            {"id": "F1", "status": "READY_FOR_PROCESSING", "updatedAt": "2026-11-10T08:00:00Z",
             "lineItems": [{"id": "LI1", "quantity": 2, "boughtAt": "2026-11-10T07:00:00Z",
                            "offer": {"id": "777", "external": {"id": "SKU-A"}}}]},
            {"id": "F2", "status": "CANCELLED", "lineItems": [{"id": "LI2", "quantity": 1,
                                                              "offer": {"id": "778", "external": {"id": "SKU-B"}}}]}]}


class FakeOtto:
    def call(self, method, path, params=None, body=None):
        CALLS.append(("otto", path, dict(params or {})))
        if path == "/v4/orders":
            return 200, {"resources": [{"orderDate": "2026-11-10T08:00:00Z", "positionItems": [
                {"positionItemId": "P1", "fulfillmentStatus": "PROCESSABLE", "product": {"sku": "SKU-O"}},
                {"positionItemId": "P2", "fulfillmentStatus": "PROCESSABLE", "product": {"sku": "SKU-O"}}]}],
                "links": [{"rel": "next", "href": "/v4/orders?nextcursor=abc"}]}
        return 200, {"resources": [{"orderDate": "2026-11-10T08:00:00Z", "positionItems": [
            {"positionItemId": "P3", "fulfillmentStatus": "CANCELLED_BY_MARKETPLACE", "product": {"sku": "SKU-O2"}}]}],
            "links": []}


class FakeEmag:
    def read_all(self, path, filters=None):
        CALLS.append(("emag", path, dict(filters or {})))
        yield {"id": 501, "status": 1, "date": "2026-11-10 10:00:00",
               "products": [{"id": 1, "product_id": 3, "quantity": 1, "status": 1},
                            {"id": 2, "product_id": 4, "quantity": 1, "status": 0}]}
        yield {"id": 502, "status": 0, "date": "2026-11-10 10:00:00",
               "products": [{"id": 3, "product_id": 3, "quantity": 1, "status": 1}]}


class FakeCdon:
    def call(self, method, path, body=None, params=None):
        CALLS.append(("cdon", path, dict(params or {})))
        st = params["state"]
        rows = {"CREATED": [{"id": "D1", "article_sku": "SKU-D", "quantity": 1, "state": "CREATED",
                             "created_at": "2026-11-10T08:00:00Z", "updated_at": "2026-11-10T08:00:00Z"}],
                "ACCEPTED": [],
                "FULFILLED": [{"id": "D0", "article_sku": "OLD", "quantity": 1, "state": "FULFILLED",
                               "created_at": "2026-01-01T08:00:00Z", "updated_at": "2026-01-02T08:00:00Z"}],
                "NOT_FULFILLED": [{"id": "D2", "article_sku": "SKU-E", "quantity": 1, "state": "NOT_FULFILLED",
                                   "created_at": "2026-11-10T08:00:00Z", "updated_at": ss.iso(ss.utcnow())}]}[st]
        return 200, rows


class FakeOnBuy:
    def pages(self, path, params, limit=100):
        CALLS.append(("onbuy", path, dict(params)))
        if params["site_id"] == 2000:
            yield {"order_id": "TNQ2K", "status": "Awaiting Dispatch", "date": "2026-11-10 08:00:00",
                   "products": [{"onbuy_internal_reference": 9, "sku": "SKU-U", "quantity": "2"}]}
            yield {"order_id": "TNQ2L", "status": "Cancelled By Buyer", "date": "2026-11-10 08:00:00",
                   "products": [{"onbuy_internal_reference": 10, "sku": "SKU-V", "quantity": "1"}]}


class FakeBooklooker:
    def call(self, method, interface, params=None, files=None):
        CALLS.append(("booklooker", interface, dict(params or {})))
        return [{"orderId": 7, "orderDate": "2026-11-10", "orderTime": "10:00:00", "status": "READY_FOR_SHIPMENT",
                 "orderItems": [{"orderItemId": 71, "orderNo": "SKU-K", "amount": 1, "status": "Ordered"},
                                {"orderItemId": 72, "orderNo": "SKU-L", "amount": 1, "status": "Canceled"}]}]


class FakeVinted:
    def __init__(self, token, sandbox=False):
        pass

    def request(self, method, path, payload=None):
        CALLS.append(("vinted", path, {}))
        if path.startswith("/api/v1/orders?after-id="):
            after = int(path.split("=")[1])
            orders = [o for o in VINTED_ORDERS if o["id"] > after][:250]
            return 200, {"orders": orders}
        oid = int(path.rsplit("/", 1)[1])
        return 200, {"id": oid, "status": "CREATED", "created_at": "2026-11-10T08:00:00Z",
                     "items": [{"id": f"u-{oid}", "item_reference": f"SKU-V{oid}", "price": "5.00"}]}


VINTED_ORDERS = [{"id": 101, "status": "CREATED", "created_at": "2026-11-10T08:00:00Z"},
                 {"id": 102, "status": "CANCELED", "created_at": "2026-11-10T08:00:00Z"}]


class FakeTemu:
    def call(self, api_type, retries=5, **params):
        CALLS.append(("temu", api_type, dict(params)))
        return {"totalItemNum": 1, "pageItems": [{"parentOrderMap": {"parentOrderSn": "PO-1", "parentOrderStatus": 2},
                "orderList": [{"orderSn": "S1", "quantity": 2, "canceledQuantityBeforeShipment": 1, "skuId": 555,
                               "isCancelledDuringPending": False, "orderCreateTime": 1793260800,
                               "productList": [{"productSkuId": 555, "extCode": ""}]},
                              {"orderSn": "S2", "quantity": 1, "canceledQuantityBeforeShipment": 0, "skuId": 556,
                               "isCancelledDuringPending": False, "orderCreateTime": 1793260800,
                               "productList": [{"productSkuId": 556, "extCode": "SKU-T2"}]}]}]}


class FakeTikTok:
    def call(self, method, path, query=None, body=None, files=None, shop=True):
        CALLS.append(("tiktok", path, {"query": dict(query or {}), "body": dict(body or {})}))
        if not (query or {}).get("page_token"):
            return {"orders": [{"id": "T1", "status": "AWAITING_SHIPMENT", "create_time": 1793260800,
                                "line_items": [{"id": "LT1", "seller_sku": "SKU-123456"},
                                               {"id": "LT2", "seller_sku": "SKU-123456"}]}],
                    "next_page_token": "p2"}
        return {"orders": [{"id": "T2", "status": "CANCELLED", "create_time": 1793260800,
                            "line_items": [{"id": "LT3", "seller_sku": "SKU-ZZ"}]}], "next_page_token": ""}


# ---------------------------------------------------------------- Fruugo HTTP mock

FRUUGO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<o:orders merchantId="444" xmlns:o="https://www.fruugo.com/orders/schema">
 <o:order>
  <o:orderId>4099940001000444</o:orderId><o:orderDate>2017-06-16T13:24:24.000+03:00</o:orderDate>
  <o:orderStatus>PENDING</o:orderStatus>
  <o:orderLines>
   <o:orderLine><o:fruugoProductId>1</o:fruugoProductId><o:fruugoSkuId>11</o:fruugoSkuId>
    <o:productId>SKU-F</o:productId><o:skuId>SKU-F</o:skuId><o:totalNumberOfItems>2</o:totalNumberOfItems>
    <o:cancelledItems>1</o:cancelledItems></o:orderLine>
   <o:orderLine><o:fruugoProductId>2</o:fruugoProductId><o:fruugoSkuId>22</o:fruugoSkuId>
    <o:productId>SKU-G</o:productId><o:skuId>SKU-G</o:skuId><o:totalNumberOfItems>1</o:totalNumberOfItems>
    <o:cancelledItems>1</o:cancelledItems></o:orderLine>
  </o:orderLines>
 </o:order>
</o:orders>"""


class FruugoHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        CALLS.append(("fruugo", self.path, {"auth": self.headers.get("Authorization")}))
        import base64
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"retailer:pw").decode():
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/XML")
        self.end_headers()
        self.wfile.write(FRUUGO_XML)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import allegro_push, booklooker_push, bol_push, cdiscount_push, cdon_push, emag_push, onbuy_push  # noqa: E401
    import otto_push, temu_push, tiktok_push, vinted_api_push
    bol_push.Bol, cdiscount_push.Octopia, allegro_push.Allegro = FakeBol, FakeOctopia, FakeAllegro
    otto_push.Otto, emag_push.Emag, cdon_push.Cdon, onbuy_push.OnBuy = FakeOtto, FakeEmag, FakeCdon, FakeOnBuy
    booklooker_push.Booklooker, vinted_api_push.VintedPro = FakeBooklooker, FakeVinted
    temu_push.Temu, tiktok_push.TikTok = FakeTemu, FakeTikTok

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FruugoHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            Path("emag_ids.json").write_text(json.dumps({"SKU-E3": 3, "SKU-E4": 4}), encoding="utf-8")
            Path("onbuy_posted.json").write_text(json.dumps({"gb:SKU-U": {}, "es:SKU-X": {}}), encoding="utf-8")
            Path("temu_posted.json").write_text(json.dumps({"SKU-T1": {"sku_id": 555}}), encoding="utf-8")
            Path("tiktok_posted.json").write_text(json.dumps({"123456": {"seller_sku": "SKU-123456"}}), encoding="utf-8")
            run(httpd)
        finally:
            os.chdir(cwd)
    failed = [n for good, n in RESULTS if not good]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


def calls(name):
    return [c for c in CALLS if c[0] == name]


def run(httpd):
    now = ss.utcnow()
    recent, old = now - timedelta(minutes=20), now - timedelta(hours=5)

    out = ss.read_bol(recent)
    b = calls("bol")
    ok(len(out) == 51 and b[0][2]["status"] == "ALL" and b[0][2]["change-interval-minute"] == 60
       and [c[2]["page"] for c in b] == [1, 2], "bol: status=ALL, change-interval-minute=60, pages of 50")
    ok(("9788410413290", 1, True) in lines(out) and ("9780306406157", 2, False) in lines(out)
       and all(s["sku"] == "" and s.get("ean") for s in out), "bol: EAN reported (mapped to the book later), "
       "fully cancelled item = cancelled, 3 - 1 cancelled = 2")
    CALLS.clear()
    ss.read_bol(old)
    ok("latest-change-date" in calls("bol")[0][2] and "change-interval-minute" not in calls("bol")[0][2],
       "bol: after a gap > 55 min, latest-change-date instead (never both)")

    out = ss.read_cdiscount(recent)
    c = calls("cdiscount")[0]
    ok(c[1] == "/orders" and c[2]["pageSize"] == 100 and "updatedAtMin" in c[2], "Cdiscount: /orders, updatedAtMin, pageSize 100")
    ok(lines(out) == {("SKU-C1", 1, False), ("SKU-C2", 1, True), ("9788410413290", 1, True)},
       "Cdiscount: sellerProductId, cancelled line, refused order (EAN fallback)")

    out = ss.read_allegro(recent)
    p = dict(calls("allegro")[0][2])
    ok("updatedAt.gte" in p and p["limit"] == 100, "Allegro: checkout-forms with updatedAt.gte, limit 100")
    ok(lines(out) == {("SKU-A", 2, False), ("SKU-B", 1, True)}, "Allegro: offer.external.id, quantity, CANCELLED")

    out = ss.read_otto(recent)
    o = calls("otto")
    import otto_push
    ok(otto_push.SCOPES == "orders" and o[0][2]["fromDate"] and o[1][1] == "/v4/orders?nextcursor=abc",
       "OTTO: token scope 'orders', fromDate, follows the next link")
    ok(len(out) == 3 and sum(1 for s in out if s["sku"] == "SKU-O" and not s["cancelled"]) == 2
       and ("SKU-O2", 1, True) in lines(out), "OTTO: one position item per copy; CANCELLED_BY_* = cancelled")

    out = ss.read_emag(recent)
    e = calls("emag")[0][2]
    ok("modifiedAfter" in e and "modifiedBefore" in e, "eMAG: modifiedAfter + modifiedBefore (both needed)")
    ok(lines(out) == {("SKU-E3", 1, False), ("SKU-E4", 1, True), ("SKU-E3", 1, True)},
       "eMAG: product_id mapped via emag_ids.json; cancelled line and cancelled order")

    out = ss.read_cdon(recent)
    ok({c[2]["state"] for c in calls("cdon")} == {"CREATED", "ACCEPTED", "FULFILLED", "NOT_FULFILLED"},
       "CDON: every state read (the API has no date filter)")
    ok(lines(out) == {("SKU-D", 1, False), ("SKU-E", 1, True)}, "CDON: article_sku; old orders ignored; NOT_FULFILLED = cancelled")

    out = ss.read_onbuy(recent)
    sites = {c[2]["site_id"] for c in calls("onbuy")}
    ok(sites == {2000, 2013} and all(c[2]["filter[status]"] == "all" and c[2]["filter[modified_since]"] for c in calls("onbuy")),
       "OnBuy: every site in use, filter[status]=all, filter[modified_since]")
    ok(lines(out) == {("SKU-U", 2, False), ("SKU-V", 1, True)}, "OnBuy: sku, quantity (string), cancelled status")

    out = ss.read_booklooker(recent)
    bl = calls("booklooker")[0]
    ok(bl[1] == "order" and bl[2]["dateFrom"] and bl[2]["dateTo"], "booklooker: GET order with dateFrom / dateTo")
    ok(lines(out) == {("SKU-K", 1, False), ("SKU-L", 1, True)}, "booklooker: orderNo, amount, Canceled item")

    os.environ.update(FRUUGO_USERNAME="retailer", FRUUGO_PASSWORD="pw",
                      FRUUGO_ORDERS_URL=f"http://127.0.0.1:{httpd.server_address[1]}/orders/download")
    out = ss.read_fruugo(recent)
    f = calls("fruugo")[0]
    ok(f[1].startswith("/orders/download?from=") and f[2]["auth"].startswith("Basic "), "Fruugo: /orders/download?from=, Basic auth")
    ok(lines(out) == {("SKU-F", 1, False), ("SKU-G", 1, True)}, "Fruugo: skuId; total - cancelled items")

    os.environ["VINTED_PRO_TOKEN"] = "a,b"
    ss.LEDGER.clear()
    out = ss.read_vinted(recent)
    ok(lines(out) == {("SKU-V101", 1, False), ("SKU-V102", 1, True)} and ss.LEDGER["vinted_after_id"] == 102,
       "Vinted: new orders read after the last id, items' item_reference, CANCELED")
    ss.LEDGER["sales"] = {s["key"]: s for s in out}
    VINTED_ORDERS[0]["status"] = "CANCELED"
    CALLS.clear()
    out = ss.read_vinted(recent)
    ok(all(s["cancelled"] for s in out if s["sku"] == "SKU-V101")
       and not [c for c in calls("vinted") if not c[1].startswith("/api/v1/orders?")],
       "Vinted: a known order that gets cancelled is updated without re-reading its details")

    out = ss.read_temu(recent)
    t = calls("temu")[0]
    ok(t[1] == "bg.order.list.v2.get" and t[2]["updateAtStart"] and t[2]["pageSize"] == 100,
       "Temu: bg.order.list.v2.get with updateAtStart (seconds)")
    ok(lines(out) == {("SKU-T1", 1, False), ("SKU-T2", 1, False)},
       "Temu: extCode, or skuId via temu_posted.json; cancelled quantity taken off")

    out = ss.read_tiktok(recent)
    tk = calls("tiktok")
    ok(tk[0][1] == "/order/202309/orders/search" and tk[0][2]["body"]["update_time_ge"]
       and tk[1][2]["query"]["page_token"] == "p2", "TikTok: orders/search with update_time_ge, next page token")
    ok(sum(1 for s in out if s["sku"] == "123456" and not s["cancelled"]) == 2 and ("SKU-ZZ", 1, True) in lines(out),
       "TikTok: seller_sku mapped back to our SKU, one line item per copy, CANCELLED")

    # bol.com reports only the EAN: the sync matches it to the book with that ISBN
    import argparse
    Path("books.json").write_text(json.dumps([{"id": "BK", "title": "x", "isbn": "978-84-09591-92-3"},
                                             {"id": "OTHER", "title": "y", "isbn": "9788410413290"}]), encoding="utf-8")
    for name in ("LEDGER_FILE", "SYNCED_FILE", "CHANGES_FILE"):
        setattr(ss, name, Path(getattr(ss, name).name).resolve())
    real = ss.readers
    ss.readers = lambda: [("bol", lambda since: [ss.sale("bol", "bol:9:1", None, 1, False, ss.iso(ss.utcnow()),
                                                         ean="9788409591923")], [])]
    from contextlib import redirect_stdout
    import io
    with redirect_stdout(io.StringIO()):
        ss.cmd_sync(argparse.Namespace(source="books.json", dry_run=False, no_apply=True, verbose=False))
    ss.readers = real
    synced = {b["id"]: b for b in json.loads(Path("books_synced.json").read_text(encoding="utf-8"))}
    ok(synced["BK"].get("sold_out") and not synced["OTHER"].get("sold_out"),
       "sync: a bol.com sale (EAN only) takes the right book down")

    names = {n for n, _, _ in ss.readers()}
    ok({"bol", "cdiscount", "allegro", "otto", "emag", "cdon", "onbuy", "booklooker", "fruugo", "vinted", "temu",
        "tiktok", "amazon", "ebay", "kaufland", "casadellibro"} <= names, "all readers registered in stock_sync")


if __name__ == "__main__":
    main()
