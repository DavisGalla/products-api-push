#!/usr/bin/env python3
"""
End-to-end test for onbuy_push.py, run against a LOCAL MOCK of the OnBuy /
OnCommerce API v2: request-token (form fields secret_key / consumer_key,
15-minute token sent raw in the Authorization header), product search by
barcode (filter[field]=product_code, max 50 per call, per site), batch
listing creation with per-row results, PUT / DELETE /listings/by-sku with
OnBuy's per-row errors, paginated listings, check-winning, seller delivery
templates and 429 rate limiting. Nothing is sent to OnBuy, and your real
onbuy_posted.json is not touched (the test runs in a temporary folder).

    python tests/test_onbuy.py
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
import onbuy_push as ob                  # noqa: E402

CK, SK = "onbuy-test-consumer", "onbuy-test-secret"
RESULTS = []


def ok(cond, name):
    RESULTS.append((bool(cond), name))
    print(("PASS " if cond else "FAIL ") + name)


def isbn(n):
    core = f"978849{n:06d}"
    d = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(d)


# OnBuy catalog: GTIN -> OPC, per site (book 3 only exists on the UK site)
CATALOG = {2000: {isbn(n): f"P{n:05d}" for n in range(1, 80)},
           2013: {isbn(n): f"P{n:05d}" for n in range(1, 80) if n != 3}}


class S:
    @classmethod
    def reset(cls):
        cls.tokens = {}            # token -> expiry
        cls.logins = 0
        cls.listings = {}          # (site, sku) -> listing
        cls.calls = []             # (method, path, size)
        cls.searches = []          # list of barcode counts
        cls.throttle_once = False
        cls.expire_next = False


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

    def read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def route(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        raw = self.read()
        path = u.path.removeprefix("/v2/")
        if path == "auth/request-token":
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            if method != "POST" or form.get("secret_key") != SK or form.get("consumer_key") != CK:
                return self.reply(401, {"error": {"message": "Authentication Failed"}})
            S.logins += 1
            tok = f"TOKEN-{S.logins}"
            S.tokens[tok] = time.time() + 900
            return self.reply(200, {"access_token": tok, "expires_at": int(S.tokens[tok])})
        tok = self.headers.get("Authorization")
        if S.expire_next and tok in S.tokens:
            S.expire_next = False
            S.tokens[tok] = 0
        if tok not in S.tokens or S.tokens[tok] < time.time():
            return self.reply(401, {"error": {"message": "Bad Token"}})
        if S.throttle_once:
            S.throttle_once = False
            return self.reply(429, {"error": {"message": "Too Many Requests Usage limit has been exceeded."}})
        body = json.loads(raw) if raw else {}
        S.calls.append((method, path, len(body.get("listings") or body.get("skus") or [])))
        site = int(q.get("site_id") or body.get("site_id") or 0)
        if site not in (2000, 2013, 2002):
            return self.reply(400, {"error": {"message": "site_id required"}})

        if method == "GET" and path == "products":
            if q.get("filter[field]") != "product_code":
                return self.reply(400, {"error": {"message": "unsupported search"}})
            codes = q.get("filter[query]", "").split(",")
            S.searches.append(len(codes))
            if len(codes) > 50:
                return self.reply(400, {"error": {"message": "max 50 product codes"}})
            res = [{"opc": CATALOG.get(site, {})[c], "name": f"Book {c}", "product_codes": [c],
                    "url": f"https://www.onbuy.com/gb/p/book~{c}/"} for c in codes if c in CATALOG.get(site, {})]
            return self.reply(200, {"results": res, "metadata": {"limit": 100, "offset": 0, "total_rows": len(res)}})

        if method == "POST" and path == "listings":
            out = []
            for row in body.get("listings") or []:
                err = None
                if row.get("opc") not in CATALOG.get(site, {}).values():
                    err = "Product not found"
                elif row.get("condition") not in ("new", "refurbished"):
                    err = "Invalid condition"
                elif not isinstance(row.get("price"), (int, float)) or row["price"] <= 0:
                    err = "Price must be a positive float value"
                elif not isinstance(row.get("stock"), int) or row["stock"] < 0:
                    err = "Stock cannot be less than 0"
                elif row.get("sku") == "C66":
                    err = "This product is restricted in your account"
                if err:
                    out.append({"success": False, "sku": row.get("sku"), "message": err})
                    continue
                key = (site, row["sku"])
                existed = key in S.listings
                S.listings[key] = dict(row, product_listing_id=str(100000 + len(S.listings)))
                out.append({"success": True, "created": not existed, "updated": existed, "sku": row["sku"],
                            "opc": row["opc"], "condition": row["condition"],
                            "product_listing_id": S.listings[key]["product_listing_id"]})
            return self.reply(200, {"success": True, "results": out})

        if method == "PUT" and path == "listings/by-sku":
            rows = body.get("listings") or []
            if len(rows) > 1000:
                return self.reply(400, {"error": {"message": "max 1000 SKUs"}})
            out = []
            for row in rows:
                r = dict(row)
                key = (site, row.get("sku"))
                if key not in S.listings:
                    r["error"] = "SKU does not exist"
                elif "price" in row and (not isinstance(row["price"], (int, float)) or row["price"] <= 0):
                    r["error"] = "Price must be a positive float value"
                elif "stock" in row and row["stock"] < 0:
                    r["error"] = "Stock cannot be less than 0"
                else:
                    S.listings[key].update({k: row[k] for k in ("price", "stock") if k in row})
                out.append(r)
            return self.reply(200, {"success": True, "results": out})

        if method == "DELETE" and path == "listings/by-sku":
            out = {}
            for sku in body.get("skus") or []:
                if S.listings.pop((site, sku), None) is None:
                    out[sku] = {"error": "SKU not found"}
                else:
                    out[sku] = {"status": "ok"}
            return self.reply(200, {"success": True, "results": out})

        if method == "GET" and path == "listings":
            limit, offset = int(q.get("limit", 50)), int(q.get("offset", 0))
            mine = [dict(v, sku=k[1], product_url=f"https://www.onbuy.com/gb/p/{v['opc']}/")
                    for k, v in sorted(S.listings.items()) if k[0] == site]
            return self.reply(200, {"results": mine[offset:offset + limit],
                                    "metadata": {"limit": limit, "offset": offset, "total_rows": len(mine)}})

        if method == "GET" and path == "listings/check-winning":
            res = [{"sku": s, "price": S.listings[(site, s)]["price"], "lead_price": 5.0,
                    "winning": S.listings[(site, s)]["price"] <= 9}
                   for s in body.get("skus") or [] if (site, s) in S.listings]
            return self.reply(200, {"results": res})

        if method == "GET" and path == "sellers/deliveries":
            return self.reply(200, {"results": [{"delivery_template_id": 1061, "name": "Royal Mail 48"}]})
        self.reply(404, {"error": {"message": f"unknown endpoint {method} {path}"}})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
ENV = {"ONBUY_API_URL": f"http://127.0.0.1:{httpd.server_address[1]}/v2",
       "ONBUY_CONSUMER_KEY": CK, "ONBUY_SECRET_KEY": SK}


def book(n, **kw):
    b = {"id": f"C{n}", "title": f"Libro {n}", "price_eur": 10, "isbn": isbn(n), "condition": "NewCondition",
         "sold_out": False, "images": [f"https://img.example.com/{n}.jpg"]}
    b.update(kw)
    return b


SETTINGS = dict(SITES=["gb"], DELIVERY_TEMPLATE_ID=None, FREE_RETURNS=None, HANDLING_TIME_DAYS=3,
                RESPONSIBLE_PERSON={"name": ""})


def run(*argv, env=None, books=None, **settings):
    for k, v in dict(SETTINGS, **settings).items():
        setattr(ob, k, v)
    for k in [k for k in os.environ if k.startswith("ONBUY_")]:
        del os.environ[k]
    os.environ.update(ENV if env is None else env)
    if books is not None:
        Path("books.json").write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
    sys.argv = ["onbuy_push.py", *argv]
    buf, code = io.StringIO(), None
    with contextlib.redirect_stdout(buf):
        try:
            ob.main()
        except SystemExit as e:
            code = str(e.code) if e.code not in (None, 0) else None
    return buf.getvalue() + (code or ""), code


def state():
    return json.loads(Path("onbuy_posted.json").read_text(encoding="utf-8"))


def calls(method, path):
    return [c for c in S.calls if c[0] == method and c[1] == path]


def tests():
    BOOKS = [book(1), book(2, price_eur=8.5), book(3), book(4, isbn="123"), book(5, sold_out=True),
             book(6, condition="UsedCondition"), book(500), book(66)]

    out, code = run("push", "books.json", "--dry-run", env={}, books=BOOKS)
    prev = json.loads(Path("onbuy_listings.json").read_text(encoding="utf-8"))
    ok(code is None and not S.calls and [x["sku"] for x in prev] == ["C1", "C2", "C3", "C500", "C66"],
       "dry run without credentials: listings built, nothing sent")
    ok("invalid ISBN" in out and "UsedCondition" in out, "skips explained: bad ISBN, used condition")
    ok(prev[0]["price"] == 8.5 and prev[0]["condition"] == "new" and prev[0]["handling_time"] == 3,
       "UK site: 10 EUR -> 8.50 GBP, condition new, handling time")

    out, code = run("check", env=dict(ENV, ONBUY_SECRET_KEY="wrong"))
    ok(code and "refused the login" in code, "wrong secret key: clear login error")
    out, code = run("check")
    ok(code is None and "Login OK" in out and "1061" in out and "RESPONSIBLE_PERSON" in out,
       "check: login, delivery templates, GPSR warning")
    out, code = run("lookup", "8408000003", "--sites", "gb,es")
    out2, _ = run("lookup", isbn(3), "--sites", "gb,es")
    ok("not in the OnBuy catalog" in out and "gb: OPC P00003" in out2 and "es: not in the OnBuy catalog" in out2,
       "lookup: per site, ISBN-10 converted")

    S.calls, S.searches = [], []
    out, code = run("push", "books.json", "--yes", "--limit", "1")
    ok(code is None and list(state()) == ["gb:C1"] and state()["gb:C1"]["opc"] == "P00001"
       and S.listings[(2000, "C1")]["price"] == 8.5, "push --limit 1: matched by ISBN, listed on the UK site")

    S.calls, S.searches = [], []
    out, code = run("push", "books.json", "--yes")
    st = state()
    ok({"gb:C2", "gb:C3"} <= set(st) and "Libro 500" in out and "not in the OnBuy catalog" in out and "gb:C500" not in st,
       "books OnBuy doesn't know are skipped")
    ok("! C66: listing rejected: This product is restricted" in out and "gb:C66" not in st,
       "per-row rejection shown, not recorded")
    ok(len(calls("POST", "listings")) == 1, "new listings sent in one batch")

    many = [book(n) for n in range(1, 79)]
    S.calls, S.searches = [], []
    S.throttle_once = True
    out, code = run("push", "books.json", "--yes", books=many)
    ok(S.searches and max(S.searches) <= 50 and sum(S.searches) == 75 and "retrying" in out,
       "barcode search in chunks of at most 50 (after a 429 retry)")
    ok(sum(1 for k in state() if k.startswith("gb:")) == 77, "77 UK listings (book 66 restricted)")

    S.expire_next = True
    logins = S.logins
    out, code = run("push", "books.json", "--yes", "--resend",
                    books=[dict(b, price_eur=12) if b["id"] == "C2" else dict(b, stock=4) if b["id"] == "C3" else b
                           for b in many])
    puts = calls("PUT", "listings/by-sku")
    ok(puts and puts[-1][2] == 2 and S.listings[(2000, "C2")]["price"] == 10.2 and S.listings[(2000, "C3")]["stock"] == 4
       and state()["gb:C2"]["price"] == 10.2, "--resend: only changed price / stock via PUT by-sku")
    ok(S.logins == logins + 2, "expired token renewed automatically (login + one renewal)")
    S.calls = []
    run("push", "books.json", "--yes", "--resend")
    ok(not calls("PUT", "listings/by-sku"), "--resend again: nothing changed, nothing sent")

    sold = [dict(b, sold_out=b["id"] in ("C4", "C5", "C7")) for b in many]
    run("push", "books.json", "--yes", "--zero-sold-out", books=sold)
    ok(all(S.listings[(2000, s)]["stock"] == 0 for s in ("C4", "C5", "C7")) and state()["gb:C4"]["stock"] == 0,
       "--zero-sold-out: stock 0")
    out, code = run("push", "books.json", "--yes", "--delete-sold-out")
    ok((2000, "C4") not in S.listings and "gb:C4" not in state() and "deleted" in out, "--delete-sold-out: removed")

    S.calls = []
    out, code = run("push", "books.json", "--yes", "--sites", "gb,es", books=many[:5])
    es = {k for k in state() if k.startswith("es:")}
    ok(es == {"es:C1", "es:C2", "es:C4", "es:C5"} and S.listings[(2013, "C1")]["price"] == 10.0,
       "--sites gb,es: Spanish site listed separately in EUR (book 3 unknown there)")
    ok(not [c for c in calls("POST", "listings")] or all(n <= 4 for _, _, n in calls("POST", "listings")),
       "UK books already listed are not sent again")

    out, code = run("status", "--sites", "gb,es")
    ok("C1: 8.5 GBP" in out and "winning" in out and "not winning" in out and "--- es" in out,
       "status: price, stock, buy box per site")

    out, code = run("push", "books.json", "--dry-run", "--sites", "xx")
    ok(code and "Unknown OnBuy site" in code, "unknown site refused")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ob.time.sleep = lambda s: None
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            tests()
        finally:
            os.chdir(cwd)
    failed = [n for good, n in RESULTS if not good]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
