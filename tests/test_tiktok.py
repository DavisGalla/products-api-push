#!/usr/bin/env python3
"""
End-to-end test for tiktok_push.py, run against a LOCAL MOCK of the TikTok
Shop Partner API. The mock recomputes the HMAC-SHA256 signature of every
request as TikTok documents it (path + sorted query + JSON body, body left
out for multipart uploads), checks the access token header and the shop
cipher, and implements the calls the script uses: token create/refresh,
authorized shops, warehouses, GPSR searches, categories, category
recommendation, attributes, rules, image upload (multipart), create product
(with the documented validation rules), inventory/price updates,
activate/deactivate and product search. Nothing is sent to TikTok, and your
real tiktok_posted.json / tiktok_token.json are not touched (each test runs
in a temporary folder).

    python tests/test_tiktok.py
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
APP_KEY = "6abc123def"
SECRET = "test-tiktok-secret"
CIPHER = "GCP_XF90igAAAABh00qsWgtvOiGFNqyubMt3"
ENV = {"TIKTOK_API_URL": BASE, "TIKTOK_AUTH_URL": BASE, "TIKTOK_APP_KEY": APP_KEY, "TIKTOK_APP_SECRET": SECRET}
ALLOW_ALL = {"NewCondition", "UsedCondition", "RefurbishedCondition"}
BOOKS_CAT = "601450"
LANG_ATTR = {"id": "100107", "values": [{"id": "1000201"}]}
ATTRIBUTES = [
    {"id": "100107", "name": "Idioma", "type": "PRODUCT_PROPERTY", "is_requried": True, "is_customizable": False,
     "values": [{"id": "1000201", "name": "Español"}, {"id": "1000202", "name": "Inglés"}]},
    {"id": "100990", "name": "ISBN", "type": "PRODUCT_PROPERTY", "is_requried": True, "is_customizable": True, "values": []},
    {"id": "100200", "name": "Autor", "type": "PRODUCT_PROPERTY", "is_requried": False, "is_customizable": True, "values": []},
    {"id": "100000", "name": "Formato", "type": "SALES_PROPERTY", "is_requried": False, "values": []},
]


def expected_sign(path, query, body):
    params = "".join(f"{k}{v}" for k, v in sorted(query.items()) if k not in ("sign", "access_token"))
    text = SECRET + path + params + body + SECRET
    return hmac.new(SECRET.encode(), text.encode(), hashlib.sha256).hexdigest()


class S:
    @classmethod
    def reset(cls):
        cls.tokens = {}                 # access token -> expiry (epoch seconds)
        cls.refresh = {"r-0"}
        cls.products = {}               # product_id -> dict
        cls.creates = cls.uploads = cls.refreshes = cls.bad_sigs = cls.price_calls = cls.inv_calls = 0
        cls.throttle_once = False
        cls.rps = [{"id": "rp-555", "locales": [{"locale": "es-ES", "name": "EU Rep SL"}]}]
        cls.mans = [{"id": "man-777", "locales": [{"locale": "es-ES", "name": "Editorial Ejemplo"}]}]
        cls.last_create = None
        cls.paths = []


S.reset()


def ok(data):
    return {"code": 0, "message": "Success", "request_id": "req-1", "data": data}


def err(code, msg):
    return {"code": code, "message": msg, "request_id": "req-1", "data": {}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def handle_any(self, method):
        try:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            if u.path.startswith("/img/"):
                return self.send(b"\xff\xd8\xff fake jpeg " + u.path.encode(), ctype="image/jpeg")
            if u.path.startswith("/api/v2/token/"):
                return self.token(u.path, q)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type") or ""
            multipart = ctype.startswith("multipart/form-data")
            if q.get("app_key") != APP_KEY:
                return self.send(err(106005, "Invalid app_key"))
            if abs(int(q.get("timestamp", 0)) - time.time()) > 300:
                return self.send(err(106002, "Timestamp expired"))
            if q.get("sign") != expected_sign(u.path, q, "" if multipart else raw.decode("utf-8")):
                S.bad_sigs += 1
                return self.send(err(106001, "The signature is invalid"))
            if S.tokens.get(self.headers.get("x-tts-access-token"), 0) < time.time():
                return self.send(err(105002, "Expired access token"))
            shop_free = u.path.startswith("/authorization/") or u.path == "/product/202309/images/upload"
            if not shop_free and q.get("shop_cipher") != CIPHER:
                return self.send(err(106011, "Invalid shop_cipher"))
            S.paths.append(u.path)
            if S.throttle_once:
                S.throttle_once = False
                return self.send({"code": 429, "message": "Too many requests"}, 429)
            body, files = {}, {}
            if multipart:
                msg = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
                for part in msg.iter_parts():
                    files[part.get_param("name", header="content-disposition")] = part.get_payload(decode=True)
            elif raw:
                body = json.loads(raw.decode("utf-8"))
            return self.send(self.api(method, u.path, q, body, files))
        except Exception as e:
            self.send({"code": 500, "message": repr(e)}, 500)

    def token(self, path, q):
        if q.get("app_key") != APP_KEY or q.get("app_secret") != SECRET:
            return self.send(err(36004004, "invalid app_key or app_secret"))
        if path.endswith("/get") and q.get("auth_code") != "code-ok":
            return self.send(err(36004004, "invalid auth_code"))
        if path.endswith("/refresh"):
            if q.get("refresh_token") not in S.refresh:
                return self.send(err(36004004, "invalid refresh_token"))
            S.refreshes += 1
        n = len(S.tokens) + 1
        S.tokens[f"TTP_a{n}"] = time.time() + 7 * 86400
        S.refresh.add(f"TTP_r{n}")
        self.send(ok({"access_token": f"TTP_a{n}", "access_token_expire_in": int(time.time() + 7 * 86400),
                      "refresh_token": f"TTP_r{n}", "refresh_token_expire_in": int(time.time() + 365 * 86400),
                      "open_id": "70107", "seller_name": "Libros Test ES", "seller_base_region": "ES",
                      "user_type": 0, "granted_scopes": ["seller.product.basic"]}))

    def api(self, m, p, q, b, files):
        if p == "/authorization/202309/shops":
            return ok({"shops": [{"id": "7495", "name": "Libros Test ES", "region": "ES", "seller_type": "LOCAL",
                                  "cipher": CIPHER, "code": "ESLCTEST"}]})
        if p == "/logistics/202309/warehouses":
            return ok({"warehouses": [
                {"id": "wh-return", "name": "Devoluciones", "type": "RETURN_WAREHOUSE", "effect_status": "ENABLED", "is_default": True},
                {"id": "wh-728", "name": "Almacén Madrid", "type": "SALES_WAREHOUSE", "effect_status": "ENABLED", "is_default": True}]})
        if p == "/product/202501/compliance/responsible_persons/search":
            return ok({"responsible_persons": S.rps, "total_count": len(S.rps)})
        if p == "/product/202501/compliance/manufacturers/search":
            return ok({"manufacturers": S.mans, "total_count": len(S.mans)})
        if p == "/product/202309/categories":
            cats = [{"id": "600001", "parent_id": "0", "local_name": "Libros y revistas", "is_leaf": False, "permission_statuses": ["AVAILABLE"]},
                    {"id": BOOKS_CAT, "parent_id": "600001", "local_name": "Libros infantiles", "is_leaf": True, "permission_statuses": ["AVAILABLE"]},
                    {"id": "601451", "parent_id": "600001", "local_name": "Libros de texto", "is_leaf": True, "permission_statuses": ["INVITE_ONLY"]},
                    {"id": "700001", "parent_id": "0", "local_name": "Electrónica", "is_leaf": False, "permission_statuses": ["AVAILABLE"]}]
            kw = (q.get("keyword") or "").lower()
            return ok({"categories": [c for c in cats if kw in c["local_name"].lower()]})
        if p == "/product/202309/categories/recommend":
            return ok({"leaf_category_id": BOOKS_CAT, "categories": [
                {"id": "600001", "name": "Libros y revistas", "level": 1, "is_leaf": False},
                {"id": BOOKS_CAT, "name": "Libros infantiles", "level": 2, "is_leaf": True, "permission_statuses": ["AVAILABLE"]}]})
        if p == f"/product/202309/categories/{BOOKS_CAT}/attributes":
            return ok({"attributes": ATTRIBUTES})
        if p == f"/product/202309/categories/{BOOKS_CAT}/rules":
            return ok({"responsible_person": {"is_required": True}, "manufacturer": {"is_required": True},
                       "package_dimension": {"is_required": True}, "seller_sku": {"is_required": False},
                       "product_certifications": []})
        if p == "/product/202309/images/upload":
            if not files.get("data") or files.get("use_case") != b"MAIN_IMAGE":
                return err(12052001, "data and use_case are required")
            if b"refurb" in files["data"]:
                return err(12052046, "The image dimensions must be between 300x300 px and 4000x4000 px")
            S.uploads += 1
            return ok({"uri": f"tos-maliva-i-o3syd03w52-us/img{S.uploads}", "url": f"https://p16.tiktokcdn.com/img{S.uploads}.jpg",
                       "width": 800, "height": 800, "use_case": "MAIN_IMAGE"})
        if p == "/product/202309/products" and m == "POST":
            return self.create(b)
        pid = p.split("/")[4] if p.startswith("/product/202309/products/") and len(p.split("/")) > 5 else None
        if pid and p.endswith("/inventory/update"):
            S.inv_calls += 1
            prod = S.products.get(pid)
            sku = b["skus"][0]
            inv = sku["inventory"][0]
            if not prod or sku["id"] != prod["sku_id"] or inv["warehouse_id"] != "wh-728" or not 1 <= inv["quantity"] <= 99999:
                return err(12019002, "Invalid inventory update")
            prod["qty"] = inv["quantity"]
            return ok({})
        if pid and p.endswith("/prices/update"):
            S.price_calls += 1
            prod = S.products.get(pid)
            sku = b["skus"][0]
            if not prod or sku["price"]["currency"] != "EUR":
                return err(12019003, "Invalid price update")
            prod["price"] = sku["price"]["amount"]
            return ok({})
        if p in ("/product/202309/products/activate", "/product/202309/products/deactivate"):
            ids = b.get("product_ids") or []
            if len(ids) > 20:
                return err(12019004, "Max 20 product ids")
            errors = []
            for i in ids:
                if i not in S.products:
                    errors.append({"code": 12019005, "message": "product not found", "detail": {"product_id": i}})
                else:
                    S.products[i]["status"] = "ACTIVATE" if p.endswith("/activate") else "SELLER_DEACTIVATED"
            return ok({"errors": errors})
        if p == "/product/202502/products/search":
            want = set(b.get("seller_skus") or [])
            prods = [{"id": i, "title": x["title"], "status": x["status"],
                      "skus": [{"id": x["sku_id"], "seller_sku": x["seller_sku"],
                                "price": {"currency": "EUR", "tax_exclusive_price": x["price"]},
                                "inventory": [{"warehouse_id": "wh-728", "quantity": x["qty"]}]}]}
                     for i, x in S.products.items() if not want or x["seller_sku"] in want]
            return ok({"products": prods, "total_count": len(prods), "next_page_token": ""})
        return err(40400, f"no mock for {m} {p}")

    def create(self, b):
        S.creates += 1
        S.last_create = b
        if not b.get("title") or len(b["title"]) > 255:
            return err(12052002, "title length must be 1-255")
        if b.get("category_id") != BOOKS_CAT:
            return err(12052003, "category is not a leaf category or not available")
        imgs = b.get("main_images") or []
        if not 1 <= len(imgs) <= 9 or not all(str(i.get("uri", "")).startswith("tos-") for i in imgs):
            return err(12052004, "main_images must be uploaded through the Upload Product Image API")
        attrs = {a["id"]: a for a in b.get("product_attributes") or []}
        for req in [a for a in ATTRIBUTES if a["is_requried"] and a["type"] == "PRODUCT_PROPERTY"]:
            got = attrs.get(req["id"])
            vals = (got or {}).get("values") or []
            if not vals or (not req["is_customizable"] and vals[0].get("id") not in [v["id"] for v in req["values"]]):
                return err(12052005, f"Required attribute [{req['name']}] is missing")
        if b.get("responsible_person_ids") != ["rp-555"] or b.get("manufacturer_ids") != ["man-777"]:
            return err(12052006, "The responsible person and manufacturer are required for EU products")
        dims, weight = b.get("package_dimensions") or {}, b.get("package_weight") or {}
        if dims.get("unit") != "CENTIMETER" or not all(str(dims.get(k, "")).isdigit() for k in ("length", "width", "height")):
            return err(12052007, "package_dimensions must be positive whole numbers in CENTIMETER")
        if weight.get("unit") != "KILOGRAM" or float(weight.get("value") or 0) <= 0:
            return err(12052008, "package_weight is invalid")
        sku = (b.get("skus") or [{}])[0]
        if not 6 <= len(sku.get("seller_sku") or "") <= 50:
            return err(12052009, "seller_sku must be 6-50 characters")
        price = sku.get("price") or {}
        if price.get("currency") != "EUR" or not isinstance(price.get("amount"), str) or float(price["amount"]) < 0.01:
            return err(12052010, "price is invalid")
        inv = (sku.get("inventory") or [{}])[0]
        if inv.get("warehouse_id") != "wh-728" or not 1 <= int(inv.get("quantity", 0)) <= 99999:
            return err(12052011, "warehouse_id or quantity invalid")
        code = sku.get("identifier_code") or {}
        if code.get("type") != "ISBN" or len(code.get("code", "")) != 13:
            return err(12052012, "identifier_code is invalid")
        if any(x["seller_sku"] == sku["seller_sku"] for x in S.products.values()):
            return err(12052013, "Duplicate seller_sku")
        pid, sku_id = str(1729000000000000000 + len(S.products)), str(1729500000000000000 + len(S.products))
        S.products[pid] = {"title": b["title"], "status": "PENDING", "sku_id": sku_id, "seller_sku": sku["seller_sku"],
                           "price": price["amount"], "qty": inv["quantity"]}
        return ok({"product_id": pid, "skus": [{"id": sku_id, "seller_sku": sku["seller_sku"]}], "warnings": []})


RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None, allowed=None, attrs=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TIKTOK_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = "import tiktok_push as m\nfrom tiktok_push import main"
    if allowed is not None:
        code += f"\nm.ALLOWED_CONDITIONS.clear(); m.ALLOWED_CONDITIONS.update({sorted(allowed)!r})"
    if attrs is not None:
        code += f"\nm.PRODUCT_ATTRIBUTES[:] = {attrs!r}"
    code += f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def by_sku(seller_sku):
    return next(((pid, x) for pid, x in S.products.items() if x["seller_sku"] == seller_sku), (None, {}))


def test_tiktok():
    print("\n=== tiktok_push.py (TikTok Shop Partner API mock) ===")
    sys.path.insert(0, str(ROOT))
    import tiktok_push as tt
    s = "abc000def111"
    check("signature matches TikTok's documented examples (GET, JSON body, multipart)",
          tt.sign(s, "/authorization/202309/shops", {"app_key": "123456", "timestamp": "1234567890"})
          == "c8767e4fd7a1b48a0ce3fd23197f058159ce11b53f1ae1ad6d1ec931a1da5540"
          and tt.sign(s, "/product/202309/products", {"app_key": "123456", "timestamp": "1234567890",
                                                       "shop_cipher": "GCP_XF90igAAAABh00qsWgtvOiGFNqyubMt3"},
                      '{"title":"Café","price":"9.99"}')
          == "3f6ebac240b9681f7ad14dacf791c548060bcaba2dc925cc1946121517720ba1"
          and tt.sign(s, "/order/202309/orders", {"timestamp": "1234567890", "sign": "ffff", "access_token": "TTP_x",
                                                   "app_key": "123456", "page_size": "20"})
          == "36528d5e25a5c5ba57362f811a2300811bce5b98fe7d1929727c0a0085f49023")
    check("seller SKU padded to TikTok's 6-50 characters", tt.seller_sku("T-NEW") == "SKU-T-NEW"
          and tt.seller_sku("142534") == "142534")

    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["auth-url", "--service-id", "7123456789"], tmp, ENV)
        check("auth-url prints the seller authorization link",
              "services.tiktokshop.com/open/authorize?service_id=7123456789" in out, out)

        rc, out = run(["check"], tmp, ENV)
        check("no authorization yet -> clear message", rc != 0 and "No TikTok Shop authorization yet" in out, out[-200:])

        rc, out = run(["token", "code-bad"], tmp, ENV)
        check("invalid authorization code reported", rc != 0 and "invalid auth_code" in out, out[-200:])

        rc, out = run(["token", "code-ok"], tmp, ENV)
        tok = json.loads((Path(tmp) / "tiktok_token.json").read_text(encoding="utf-8"))
        check("token: code exchanged, tokens saved but not printed", rc == 0 and tok["access_token"] == "TTP_a1"
              and "TTP_a1" not in out and "TTP_r1" not in out and "Libros Test ES" in out, out[-300:])

        rc, out = run(["check"], tmp, dict(ENV, TIKTOK_APP_SECRET="wrong"))
        check("wrong app secret -> signature error with a hint", rc != 0 and "Check TIKTOK_APP_SECRET" in out, out[-200:])

        S.bad_sigs = 0
        rc, out = run(["check"], tmp, ENV)
        tok = json.loads((Path(tmp) / "tiktok_token.json").read_text(encoding="utf-8"))
        check("check: shop, warehouses and GPSR listed; shop cipher looked up and saved",
              rc == 0 and "region ES" in out and "wh-728" in out and "EU Rep SL" in out and "Editorial Ejemplo" in out
              and tok.get("shop_cipher") == CIPHER and S.bad_sigs == 0, out[-500:])

        rc, out = run(["categories", "--find", "libro"], tmp, ENV)
        rc2, out2 = run(["recommend", "Dragona. Libro para bebes con rimas"], tmp, ENV)
        check("categories and recommend find the books category (restricted ones flagged)",
              f"{BOOKS_CAT:<10} (leaf) Libros infantiles" in out and "[INVITE_ONLY]" in out and "Electr" not in out
              and f"suggests category {BOOKS_CAT}" in out2, out + out2)

        rc, out = run(["attributes", BOOKS_CAT], tmp, ENV)
        check("attributes: required ones with ready-to-paste values, ISBN auto-filled, GPSR rules shown",
              "REQUIRED  Idioma" in out and '"1000201"' in out and "filled with the ISBN automatically" in out
              and "responsible_person, manufacturer" in out and "Formato" not in out, out)

        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = json.loads((Path(tmp) / "tiktok_products.json").read_text(encoding="utf-8"))
        p0 = prev[0] if prev else {}
        sku = (p0.get("skus") or [{}])[0]
        check("dry run without credentials builds the payload with placeholders, sends nothing",
              rc == 0 and len(prev) == 1 and S.creates == 0 and "placeholders" in out
              and sku.get("seller_sku") == "SKU-T-NEW" and sku.get("price") == {"amount": "8.50", "currency": "EUR"}
              and sku.get("identifier_code") == {"code": "9788409591923", "type": "ISBN"}
              and p0.get("package_weight") == {"value": "0.400", "unit": "KILOGRAM"}
              and "<p>ISBN: 9788409591923</p>" in p0.get("description", ""), out[-400:])
        check("only new books with images are pushed; others skipped with reasons",
              "not sold on TikTok Shop" in out and "sold out at source" in out and "no ISBN/EAN" in out, out[-500:])

        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        check("real push without CATEGORY_ID stops with instructions",
              rc != 0 and "CATEGORY_ID is not set" in out and S.creates == 0, out[-300:])

        rc, out = run(["push", pf, "--yes", "--category", BOOKS_CAT], tmp, ENV)
        check("missing required attribute detected before sending", rc != 0 and "needs: Idioma" in out
              and S.creates == 0, out[-300:])

        S.throttle_once, S.bad_sigs = True, 0
        rc, out = run(["push", pf, "--yes", "--category", BOOKS_CAT], tmp, ENV, allowed=ALLOW_ALL, attrs=[LANG_ATTR])
        state = json.loads((Path(tmp) / "tiktok_posted.json").read_text(encoding="utf-8"))
        check("push creates products (429 retried, every signature valid incl. multipart uploads)",
              set(state) == {"T-NEW", "T-USED"} and state["T-NEW"]["product_id"] and state["T-NEW"]["sku_id"]
              and "retrying" in out and S.bad_sigs == 0, out[-600:])
        attrs = {x["id"]: x for x in S.last_create["product_attributes"]}
        check("ISBN attribute filled, GPSR ids and default sales warehouse used",
              attrs.get("100990", {}).get("values") == [{"name": "9788410413290"}] and "100107" in attrs
              and S.last_create["responsible_person_ids"] == ["rp-555"] and S.last_create["manufacturer_ids"] == ["man-777"]
              and S.last_create["skus"][0]["inventory"][0]["warehouse_id"] == "wh-728", str(S.last_create)[:500])
        check("photos uploaded as files first; TikTok's image rejection shown, product not recorded",
              S.uploads >= 2 and all(i["uri"].startswith("tos-") for i in S.last_create["main_images"])
              and "! T-REFURB: 12052046" in out and "T-REFURB" not in state, out[-500:])

        uploads, creates = S.uploads, S.creates
        rc, out = run(["push", pf, "--yes", "--category", BOOKS_CAT], tmp, ENV, allowed=ALLOW_ALL, attrs=[LANG_ATTR])
        check("re-run: nothing duplicated, no photo uploaded twice",
              S.creates == creates and S.uploads == uploads and "already pushed" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        pid, prod = by_sku("SKU-T-NEW")
        check("--resend: stock 5 and price 8.08 (half-up)", prod.get("qty") == 5 and prod.get("price") == "8.08"
              and "~ T-NEW: 8.08 EUR x5" in out, str(prod))

        calls = (S.price_calls, S.inv_calls)
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        check("--resend with nothing changed sends no updates", (S.price_calls, S.inv_calls) == calls, out[-200:])

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-NEW":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(sold_file), "--yes", "--offline-sold-out"], tmp, ENV)
        check("--offline-sold-out deactivates a sold-out product",
              by_sku("SKU-T-NEW")[1]["status"] == "SELLER_DEACTIVATED" and "deactivated" in out, out[-300:])

        by_sku("T-USED")[1]["status"] = "ACTIVATE"           # 6 characters: no SKU- prefix needed
        rc, out = run(["status"], tmp, ENV)
        check("status: live / deactivated per product with price and stock",
              rc == 0 and "T-NEW: deactivated" in out and "T-USED: LIVE" in out and "stock=5" in out and "stock=1" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend"], tmp, ENV)
        check("back in stock + --resend reactivates it", by_sku("SKU-T-NEW")[1]["status"] == "ACTIVATE"
              and "T-NEW: reactivated" in out, out[-300:])

        tf = Path(tmp) / "tiktok_token.json"
        t = json.loads(tf.read_text(encoding="utf-8"))
        t["access_token_expire_in"] = int(time.time()) - 10
        tf.write_text(json.dumps(t), encoding="utf-8")
        before = S.refreshes
        rc, out = run(["status"], tmp, ENV)
        t2 = json.loads(tf.read_text(encoding="utf-8"))
        check("expired access token refreshed automatically and saved (shop cipher kept)",
              rc == 0 and S.refreshes == before + 1 and t2["access_token"] != t["access_token"]
              and t2.get("shop_cipher") == CIPHER, out[-200:])

        t2["access_token_expire_in"], t2["refresh_token"] = 0, "TTP_revoked"
        tf.write_text(json.dumps(t2), encoding="utf-8")
        rc, out = run(["status"], tmp, ENV)
        check("revoked refresh token -> clear 'authorize again' message", rc != 0 and "Authorize again" in out, out[-200:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        S.rps = []
        rc, out = run(["push", pf, "--yes", "--category", BOOKS_CAT, "--limit", "1"], tmp,
                      dict(ENV, TIKTOK_REFRESH_TOKEN="r-0"), attrs=[LANG_ATTR])
        check("TIKTOK_REFRESH_TOKEN (Bitwarden) works without a token file",
              S.refreshes == 1 and (Path(tmp) / "tiktok_token.json").exists(), out[-300:])
        check("category needs a GPSR responsible person but none exists -> stops before sending",
              rc != 0 and "an EU responsible person (GPSR)" in out and S.creates == 0, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_tiktok()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
