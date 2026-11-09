#!/usr/bin/env python3
"""
End-to-end test for otto_push.py, run against a LOCAL MOCK of the OTTO
Market API: the client credentials token (/v1/token with scopes), Products
API v5 (asynchronous update tasks with pending -> done, failed /succeeded
lists, categories, paginated brands, prices, active status, marketplace
status) and Availability API v1 (quantities and delivery information, with
207 partial failures), following the field rules of OTTO's OpenAPI spec.
Nothing is sent to OTTO, and your real otto_posted.json is not touched (each
test runs in a temporary folder).

    python tests/test_otto.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
CLIENT, SECRET = "otto-test-client", "otto-test-secret"
ENV = {"OTTO_API_URL": BASE, "OTTO_CLIENT_ID": CLIENT, "OTTO_CLIENT_SECRET": SECRET}
PROFILE = "2f236e76-543e-5a0b-ac79-53f5db132e44"
CATEGORIES = {"Buch": [{"name": "Autor", "type": "STRING", "relevance": "HIGH"},
                       {"name": "Sprache", "type": "STRING", "relevance": "HIGH", "allowedValues": ["Deutsch", "Spanisch"]}],
              "Kalender": []}
BRANDS = [{"id": "BR000001", "name": "Planeta", "usable": True},
          {"id": "BR000002", "name": "Cuentos con Valores", "usable": True},
          {"id": "BR000003", "name": "Editorial Bloqueada", "usable": False},
          {"id": "BR999999", "name": "Generic Books", "usable": True}]
GPSR = {"name": "Libros Ejemplo S.L.", "address": "Calle Mayor 1, 28001, Madrid, Spanien", "regionCode": "ES",
        "email": "info@libros.example"}


def ean_ok(e):
    return len(e) == 13 and e.isdigit() and \
        (10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(e[:12])) % 10) % 10 == int(e[12])


class S:
    @classmethod
    def reset(cls):
        cls.tokens, cls.token_calls, cls.scopes = set(), 0, []
        cls.products, cls.tasks = {}, {}         # sku -> variation; uuid -> task
        cls.quantities, cls.delivery = {}, {}
        cls.active = {}
        cls.calls = []                            # (method, path)
        cls.expire_next = cls.throttle_once = cls.slow = False
        cls.qty_reject = set()                    # skus the quantities API refuses (207)
        cls.task_polls = 0


S.reset()


def task_response(tid):
    t = S.tasks[tid]
    links = [{"rel": "self", "href": f"/v5/products/update-tasks/{tid}"}]
    if t["state"] == "done":
        links += [{"rel": r, "href": f"/v5/products/update-tasks/{tid}/{r}"} for r in ("failed", "succeeded", "unchanged")]
    return {"state": t["state"], "message": "", "total": t["total"], "progress": t["total"] if t["state"] == "done" else 0,
            "succeeded": len(t["succeeded"]), "failed": len(t["failed"]), "unchanged": len(t["unchanged"]),
            "pingAfter": "2026-11-09T10:00:00Z", "links": links}


def new_task(succeeded, failed, unchanged=()):
    tid = str(uuid.uuid4())
    S.tasks[tid] = {"state": "pending", "polls": 0, "total": len(succeeded) + len(failed) + len(unchanged),
                    "succeeded": list(succeeded), "failed": failed, "unchanged": list(unchanged)}
    return tid


def validate_variation(v):
    errs = []
    for k in ("productReference", "sku", "ean", "productDescription", "pricing", "mediaAssets", "compliance"):
        if k not in v:
            errs.append(("MISSING_FIELD", f"{k} is mandatory", f"$.{k}"))
    sku = v.get("sku", "")
    if len(sku) > 50 or sku != sku.strip():
        errs.append(("INVALID_SKU", "sku too long or with spaces", "$.sku"))
    if len(v.get("productReference", "")) > 50:
        errs.append(("INVALID_PRODUCT_REFERENCE", "max 50 characters", "$.productReference"))
    ean = v.get("ean", "")
    if not ean_ok(ean) or ean.startswith("2"):
        errs.append(("INVALID_EAN", "EAN invalid", "$.ean"))
    d = v.get("productDescription") or {}
    if d.get("category") not in CATEGORIES:
        errs.append(("UNKNOWN_CATEGORY", f"category {d.get('category')!r} unknown", "$.productDescription.category"))
    brand = next((b for b in BRANDS if b["id"] == d.get("brandId")), None)
    if not brand or not brand["usable"]:
        errs.append(("BRAND_NOT_USABLE", f"brand {d.get('brandId')!r} not usable", "$.productDescription.brandId"))
    if len(d.get("productLine", "")) > 70:
        errs.append(("INVALID_PRODUCT_LINE", "max 70 characters", "$.productDescription.productLine"))
    for b in d.get("bulletPoints") or []:
        if not 3 <= len(b) <= 180:
            errs.append(("INVALID_BULLET_POINT", "3-180 characters", "$.productDescription.bulletPoints"))
    allowed = {a["name"]: a for a in CATEGORIES.get(d.get("category"), [])}
    for at in d.get("attributes") or []:
        spec = allowed.get(at.get("name"))
        if spec and spec.get("allowedValues") and any(x not in spec["allowedValues"] for x in at.get("values") or []):
            errs.append(("INVALID_ATTRIBUTE_VALUE", f"{at['name']}: value not allowed", "$.productDescription.attributes"))
    imgs = [m for m in v.get("mediaAssets") or [] if m.get("type") == "IMAGE"]
    if not imgs:
        errs.append(("MEDIA_ASSET_MISSING", "at least one IMAGE", "$.mediaAssets"))
    if any("small-image" in m.get("location", "") for m in imgs):
        errs.append(("IMAGE_TOO_SMALL", "image smaller than 500x1000 px", "$.mediaAssets[0].location"))
    pr = v.get("pricing") or {}
    sp = pr.get("standardPrice") or {}
    if not isinstance(sp.get("amount"), (int, float)) or sp.get("amount") <= 0 or sp.get("currency") != "EUR":
        errs.append(("INVALID_PRICE", "standardPrice amount/currency", "$.pricing.standardPrice"))
    if abs(round(sp.get("amount", 0) * 100) - sp.get("amount", 0) * 100) > 1e-6:
        errs.append(("INVALID_PRICE", "max 2 decimals", "$.pricing.standardPrice.amount"))
    if pr.get("vat") not in ("FULL", "REDUCED", "FREE"):
        errs.append(("INVALID_VAT", "vat", "$.pricing.vat"))
    addrs = ((v.get("compliance") or {}).get("productSafety") or {}).get("addresses") or []
    if not any("DISTRIBUTOR" in a.get("roles", []) and a.get("name") and a.get("address")
               and (a.get("email") or a.get("url")) for a in addrs):
        errs.append(("PRODUCT_SAFETY_MISSING", "a distributor address is mandatory", "$.compliance.productSafety"))
    return errs


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

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        S.calls.append((method, u.path))
        if u.path == "/v1/token":
            f = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            S.token_calls += 1
            if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                return self.send({"error": "invalid_request"}, 400)
            if (f.get("grant_type"), f.get("client_id"), f.get("client_secret")) != ("client_credentials", CLIENT, SECRET):
                return self.send({"error": "invalid_client"}, 401)
            S.scopes.append(f.get("scope", ""))
            tok = uuid.uuid4().hex
            S.tokens.add(tok)
            return self.send({"access_token": tok, "token_type": "Bearer", "expires_in": 1800,
                              "scope": f.get("scope", "")})
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in S.tokens:
            return self.send({"message": "Unauthorized"}, 401)
        if S.expire_next:
            S.expire_next = False
            S.tokens.discard(auth[7:])
            return self.send({"message": "Unauthorized"}, 401)
        if S.throttle_once and u.path.startswith("/v5/products") and method == "POST":
            S.throttle_once = False
            return self.send({"message": "Too Many Requests"}, 429)
        body = json.loads(raw.decode("utf-8")) if raw else None
        p = u.path

        if p == "/v1/shipping-profiles":
            return self.send({"results": [{"shippingProfileId": PROFILE, "shippingProfileName": "Bücher DHL",
                                           "workingDays": ["MONDAY"], "orderCutoff": "14:00", "deliveryType": "PARCEL",
                                           "defaultProcessingTime": 2, "transportTime": 2}]})
        if p == "/v5/products/categories":
            if q.get("category"):
                if q["category"] not in CATEGORIES:
                    return self.send({"errors": [{"title": "not found"}]}, 404)
                cats = [q["category"]]
            else:
                cats = list(CATEGORIES)
            return self.send({"categoryGroups": [{"categoryGroup": "Bücher" if c == "Buch" else c, "categories": [c],
                                                  "title": "[Titel]", "attributes": CATEGORIES[c]} for c in cats],
                              "links": [{"rel": "self", "href": p}]})
        if p == "/v5/products/brands":
            page = int(q.get("page", 0))
            chunk = BRANDS[page * 2: page * 2 + 2]
            links = [{"rel": "self", "href": f"{p}?page={page}"}]
            if page * 2 + 2 < len(BRANDS):
                links.append({"rel": "next", "href": f"{p}?page={page + 1}&limit=2"})
            return self.send({"brands": chunk, "links": links})
        if p == "/v5/products" and method == "POST":
            if not isinstance(body, list) or len(body) > 500:
                return self.send({"errors": [{"title": "max 500 variations"}]}, 400)
            ok, failed, unchanged = [], [], []
            for v in body:
                errs = validate_variation(v)
                if errs:
                    failed.append({"variation": v.get("sku"), "errors": [
                        {"code": c, "title": t, "path": path, "jsonPath": path, "logref": "x"} for c, t, path in errs]})
                elif S.products.get(v["sku"]) == v:
                    unchanged.append(v["sku"])
                else:
                    S.products[v["sku"]] = v
                    ok.append(v["sku"])
            return self.send(task_response(new_task(ok, failed, unchanged)), 202)
        if p.startswith("/v5/products/update-tasks/"):
            parts = p.split("/")
            tid = parts[4]
            if tid not in S.tasks:
                return self.send({"errors": [{"title": "unknown task"}]}, 404)
            t = S.tasks[tid]
            if len(parts) == 5:
                S.task_polls += 1
                t["polls"] += 1
                if t["polls"] >= 1 and not S.slow:
                    t["state"] = "done"
                return self.send(task_response(tid))
            part = parts[5]
            if part == "failed":
                return self.send({"results": t["failed"]})
            return self.send({"results": [{"variation": s} for s in t[part]]})
        if p == "/v5/products/prices" and method == "POST":
            ok, failed = [], []
            for x in body:
                if x.get("sku") not in S.products:
                    failed.append({"variation": x.get("sku"), "errors": [{"code": "UNKNOWN_SKU", "title": "unknown"}]})
                elif x.get("standardPrice", {}).get("currency") != "EUR":
                    failed.append({"variation": x.get("sku"), "errors": [{"code": "INVALID_PRICE", "title": "currency"}]})
                else:
                    S.products[x["sku"]]["pricing"]["standardPrice"] = x["standardPrice"]
                    ok.append(x["sku"])
            return self.send(task_response(new_task(ok, failed)), 202)
        if p == "/v5/products/active-status" and method == "POST":
            ok = []
            for x in (body or {}).get("status", []):
                S.active[x["sku"]] = x["active"]
                ok.append(x["sku"])
            return self.send(task_response(new_task(ok, [])), 202)
        if p == "/v5/products/marketplace-status":
            out = []
            for sku, v in S.products.items():
                st = "ONLINE" if S.quantities.get(sku, 0) > 0 and S.active.get(sku, True) else "INACTIVE"
                errors = []
                if sku == "OT-PENDING":
                    st = "REJECTED"
                    errors = [{"code": "800020 - PRODUCTDATASHEET_MISSING", "title": "Das Produktdatenblatt fehlt."}]
                out.append({"sku": sku, "status": st, "errors": errors, "information": [],
                            "links": [{"rel": "shop", "href": f"https://www.otto.de/p/{sku.lower()}"}]})
            return self.send({"marketPlaceStatus": out, "links": [{"rel": "self", "href": p}]})
        if p in ("/v1/availability/quantities", "/v1/availability/product-delivery-information") and method == "POST":
            if not isinstance(body, list) or len(body) > 200:
                return self.send({"errors": [{"code": 413, "title": "too many", "detail": "", "logref": "", "path": p}]}, 413)
            results, errors = [], []
            for x in body:
                sku = x.get("sku")
                bad = None
                if sku not in S.products:
                    bad = f"unknown sku {sku}"
                elif p.endswith("quantities"):
                    if not isinstance(x.get("quantity"), int) or x["quantity"] < 0 or sku in S.qty_reject:
                        bad = f"quantity rejected for sku {sku}"
                else:
                    pt = str(x.get("processingTime"))
                    if x.get("shippingProfileId") != PROFILE or not (pt == "DEFAULT" or pt.isdigit() and 1 <= int(pt) <= 99):
                        bad = f"delivery information rejected for sku {sku}"
                if bad:
                    errors.append({"code": 422, "title": "Unprocessable", "detail": bad, "logref": "x", "path": p})
                else:
                    (S.quantities if p.endswith("quantities") else S.delivery)[sku] = \
                        x.get("quantity") if p.endswith("quantities") else x
                    results.append(dict(x, lastModified="2026-11-09T10:00:00Z", links=[]))
            if errors:
                return self.send({"results": results, "errors": errors}, 207)
            return self.send({}, 200) if p.endswith("quantities") else self.send({"message": "OK"}, 202)
        return self.send({"errors": [{"title": f"no route {method} {p}"}]}, 404)


# ------------------------------------------------------------------ the tests

RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None, settings=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    sets = settings if settings is not None else {"CATEGORY": "Buch", "BRAND_ID": "BR999999", "GPSR_DISTRIBUTOR": GPSR,
                                                  "ATTRIBUTES": {"Sprache": ["Spanisch"]},
                                                  "FIELD_ATTRIBUTES": {"author": "Autor"}}
    code = "import otto_push as o\n" + "".join(f"o.{k} = {v!r}\n" for k, v in sets.items()) + \
        f"import sys\nsys.argv = {['x'] + list(args)!r}\no.main()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


IMG = f"{BASE}/img/cover.jpg"
BOOKS = [
    {"id": "OT-PLANETA", "title": "Libro de Planeta", "price_eur": 19.9, "isbn": "9788408000006", "publisher": "Planeta",
     "author": "Ana Autora", "condition": "NewCondition", "images": [IMG], "stock": 2},
    {"id": "OT-GENERIC", "title": "Libro sin marca OTTO", "price_eur": 8.075, "isbn": "84-206-5867-X",
     "publisher": "Editorial Pequeña", "condition": "NewCondition", "images": [IMG],
     "description": "Primera línea <con> & símbolos\nSegunda línea"},
    {"id": "OT-BLOCKED", "title": "Marca no usable", "price_eur": 10, "isbn": "9780306406157",
     "publisher": "Editorial Bloqueada", "condition": "NewCondition", "images": [IMG]},
    {"id": "OT-SMALLIMG", "title": "Imagen pequeña", "price_eur": 12, "isbn": "9781861972712",
     "publisher": "Planeta", "condition": "NewCondition", "images": [f"{BASE}/img/small-image.jpg"]},
    {"id": "OT-PENDING", "title": "Rechazado después", "price_eur": 7, "isbn": "9780000000002",
     "publisher": "Planeta", "condition": "NewCondition", "images": [IMG]},
    {"id": "OT-USED", "title": "Usado", "price_eur": 5, "isbn": "9788410413290", "condition": "UsedCondition", "images": [IMG]},
    {"id": "OT-NOIMG", "title": "Sin imagen", "price_eur": 5, "isbn": "9788420658674", "condition": "NewCondition"},
    {"id": "OT-EAN2", "title": "EAN interno", "price_eur": 5, "ean": "2000000000008", "condition": "NewCondition", "images": [IMG]},
]


def write(tmp, books):
    f = Path(tmp) / "books.json"
    f.write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
    return str(f)


def test_otto():
    fast = ["--poll", "0"]
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        pf = write(tmp, BOOKS)
        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = json.loads((Path(tmp) / "otto_products.json").read_text(encoding="utf-8")) if (Path(tmp) / "otto_products.json").exists() else []
        check("dry run without credentials: products file, no API call",
              rc == 0 and len(prev) == 5 and not S.calls, out[-300:])
        check("used, no image and EAN starting with 2 skipped locally",
              "OT-USED" not in str(prev) and "Usado" in out and "no image URL" in out and "starting with 2" in out, out[-500:])
        g = next((v for v in prev if v["sku"] == "OT-GENERIC"), {})
        check("ISBN-10 -> EAN-13, price half-up, HTML-escaped description, attributes",
              g.get("ean") == "9788420658674" and g["pricing"]["standardPrice"]["amount"] == 8.08
              and "&lt;con&gt; &amp;" in g["productDescription"]["description"] and "<br>" in g["productDescription"]["description"]
              and {"name": "Sprache", "values": ["Spanisch"]} in g["productDescription"]["attributes"], g)

        rc, out = run(["check"], tmp, dict(ENV, OTTO_CLIENT_SECRET="wrong"))
        check("wrong client secret: clear login message", rc != 0 and "OTTO login failed (401)" in out, out[-200:])

        rc, out = run(["check"], tmp, ENV)
        check("check: token with both scopes, shipping profile, category, GPSR",
              rc == 0 and PROFILE in out and "category 'Buch': found" in out and "GPSR distributor: OK" in out
              and S.scopes and set(S.scopes[-1].split()) == {"products", "availability"}, out[-400:])

        rc, out = run(["categories", "--find", "buch"], tmp, ENV)
        check("categories --find", rc == 0 and "Bücher: Buch" in out and "Kalender" not in out, out[-200:])
        rc, out = run(["attributes", "Buch"], tmp, ENV)
        check("attributes: names, relevance, allowed values", rc == 0 and "Sprache [STRING]" in out
              and "Deutsch, Spanisch" in out, out[-300:])
        rc, out = run(["brands", "--find", "editorial"], tmp, ENV)
        check("brands: all pages read, unusable brands hidden",
              rc == 0 and "0 usable brand(s)" in out and (Path(tmp) / "otto_brands.json").exists(), out[-200:])

        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV,
                      {"CATEGORY": "Buch", "BRAND_ID": "BR999999", "GPSR_DISTRIBUTOR": {"name": "", "address": "",
                                                                                          "regionCode": "", "email": ""}})
        check("missing GPSR distributor: real push refused", rc != 0 and "GPSR_DISTRIBUTOR is incomplete" in out
              and not S.products, out[-200:])

        S.throttle_once = True
        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "otto_posted.json").read_text(encoding="utf-8"))
        check("429 on upload: retried", "retrying" in out and S.products, out[-300:])
        check("update task polled until done", S.task_polls >= 1 and "product data:" in out, out[-300:])
        check("publisher matched to its usable OTTO brand, others use BRAND_ID",
              S.products.get("OT-PLANETA", {}).get("productDescription", {}).get("brandId") == "BR000001"
              and S.products.get("OT-GENERIC", {}).get("productDescription", {}).get("brandId") == "BR999999"
              and S.products.get("OT-BLOCKED", {}).get("productDescription", {}).get("brandId") == "BR999999", out[-300:])
        check("OTTO rejection shown with code and path, not recorded",
              "OT-SMALLIMG: IMAGE_TOO_SMALL" in out and "mediaAssets" in out and "OT-SMALLIMG" not in state, out[-400:])
        check("accepted products get stock and delivery information (profile, DEFAULT)",
              S.quantities.get("OT-PLANETA") == 2 and S.quantities.get("OT-GENERIC") == 1
              and S.delivery.get("OT-PLANETA", {}).get("processingTime") == "DEFAULT"
              and S.delivery.get("OT-PLANETA", {}).get("shippingProfileId") == PROFILE, f"{S.quantities} {S.delivery}")
        check("state: one entry per accepted product", sorted(state) == ["OT-BLOCKED", "OT-GENERIC", "OT-PENDING", "OT-PLANETA"],
              sorted(state))

        n = len([c for c in S.calls if c == ("POST", "/v5/products")])
        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        check("second run: pushed ones skipped, only the rejected one is tried again",
              out.count("already pushed") == 4 and "1 new product(s)" in out
              and len([c for c in S.calls if c == ("POST", "/v5/products")]) == n + 1, out[-300:])

        books = json.loads(json.dumps(BOOKS))
        books[0]["price_eur"] = 21.5
        books[1]["stock"] = 4
        S.qty_reject = {"OT-GENERIC"}
        rc, out = run(["push", write(tmp, books), "--yes", "--resend"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "otto_posted.json").read_text(encoding="utf-8"))
        check("--resend: changed price via prices API only",
              S.products["OT-PLANETA"]["pricing"]["standardPrice"]["amount"] == 21.5 and state["OT-PLANETA"]["price"] == 21.5
              and ("POST", "/v5/products/prices") in S.calls, out[-300:])
        check("207 partial stock failure: reported, not recorded, retried next time",
              "quantity rejected for sku OT-GENERIC" in out and state["OT-GENERIC"]["stock"] == 1
              and "OT-GENERIC: stock 4" not in out, out[-300:])
        S.qty_reject = set()
        rc, out = run(["push", write(tmp, books), "--yes", "--resend"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "otto_posted.json").read_text(encoding="utf-8"))
        check("stock update goes through on the next --resend", S.quantities["OT-GENERIC"] == 4
              and state["OT-GENERIC"]["stock"] == 4 and "OT-PLANETA: unchanged" in out, out[-300:])

        books[0]["sold_out"] = True
        books[1]["sold_out"] = True
        rc, out = run(["push", write(tmp, books), "--yes", "--zero-sold-out", "--offline-sold-out"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "otto_posted.json").read_text(encoding="utf-8"))
        check("--zero-sold-out: stock 0", S.quantities["OT-PLANETA"] == 0 and state["OT-PLANETA"]["stock"] == 0, out[-300:])
        check("--offline-sold-out: active status false", S.active.get("OT-GENERIC") is False
              and state["OT-GENERIC"]["active"] is False and "taken offline" in out, out[-300:])

        S.expire_next = True
        calls = S.token_calls
        rc, out = run(["status"], tmp, ENV)
        check("token rejected mid-run: new token, request repeated", rc == 0 and S.token_calls >= calls + 2, out[-200:])
        check("status: ONLINE / REJECTED with OTTO's error and shop link",
              "OT-BLOCKED: ONLINE" in out and "OT-PENDING: REJECTED" in out and "PRODUCTDATASHEET_MISSING" in out
              and "(offline)" in out and "https://www.otto.de/p/ot-blocked" in out, out[-500:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        S.slow = True
        rc, out = run(["push", write(tmp, BOOKS[:1]), "--yes", "--wait", "0", "--poll", "0"], tmp, ENV)
        check("slow processing: nothing recorded, safe to run again", "Still pending" in out
              and not (Path(tmp) / "otto_posted.json").exists(), out[-300:])
        S.slow = False
        rc, out = run(["push", write(tmp, BOOKS), "--yes", "--limit", "1"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "otto_posted.json").read_text(encoding="utf-8"))
        check("--limit 1: one new product", list(state) == ["OT-PLANETA"], list(state))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_otto()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
