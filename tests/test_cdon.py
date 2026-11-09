#!/usr/bin/env python3
"""
End-to-end test for cdon_push.py, run against a LOCAL MOCK of the CDON
merchant API (new platform, docs.cdon.com): Basic auth with merchantID:token,
POST/PUT /v2/articles/bulk (max 100 per call, required fields and limits,
market/currency pairs, no HTML, update actions with their own bodies),
asynchronous processing with /v1/statuses/batch and /v1/statuses/sku
(pending first, then ok / failed with error codes such as
sku_already_exists), categories, shipping configurations and 429 rate
limiting. Nothing is sent to CDON, and your real cdon_posted.json is not
touched (the test runs in a temporary folder).

    python tests/test_cdon.py
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import cdon_push as cp                   # noqa: E402

MERCHANT, TOKEN = "cdon-test-merchant", "cdon-test-token"
CURRENCY = {"SE": "SEK", "DK": "DKK", "FI": "EUR", "NO": "NOK"}
LANGS = {"sv-SE", "en-US", "da-DK", "fi-FI", "nb-NO"}
CATEGORIES = [{"id": 1001, "name": "Books", "path": "Media.Books", "search_friendly_name": "books"},
              {"id": 1002, "name": "Children's books", "path": "Media.Books.Children's books",
               "search_friendly_name": "childrens books"},
              {"id": 2001, "name": "Toys", "path": "Toys", "search_friendly_name": "toys"}]
RESULTS = []


def ok(cond, name):
    RESULTS.append((bool(cond), name))
    print(("PASS " if cond else "FAIL ") + name)


def isbn(n):
    core = f"978849{n:06d}"
    d = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(d)


class S:
    @classmethod
    def reset(cls):
        cls.articles = {}        # sku -> article as stored by CDON
        cls.batches = {}         # batch id -> [status dicts]
        cls.polls = {}           # batch id -> number of polls
        cls.calls = []           # (method, path, n items)
        cls.throttle_once = False
        cls.existing = {"PRE-EXISTING"}   # SKUs that already exist at CDON (made by hand)
        cls.bad_gtin = {isbn(77)}         # CDON rejects these asynchronously


S.reset()


# --------------------------------------------------------------------- validation

def check_texts(v, lo, hi, name, errs):
    if not isinstance(v, list) or not 1 <= len(v) <= 10:
        errs.append((name, "must be a list of 1..10 language entries"))
        return
    for t in v:
        if t.get("language") not in LANGS:
            errs.append((name, f"unknown language {t.get('language')}"))
        val = t.get("value")
        if not isinstance(val, str) or not lo <= len(val) <= hi:
            errs.append((name, f"value must be {lo}..{hi} characters"))
        elif name == "description" and "<" in val and ">" in val:
            errs.append((name, "HTML is not allowed"))


def check_common(a, errs, markets_needed=True):
    for f in ("status", "main_image", "markets", "shipping_time", "title", "description"):
        if f not in a:
            errs.append((f, "required"))
    if a.get("status") not in (None, "for sale", "paused"):
        errs.append(("status", "must be 'for sale' or 'paused'"))
    mi = a.get("main_image")
    if mi is not None and (not isinstance(mi, str) or not mi.startswith("http") or len(mi) > 1500 or "bad-image" in mi):
        errs.append(("main_image", "not a valid image URL"))
    ms = a.get("markets") or []
    if markets_needed and (not ms or any(m not in CURRENCY for m in ms)):
        errs.append(("markets", "must be SE, DK, FI or NO"))
    for s in a.get("shipping_time") or []:
        if s.get("market") not in CURRENCY or not (0 <= s.get("min", -1) <= 21) or not (1 <= s.get("max", 0) <= 21):
            errs.append(("shipping_time", "min 0..21, max 1..21"))
    if "title" in a:
        check_texts(a["title"], 5, 150, "title", errs)
    if "description" in a:
        check_texts(a["description"], 10, 4096, "description", errs)
    if len(a.get("images") or []) > 10:
        errs.append(("images", "max 10"))
    if "gtin" in a and not (isinstance(a["gtin"], str) and 8 <= len(a["gtin"]) <= 14 and a["gtin"].isdigit()):
        errs.append(("gtin", "8..14 digits"))
    if "category" in a and not (isinstance(a["category"], str) and 1 <= len(a["category"]) <= 10):
        errs.append(("category", "1..10 characters"))
    if "brand" in a and not 1 <= len(a["brand"]) <= 50:
        errs.append(("brand", "1..50 characters"))
    for c in a.get("classifications") or []:
        if c.get("name") == "CONDITION" and c.get("value") not in ("NEW", "REFURB"):
            errs.append(("classifications", "CONDITION must be NEW or REFURB"))
    m = a.get("manufacturer")
    if m is not None and (not m.get("name") or not all((m.get("address") or {}).get(k)
                                                      for k in ("street_address", "city", "postal_code", "country"))):
        errs.append(("manufacturer", "name and full address required"))


def check_price(price, errs):
    if not isinstance(price, list) or not 1 <= len(price) <= 10:
        errs.append(("price", "1..10 entries"))
        return
    for p in price:
        v = p.get("value") or {}
        if "amount" in v:
            errs.append(("price", "the field is amount_including_vat"))
        amt = v.get("amount_including_vat")
        if not isinstance(amt, (int, float)) or amt <= 0:
            errs.append(("price", "amount_including_vat must be a positive number"))
        if CURRENCY.get(p.get("market")) != v.get("currency"):
            errs.append(("price", f"currency {v.get('currency')} does not match market {p.get('market')}"))


def new_batch(statuses):
    bid = str(uuid.uuid4())
    S.batches[bid] = statuses
    S.polls[bid] = 0
    return bid


def market_status(markets, code=None):
    return [{"market": m, "status": "failed" if code else "ok", "error_code": code} for m in markets]


# ------------------------------------------------------------------- mock server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, data, headers=None):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        want = "Basic " + base64.b64encode(f"{MERCHANT}:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.reply(401, {"message": "Unauthorized"})
            return False
        if S.throttle_once:
            S.throttle_once = False
            self.reply(429, {"detail": "Rate limit exceeded"}, {"X-RateLimit-Burst-Reset": "0"})
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self.authed():
            return
        path = self.path.split("?")[0]
        S.calls.append(("GET", path, 0))
        if path.startswith("/api/v1/categories/"):
            parts = path.strip("/").split("/")
            if len(parts) != 5 or parts[3] not in CURRENCY or parts[4] not in LANGS:
                return self.reply(400, {"message": "invalid market or language"})
            return self.reply(200, CATEGORIES)
        if path == "/api/v1/shipping-configurations":
            return self.reply(200, [{"id": "sc-se-1", "name": "Postnord", "market": "SE"}])
        self.reply(404, {"message": "not found"})

    def do_POST(self):
        if not self.authed():
            return
        path, data = self.path.split("?")[0], self.body()
        if path == "/api/v2/articles/bulk":
            arts = data.get("articles")
            S.calls.append(("POST", path, len(arts or [])))
            if not isinstance(arts, list) or not 1 <= len(arts) <= 100:
                return self.reply(400, {"message": "articles: 1..100 items", "errors": {"articles": "size"}})
            success, failed, statuses = [], [], []
            for a in arts:
                errs = []
                for f in ("sku", "quantity", "price"):
                    if f not in a:
                        errs.append((f, "required"))
                if not 1 <= len(str(a.get("sku", ""))) <= 64:
                    errs.append(("sku", "1..64 characters"))
                if not isinstance(a.get("quantity"), int) or not 0 <= a["quantity"] <= 500000:
                    errs.append(("quantity", "0..500000"))
                check_common(a, errs)
                check_price(a.get("price"), errs)
                if errs:
                    failed.append({"sku": a.get("sku"), "errors": [{"message": m, "field": [f]} for f, m in errs]})
                    continue
                success.append({"id": str(uuid.uuid4()), "sku": a["sku"]})
                code = None
                if a["sku"] in S.existing or a["sku"] in S.articles:
                    code = "sku_already_exists"
                elif a.get("gtin") in S.bad_gtin:
                    code = "invalid_classification"
                elif a.get("category") and a["category"] not in {str(c["id"]) for c in CATEGORIES}:
                    code = "invalid_category"
                if code is None:
                    S.articles[a["sku"]] = json.loads(json.dumps(a))
                statuses.append({"correlation_id": str(uuid.uuid4()), "article_id": None, "sku": a["sku"],
                                 "action": "create_article", "markets": market_status(a["markets"], code)})
            bid = new_batch(statuses)
            return self.reply(202, {"batch_id": bid, "success": success, "failed": failed})
        if path in ("/api/v1/statuses/batch", "/api/v1/statuses/sku"):
            S.calls.append(("POST", path, 0))
            if path.endswith("batch"):
                out = []
                for bid in data.get("batch_ids") or []:
                    if bid not in S.batches:
                        continue
                    S.polls[bid] += 1
                    sts = S.batches[bid]
                    if S.polls[bid] == 1:      # first look: still processing
                        sts = [dict(s, markets=[dict(m, status="pending", error_code=None) for m in s["markets"]])
                               for s in sts]
                    out.append({"id": bid, "statuses": sts})
                return self.reply(200, {"batches": out})
            skus = set(data.get("skus") or [])
            sts = [s for b in S.batches.values() for s in b if s["sku"] in skus]
            return self.reply(200, {"batches": [{"id": "by-sku", "statuses": sts}]})
        self.reply(404, {"message": "not found"})

    def do_PUT(self):
        if not self.authed():
            return
        path, data = self.path.split("?")[0], self.body()
        if path != "/api/v2/articles/bulk":
            return self.reply(404, {"message": "not found"})
        acts = data.get("actions")
        S.calls.append(("PUT", path, len(acts or [])))
        if not isinstance(acts, list) or not 1 <= len(acts) <= 100:
            return self.reply(400, {"message": "actions: 1..100 items"})
        success, failed, statuses = [], [], []
        for act in acts:
            sku, kind, body, errs = act.get("sku"), act.get("action"), act.get("body"), []
            if kind == "update_article_price":
                check_price((body or {}).get("price"), errs)
            elif kind == "update_article_quantity":
                q = (body or {}).get("quantity")
                if not isinstance(q, int) or not 0 <= q <= 500000:
                    errs.append(("quantity", "0..500000"))
            elif kind == "update_article":
                for f in ("price", "quantity", "sku"):
                    if f in (body or {}):
                        errs.append((f, "not allowed in update_article"))
                check_common(body or {}, errs)
            elif kind == "delete_article":
                if body:
                    errs.append(("body", "delete_article takes no body"))
            else:
                errs.append(("action", f"unknown action {kind}"))
            if errs:
                failed.append({"sku": sku, "action": kind, "errors": [{"message": m, "field": [f]} for f, m in errs]})
                continue
            success.append({"sku": sku, "action": kind})
            art = S.articles.get(sku)
            code = None if (art or sku in S.existing) else "article_does_not_exist"
            if art is not None:
                if kind == "update_article_price":
                    art["price"] = body["price"]
                elif kind == "update_article_quantity":
                    art["quantity"] = body["quantity"]
                elif kind == "update_article":
                    art.update(body)
                elif kind == "delete_article":
                    S.articles.pop(sku)
            statuses.append({"correlation_id": str(uuid.uuid4()), "sku": sku, "action": kind,
                             "markets": market_status((art or {}).get("markets") or ["SE"], code)})
        bid = new_batch(statuses)
        self.reply(202, {"batch_id": bid, "success": success, "failed": failed})


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
API = f"http://127.0.0.1:{httpd.server_address[1]}/api"
ENV = {"CDON_API_URL": API, "CDON_MERCHANT_ID": MERCHANT, "CDON_API_TOKEN": TOKEN}


def book(n, **kw):
    b = {"id": f"C{n}", "title": f"Libro número {n}", "price_eur": 10, "brand": "Cuentos con Valores",
         "isbn": isbn(n), "condition": "NewCondition", "sold_out": False,
         "description": f"<p>Descripción del libro {n}.</p><ul><li>Rimas</li></ul>",
         "images": [f"https://img.example.com/{n}-a.jpg", f"https://img.example.com/{n}-b.jpg"]}
    b.update(kw)
    return b


SETTINGS = dict(MARKETS=["SE", "DK", "FI"], CATEGORY="1002", TEXT_LANGUAGES=["en-US"], SHIPPING_CONFIGURATIONS={},
                MANUFACTURER={"name": "", "address": {}}, DELIVERY_TYPE=None, SHIPPING_DAYS=(3, 7))


def run(*argv, env=None, books=None, **settings):
    for k, v in dict(SETTINGS, **settings).items():
        setattr(cp, k, v)
    for k in [k for k in os.environ if k.startswith("CDON_")]:
        del os.environ[k]
    os.environ.update(ENV if env is None else env)
    if books is not None:
        Path("books.json").write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
    sys.argv = ["cdon_push.py", *argv]
    buf, code = io.StringIO(), None
    with contextlib.redirect_stdout(buf):
        try:
            cp.main()
        except SystemExit as e:
            code = str(e.code) if e.code not in (None, 0) else None
    return buf.getvalue() + (code or ""), code


def state():
    return json.loads(Path("cdon_posted.json").read_text(encoding="utf-8"))


def calls(method, path_end):
    return [c for c in S.calls if c[0] == method and c[1].endswith(path_end)]


# --------------------------------------------------------------------------- tests

def tests():
    BOOKS = [book(1), book(2, price_eur=8.5), book(3, title="Abc"), book(4, isbn="123"),
             book(5, sold_out=True), book(6, images=[]), book(7, description="Corta")]

    out, code = run("push", "books.json", "--dry-run", env={}, books=BOOKS)
    arts = json.loads(Path("cdon_articles.json").read_text(encoding="utf-8"))
    ok(code is None and not S.calls and [x["sku"] for x in arts] == ["C1", "C2", "C7"],
       "dry run without credentials: articles built, nothing sent")
    ok("title shorter than 5" in out and "invalid ISBN" in out and "no image URL" in out and "sold out" in out,
       "skips explained: short title, bad ISBN, no image, sold out")
    a1 = arts[0]
    prices = {p["market"]: p["value"] for p in a1["price"]}
    ok(prices["SE"] == {"amount_including_vat": 110, "currency": "SEK", "vat_rate": 6}
       and prices["DK"] == {"amount_including_vat": 75, "currency": "DKK", "vat_rate": 25}
       and prices["FI"] == {"amount_including_vat": 10.0, "currency": "EUR", "vat_rate": 14},
       "prices per market: converted, SEK/DKK whole, EUR cents, books VAT rate")
    ok(a1["gtin"] == isbn(1) and a1["classifications"] == [{"name": "CONDITION", "value": "NEW"}]
       and a1["markets"] == ["SE", "DK", "FI"] and a1["category"] == "1002" and a1["shipped_from"] == "EU"
       and a1["main_image"].endswith("1-a.jpg") and a1["images"] == ["https://img.example.com/1-b.jpg"],
       "article: ISBN as gtin, condition NEW, markets, category, main image + extra images")
    ok("<" not in a1["description"][0]["value"] and a1["title"][0] == {"language": "en-US", "value": "Libro número 1"}
       and len(arts[2]["description"][0]["value"]) >= 10,
       "texts: HTML removed, language tags, short description padded to CDON's 10-character minimum")

    out, code = run("push", "books.json", "--dry-run", MARKETS=["SE", "ES"])
    ok(code and "MARKETS" in code, "settings: unknown market refused")

    out, code = run("check", env=dict(ENV, CDON_API_TOKEN="wrong"))
    ok(code and "401" in code, "wrong token: clear login error")

    S.throttle_once = True
    out, code = run("check")
    ok(code is None and "Login OK" in out and "Children's books" in out and "sc-se-1" in out
       and "MANUFACTURER" in out, "check: login (after a 429 retry), category, shipping configurations, GPSR warning")

    out, code = run("categories", "--find", "book")
    ok("1001" in out and "1002" in out and "2001" not in out and "2 categories" in out, "categories --find")

    S.calls = []
    out, code = run("push", "books.json", "--yes", "--limit", "1")
    ok(code is None and list(state()) == ["C1"] and state()["C1"]["status"] == "ok"
       and state()["C1"]["markets"] == {"SE": "ok", "DK": "ok", "FI": "ok"} and len(calls("POST", "statuses/batch")) == 2,
       "push --limit 1: created, waited while pending, then ok in all markets")

    many = [book(n) for n in range(1, 131)] + [book(200, id="PRE-EXISTING"),
                                               book(201, images=["https://img.example.com/bad-image.jpg"])]
    S.calls = []
    out, code = run("push", "books.json", "--yes", books=many)
    st = state()
    posts = calls("POST", "articles/bulk")
    ok(len(posts) == 2 and all(n <= 100 for _, _, n in posts) and sum(n for _, _, n in posts) == 131,
       "131 new articles sent in batches of at most 100")
    ok("C201: create rejected: main_image" in out and "C201" not in st, "immediate rejection shown, not recorded")
    ok(st["C77"]["status"] == "failed" and "invalid_classification" in " ".join(st["C77"]["errors"])
       and "! C77" in out, "asynchronous rejection: status failed with CDON's error code")
    ok(st["PRE-EXISTING"]["status"] == "ok" and "already exists at CDON" in out,
       "sku_already_exists: the existing article is taken over")
    ok(sum(1 for e in st.values() if e["status"] == "ok") == 130, "130 articles ok (C77 rejected by CDON)")

    S.calls = []
    out, code = run("push", "books.json", "--yes")
    ok([n for _, _, n in calls("POST", "articles/bulk")] == [2],
       "second run: only the two rejected articles are tried again")
    ok(not calls("PUT", "articles/bulk"), "second run without --resend: no updates")

    changed = [dict(b, price_eur=12) if b["id"] == "C2" else dict(b, stock=4) if b["id"] == "C3" else b for b in many]
    S.calls = []
    out, code = run("push", "books.json", "--yes", "--resend", books=changed)
    puts = calls("PUT", "articles/bulk")
    ok(len(puts) == 1 and puts[0][2] == 2 and S.articles["C2"]["price"][0]["value"]["amount_including_vat"] == 132
       and S.articles["C3"]["quantity"] == 4 and state()["C2"]["prices"]["SE"] == 132 and state()["C3"]["quantity"] == 4,
       "--resend: only the changed price and the changed stock are sent")
    S.calls = []
    run("push", "books.json", "--yes", "--resend")
    ok(not calls("PUT", "articles/bulk"), "--resend again: nothing changed, nothing sent")

    sold = [dict(b, sold_out=b["id"] in ("C4", "C5", "C6")) for b in changed]
    run("push", "books.json", "--yes", "--zero-sold-out", books=sold)
    ok(all(S.articles[s]["quantity"] == 0 for s in ("C4", "C5", "C6")), "--zero-sold-out: stock 0")
    run("push", "books.json", "--yes", "--pause-sold-out")
    ok(all(S.articles[s]["status"] == "paused" for s in ("C4", "C5", "C6"))
       and state()["C4"]["sale_status"] == "paused", "--pause-sold-out: update_article with status paused")
    back = [dict(b, sold_out=b["id"] in ("C5", "C6")) for b in changed]
    run("push", "books.json", "--yes", "--resend", books=back)
    ok(S.articles["C4"]["status"] == "for sale" and S.articles["C4"]["quantity"] == 1
       and state()["C4"]["sale_status"] == "for sale", "--resend: back in stock -> reactivated, stock restored")
    out, code = run("push", "books.json", "--yes", "--delete-sold-out")
    ok("C5" not in S.articles and "C5" not in state() and "C6" not in state() and "deleted" in out,
       "--delete-sold-out: delete_article, removed from the state")

    out, code = run("status")
    ok("C77: failed" in out and "invalid_classification" in out and "C1: ok" in out and "SE:ok" in out,
       "status: per book and market, with CDON's error codes")

    out, code = run("push", "books.json", "--yes", "--limit", "1", books=[book(300)],
                    MANUFACTURER={"name": "Cuentos con Valores", "address": {"street_address": "Calle 1",
                                  "city": "Cádiz", "postal_code": "11001", "country": "ES"}, "email": "x@y.es"})
    ok(S.articles["C300"]["manufacturer"]["name"] == "Cuentos con Valores"
       and S.articles["C300"]["manufacturer"]["address"]["country"] == "ES", "GPSR manufacturer sent when configured")

    out, code = run("push", "books.json", "--yes", books=[book(301)], MARKETS=["SE", "NO"], CATEGORY="99999")
    ok(state()["C301"]["status"] == "failed" and "invalid_category" in " ".join(state()["C301"]["errors"]),
       "unknown category: CDON's invalid_category is reported")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cp.time.sleep = lambda s: None           # no real waiting for the mock
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
