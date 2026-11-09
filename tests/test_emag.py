#!/usr/bin/env python3
"""
End-to-end test for emag_push.py, run against a LOCAL MOCK of the eMAG
Marketplace API (documentation v4.5.1): Basic authentication, the {"data": ...}
POST envelope, isError / messages responses, the 50-entities bulk limit, the
100-EAN limit of documentation/find_by_eans, vat/read, handling_time/read,
product_offer/save (attach by part_number_key, min/max price window, one
offer per PNK), the light offer/save, product_offer/read with pagination,
and rate limiting (429). Nothing is sent to eMAG, and your real
emag_posted.json is not touched (each test runs in a temporary folder).

    python tests/test_emag.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/api-3"
USER, PASSWORD = "emag-test-user", "emag-test-password"
ENV = {"EMAG_API_URL": BASE, "EMAG_USERNAME": USER, "EMAG_PASSWORD": PASSWORD}
VAT = [{"vat_id": 1, "vat_rate": 21}, {"vat_id": 4, "vat_rate": 11}, {"vat_id": 6, "vat_rate": 0}]
HANDLING = [{"value": v} for v in (0, 1, 2, 3, 5)]
CATALOG = {   # EAN -> product known to eMAG
    "9788409591923": {"part_number_key": "D5DD9BBBM", "product_name": "Dragona", "category_name": "Carti pentru copii",
                      "allow_to_add_offer": True, "vendor_has_offer": False},
    "9788420658674": {"part_number_key": "DQ2K1BBBM", "product_name": "Libro viejo", "category_name": "Carti",
                      "allow_to_add_offer": True, "vendor_has_offer": False},
    "9780306406157": {"part_number_key": "DXXLOCKED", "product_name": "Blocked", "category_name": "Manuale",
                      "allow_to_add_offer": False, "vendor_has_offer": False},
    "9781861972712": {"part_number_key": "DMANUAL01", "product_name": "Already sold", "category_name": "Carti",
                      "allow_to_add_offer": True, "vendor_has_offer": True},
    "9780000000002": {"part_number_key": "DBADPRICE", "product_name": "Price check", "category_name": "Carti",
                      "allow_to_add_offer": True, "vendor_has_offer": False},
}


class S:
    @classmethod
    def reset(cls):
        cls.offers = {}                 # id -> offer
        cls.pnk_owner = {}              # pnk -> id
        cls.calls = []
        cls.throttle_once = False
        cls.ean_requests = []
        cls.bad_auth = 0


S.reset()


def dec_ok(x, places=4):
    return isinstance(x, (int, float)) and x > 0 and abs(round(x * 10 ** places) - x * 10 ** places) < 1e-6


def validate_offer(o, light=False):
    errs = []
    oid = o.get("id")
    if not isinstance(oid, int) or not 1 <= oid <= 16777215:
        return [f"id {oid!r}: integer between 1 and 16777215 required"]
    existing = S.offers.get(oid)
    if light:
        if not existing:
            return [f"Offer {oid} not found; offer/save cannot create offers"]
    else:
        pnk = o.get("part_number_key")
        if "ean" in o and pnk:
            errs.append("part_number_key and ean are mutually exclusive")
        if pnk not in {c["part_number_key"] for c in CATALOG.values()}:
            errs.append(f"Invalid part_number_key {pnk}")
        elif S.pnk_owner.get(pnk, oid) != oid:
            errs.append(f"You already have an offer on part_number_key {pnk}")
        if not 1 <= len(o.get("name", "")) <= 255:
            errs.append("name: 1-255 characters")
        for k in ("status", "sale_price", "vat_id", "stock"):
            if k not in o:
                errs.append(f"{k} is required")
        if not existing:
            for k in ("min_sale_price", "max_sale_price"):
                if k not in o:
                    errs.append(f"{k} is required on first save")
        if pnk == "DBADPRICE":
            errs.append("Price check failed: sale_price too far from the market price")
    if "status" in o and o["status"] not in (0, 1, 2):
        errs.append("status: 0, 1 or 2")
    for k in ("sale_price", "min_sale_price", "max_sale_price"):
        if k in o and not dec_ok(o[k]):
            errs.append(f"{k}: decimal > 0, up to four decimals")
    lo = o.get("min_sale_price", (existing or {}).get("min_sale_price"))
    hi = o.get("max_sale_price", (existing or {}).get("max_sale_price"))
    if lo is not None and hi is not None:
        if hi <= lo:
            errs.append("max_sale_price must be greater than min_sale_price")
        sp = o.get("sale_price", (existing or {}).get("sale_price"))
        if sp is not None and not lo <= sp <= hi:
            errs.append(f"sale_price {sp} outside min/max {lo}-{hi}")
    if "vat_id" in o and o["vat_id"] not in {v["vat_id"] for v in VAT}:
        errs.append(f"Invalid vat_id {o['vat_id']}")
    if "currency_type" in o and o["currency_type"] not in ("EUR", "PLN"):
        errs.append("currency_type: EUR or PLN")
    for s in o.get("stock", []):
        if s.get("warehouse_id") != 1 or not isinstance(s.get("value"), int) or not 0 <= s["value"] <= 65535:
            errs.append("stock: warehouse_id 1, value 0-65535")
    for h in o.get("handling_time", []):
        if h.get("value") not in [x["value"] for x in HANDLING]:
            errs.append(f"Invalid handling_time {h.get('value')}")
    return [f"Offer {oid}: {e}" for e in errs]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ok(self, results=None, msgs=None):
        return self.send({"isError": False, "messages": msgs or [], "results": results if results is not None else []})

    def err(self, msgs):
        return self.send({"isError": True, "messages": msgs, "results": []})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        u = urlparse(self.path)
        path = u.path.replace("/api-3/", "", 1)
        S.calls.append((method, path))
        want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            S.bad_auth += 1
            return self.send({"message": "Unauthorized"}, 401)
        if S.throttle_once:
            S.throttle_once = False
            return self.send({"message": "API rate limit exceeded"}, 429)
        if method == "GET":
            if path != "documentation/find_by_eans":
                return self.send({"message": "not found"}, 404)
            eans = parse_qs(u.query).get("eans[]", [])
            S.ean_requests.append(eans)
            if len(eans) > 100:
                return self.err(["Only the first 100 EANs were processed"])
            return self.ok([dict(CATALOG[e], eans=e) for e in eans if e in CATALOG])
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else None
        except ValueError:
            return self.err(["Invalid JSON"])
        if not isinstance(body, dict) or "data" not in body:
            return self.err(["The 'data' key is mandatory"])
        data = body["data"]
        if path == "vat/read":
            return self.ok(VAT)
        if path == "handling_time/read":
            return self.ok(HANDLING)
        if path in ("product_offer/save", "offer/save"):
            if not isinstance(data, list):
                return self.err(["data must be a list"])
            if len(data) > 50:
                return self.err(["Maximum 50 entities per request"])
            errs = []
            for o in data:
                e = validate_offer(o, light=path == "offer/save")
                if e:
                    errs += e
                    continue
                cur = S.offers.setdefault(o["id"], {"id": o["id"]})
                cur.update(o)
                if "part_number_key" in o:
                    S.pnk_owner[o["part_number_key"]] = o["id"]
            return self.err(errs) if errs else self.ok()
        if path == "product_offer/read":
            page, per = int(data.get("currentPage", 1)), int(data.get("itemsPerPage", 100))
            if per > 100:
                return self.err(["itemsPerPage max 100"])
            rows = []
            for o in sorted(S.offers.values(), key=lambda x: x["id"]):
                stock = sum(s["value"] for s in o.get("stock", []))
                rows.append({"id": o["id"], "part_number_key": o.get("part_number_key"), "status": o.get("status"),
                             "sale_price": o.get("sale_price"), "currency": o.get("currency_type", "RON"),
                             "general_stock": stock, "number_of_offers": 3, "buy_button_rank": 2,
                             "best_offer_sale_price": 6.5,
                             "offer_validation_status": {"value": 1, "description": "Valid"},
                             "validation_status": [{"value": 9, "description": "Approved documentation", "errors": None}]})
            return self.ok(rows[(page - 1) * per: page * per])
        return self.err([f"Unknown resource {path}"])


# ------------------------------------------------------------------ the tests

RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("EMAG_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = ("import emag_push as e\ne.MIN_INTERVAL = 0\n"
            f"import sys\nsys.argv = {['x'] + list(args)!r}\ne.main()\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


BOOKS = [
    {"id": "EM-NEW", "title": "Dragona", "price_eur": 8.5, "isbn": "9788409591923", "condition": "NewCondition", "stock": 3},
    {"id": "EM-ISBN10", "title": "Libro viejo", "price_eur": 20, "isbn": "84-206-5867-X", "condition": "NewCondition"},
    {"id": "EM-LOCKED", "title": "Categoría sin acceso", "price_eur": 10, "isbn": "9780306406157", "condition": "NewCondition"},
    {"id": "EM-MANUAL", "title": "Oferta ya existente", "price_eur": 10, "isbn": "9781861972712", "condition": "NewCondition"},
    {"id": "EM-BADPRICE", "title": "Precio rechazado", "price_eur": 10, "isbn": "9780000000002", "condition": "NewCondition"},
    {"id": "EM-UNKNOWN", "title": "No en eMAG", "price_eur": 10, "isbn": "9788410413290", "condition": "NewCondition"},
    {"id": "EM-USED", "title": "Usado", "price_eur": 5, "isbn": "9788408000006", "condition": "UsedCondition"},
    {"id": "EM-NOISBN", "title": "Sin ISBN", "price_eur": 5, "condition": "NewCondition"},
]


def write(tmp, books):
    f = Path(tmp) / "books.json"
    f.write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
    return str(f)


def load(tmp, name):
    return json.loads((Path(tmp) / name).read_text(encoding="utf-8"))


def test_emag():
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        pf = write(tmp, BOOKS)
        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = load(tmp, "emag_offers.json") if (Path(tmp) / "emag_offers.json").exists() else []
        check("dry run without credentials: offers file, no API call, no ids reserved",
              rc == 0 and len(prev) == 6 and not S.calls and not (Path(tmp) / "emag_ids.json").exists(), out[-300:])
        check("used and no-ISBN books skipped locally", "Usado" in out and "no ISBN/EAN" in out, out[-300:])
        n = next((o for o in prev if o["name"] == "Dragona"), {})
        check("price without 11% VAT, in EUR, with min/max window",
              n.get("sale_price") == 7.66 and n.get("min_sale_price") == 6.13 and n.get("max_sale_price") == 11.49
              and n.get("currency_type") == "EUR", n)

        rc, out = run(["check"], tmp, dict(ENV, EMAG_PASSWORD="wrong"))
        check("wrong password: login message mentions API rights and IP whitelist",
              rc != 0 and "refused the login (401)" in out and "whitelisted" in out, out[-300:])
        rc, out = run(["check"], tmp, ENV)
        check("check: VAT rates, books VAT id, handling times", rc == 0 and "books VAT 11%: vat_id 4" in out
              and "handling times: [0, 1, 2, 3, 5]" in out, out[-300:])
        rc, out = run(["lookup", "84-206-5867-X"], tmp, ENV)
        check("lookup: ISBN-10 converted, PNK and access shown", rc == 0 and "9788420658674" in out
              and "DQ2K1BBBM" in out and "add an offer: True" in out, out[-200:])

        S.throttle_once = True
        S.ean_requests = []
        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        state, ids = load(tmp, "emag_posted.json"), load(tmp, "emag_ids.json")
        check("429: retried", "retrying" in out and rc == 0, out[-300:])
        check("one find_by_eans call for all ISBNs (GET eans[])", len(S.ean_requests) == 1
              and len(S.ean_requests[0]) == 6 and "9788420658674" in S.ean_requests[0], S.ean_requests)
        check("unknown EAN, no category access and existing own offer skipped with reasons",
              "EM-UNKNOWN" not in state and "not in the eMAG catalog" in out and "no access to category 'Manuale'" in out
              and "already have an offer on DMANUAL01" in out, out[-600:])
        check("offers attached by part_number_key with vat_id, stock, handling time",
              S.offers.get(ids.get("EM-NEW"), {}).get("part_number_key") == "D5DD9BBBM"
              and S.offers[ids["EM-NEW"]]["vat_id"] == 4 and S.offers[ids["EM-NEW"]]["stock"][0]["value"] == 3
              and S.offers[ids["EM-NEW"]]["handling_time"][0]["value"] == 2, S.offers)
        check("batch error re-sent one by one: the bad offer named, the others recorded",
              "re-sending one by one" in out and "EM-BADPRICE: Offer" in out and "Price check failed" in out
              and sorted(state) == ["EM-ISBN10", "EM-NEW"], out[-500:])
        check("numeric offer ids kept per SKU", isinstance(ids.get("EM-NEW"), int) and ids["EM-NEW"] != ids["EM-ISBN10"]
              and state["EM-NEW"]["id"] == ids["EM-NEW"], ids)

        saves = len([c for c in S.calls if c[1] == "product_offer/save"])
        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        ids2 = load(tmp, "emag_ids.json")
        check("second run: pushed offers skipped; the rejected one retried with the SAME id",
              out.count("already pushed") == 2 and ids2.get("EM-BADPRICE") == ids.get("EM-BADPRICE")
              and len([c for c in S.calls if c[1] == "product_offer/save"]) > saves, out[-300:])

        books = json.loads(json.dumps(BOOKS))
        books[0]["price_eur"] = 9.0          # within the min/max window
        books[1]["price_eur"] = 40           # outside: new window needed
        books[1]["stock"] = 5
        rc, out = run(["push", write(tmp, books), "--yes", "--resend"], tmp, ENV)
        state = load(tmp, "emag_posted.json")
        light = [c for c in S.calls if c[1] == "offer/save"]
        check("--resend uses the light offer/save", light and rc == 0, S.calls[-5:])
        check("price inside the window: only sale_price sent", state["EM-NEW"]["price"] == 8.11
              and S.offers[ids["EM-NEW"]]["sale_price"] == 8.11, state["EM-NEW"])
        check("price outside the window: new min/max sent with it, stock updated",
              state["EM-ISBN10"]["price"] == 36.04 and state["EM-ISBN10"]["max"] == 54.05
              and S.offers[ids["EM-ISBN10"]]["stock"][0]["value"] == 5, state["EM-ISBN10"])
        rc, out = run(["push", write(tmp, books), "--yes", "--resend"], tmp, ENV)
        check("--resend without changes: unchanged, nothing sent", out.count("unchanged") == 2
              and len([c for c in S.calls if c[1] == "offer/save"]) == len(light), out[-200:])

        books[0]["sold_out"] = True
        books[1]["sold_out"] = True
        rc, out = run(["push", write(tmp, books), "--yes", "--zero-sold-out", "--offline-sold-out"], tmp, ENV)
        state = load(tmp, "emag_posted.json")
        check("sold out: stock 0 and status inactive", S.offers[ids["EM-NEW"]]["stock"][0]["value"] == 0
              and S.offers[ids["EM-NEW"]]["status"] == 0 and state["EM-ISBN10"]["status"] == 0, out[-300:])

        books[0]["sold_out"] = False
        rc, out = run(["push", write(tmp, books), "--yes", "--resend"], tmp, ENV)
        state = load(tmp, "emag_posted.json")
        check("back in stock: --resend reactivates and restocks", S.offers[ids["EM-NEW"]]["status"] == 1
              and state["EM-NEW"]["stock"] == 3 and "active" in out, out[-300:])

        rc, out = run(["status"], tmp, ENV)
        check("status: for sale, price validation, documentation, buy button rank",
              rc == 0 and "EM-NEW: FOR SALE" in out and "EM-ISBN10: not for sale" in out
              and "Approved documentation" in out and "buy button rank 2 of 3" in out, out[-400:])
        check("every POST used the {\"data\": ...} envelope (mock rejects others)",
              "'data' key is mandatory" not in out and S.bad_auth == 1, S.bad_auth)

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        many = [{"id": f"EM-{i}", "title": f"Libro {i}", "price_eur": 10, "isbn": "9788409591923",
                 "condition": "NewCondition"} for i in range(3)]
        rc, out = run(["push", write(tmp, BOOKS), "--yes", "--limit", "1"], tmp, ENV)
        check("--limit 1: one new offer", sorted(load(tmp, "emag_posted.json")) == ["EM-NEW"], out[-200:])
        rc, out = run(["push", write(tmp, many), "--yes"], tmp, ENV)
        check("one offer per eMAG product: a second SKU on the same PNK is refused",
              "EM-0" in out and "already have an offer" in out, out[-400:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_emag()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
