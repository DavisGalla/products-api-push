#!/usr/bin/env python3
"""
End-to-end test for aliexpress_push.py, run against a LOCAL MOCK of the
AliExpress Open Platform API. The mock recomputes the HMAC-SHA256 signature
of every request (business calls on /sync, token calls on /rest/auth/...),
checks the access token, and implements the calls the script uses: tokens,
merchant profile, shipping templates, GPSR lists, EAN lookup, category tree
and attributes, photo bank upload (multipart), product post (with the
documented validation rules), online/offline, price/stock batch updates and
the product list. Nothing is sent to AliExpress, and your real
aliexpress_posted.json / aliexpress_token.json are not touched (each test
runs in a temporary folder).

    python tests/test_aliexpress.py
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
APP_KEY = "500123"
SECRET = "test-app-secret"
ENV = {"ALIEXPRESS_API_URL": BASE, "ALIEXPRESS_APP_KEY": APP_KEY, "ALIEXPRESS_APP_SECRET": SECRET}
ALLOW_ALL = {"NewCondition", "UsedCondition", "RefurbishedCondition"}

# AliExpress EAN database in the mock. T-REFURB's ISBN (9780000000002) is deliberately unknown.
EANS = {
    "9788409591923": {"success": True, "leaf_cate_id": 200001, "ean_code": "9788409591923",
                      "main_images": ["https://ae01.alicdn.com/kf/ean-dragona.jpg"],
                      "title_list": [{"language": "es", "text": "Dragona (AliExpress)"}],
                      "product_properties": [{"property_id": 2, "property_name": "Brand Name",
                                              "property_value_id": 201512802, "value_name": "NONE"}]},
    "9788410413290": {"success": True, "leaf_cate_id": 200001, "ean_code": "9788410413290",
                      "main_images": ["https://ae01.alicdn.com/kf/ean-coleccion.jpg"],
                      "title_list": [{"language": "es", "text": "Coleccion (AliExpress)"}],
                      "product_properties": [{"property_id": 2, "property_name": "Brand Name",
                                              "property_value_id": 201512802, "value_name": "NONE"}]},
}
LEAF = {200001}
REQUIRED_ATTRS = {200001: {2}}
TEMPLATES = [{"template_id": 1000, "template_name": "Shipping Cost Template for New Sellers", "is_default": False},
             {"template_id": 716259613, "template_name": "Envio gratis", "is_default": True}]


class S:
    @classmethod
    def reset(cls):
        cls.tokens = {}                 # access_token -> expiry
        cls.refresh = {"r-0"}           # valid refresh tokens
        cls.products = {9001: {"subject": "Producto ajeno", "status": "onSelling", "sku": "OTHER", "price": "3.00",
                               "inventory": 1}}
        cls.posts = cls.uploads = cls.refreshes = cls.bad_sigs = 0
        cls.throttle_once = False
        cls.msr = [{"id": 555, "name": "EU Rep SL", "country": "ES"}]
        cls.last_post = None
        cls.methods = []


S.reset()


def err(code, msg, sub_code=None):
    return {"error_response": {"code": code, "msg": msg, "sub_code": sub_code, "sub_msg": msg, "request_id": "req1"}}


def wrap(method, body):
    return {method.replace(".", "_") + "_response": dict(body, request_id="req1")}


def expected_sign(params, prefix=""):
    text = prefix + "".join(f"{k}{params[k]}" for k in sorted(params) if params[k] not in (None, ""))
    return hmac.new(SECRET.encode(), text.encode(), hashlib.sha256).hexdigest().upper()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/img/"):
            return self.send(200, b"\xff\xd8\xff fake jpeg " + self.path.encode(), "image/jpeg")
        self.send(404, {"message": "no mock"})

    def read_params(self):
        u = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        files = {}
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type") or ""
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    files[name] = part.get_payload(decode=True)
                else:
                    params[name] = part.get_payload(decode=True).decode("utf-8")
        elif raw:
            params.update({k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()})
        return u.path, params, files

    def do_POST(self):
        try:
            path, params, files = self.read_params()
            sent_sign = params.pop("sign", None)
            if params.get("app_key") != APP_KEY:
                return self.send(200, err("InvalidAppKey", "Invalid app key"))
            if path.startswith("/rest/auth/"):
                api = path[len("/rest"):]
                if sent_sign != expected_sign(params, api):
                    S.bad_sigs += 1
                    return self.send(200, {"code": "IncompleteSignature", "message": "The request signature does not conform to platform standards"})
                return self.token_call(api, params)
            if path != "/sync":
                return self.send(404, {"message": f"no mock for {path}"})
            if sent_sign != expected_sign(params):
                S.bad_sigs += 1
                return self.send(200, err("IncompleteSignature", "The request signature does not conform to platform standards"))
            if abs(int(params.get("timestamp", 0)) / 1000 - time.time()) > 600:
                return self.send(200, err("InvalidTimestamp", "timestamp out of range"))
            if S.tokens.get(params.get("access_token"), 0) < time.time():
                return self.send(200, err("IllegalAccessToken", "The specified access token is invalid or expired"))
            method = params["method"]
            S.methods.append(method)
            if S.throttle_once:
                S.throttle_once = False
                return self.send(200, err("ApiCallLimit", "This ban will last for 1 more seconds"))
            return self.send(200, self.business(method, params, files))
        except Exception as e:
            self.send(500, {"message": repr(e)})

    def token_call(self, api, p):
        if api == "/auth/token/create" and p.get("code") != "code-ok":
            return self.send(200, {"code": "InvalidCode", "message": "authorization code invalid or expired"})
        if api == "/auth/token/refresh":
            if p.get("refresh_token") not in S.refresh:
                return self.send(200, {"code": "IllegalRefreshToken", "message": "refresh token invalid or expired"})
            S.refreshes += 1
        n = len(S.tokens) + 1
        access, refresh = f"a-{n}", f"r-{n}"
        S.tokens[access] = time.time() + 3600
        S.refresh.add(refresh)
        now = int(time.time() * 1000)
        self.send(200, {"code": "0", "access_token": access, "refresh_token": refresh, "expires_in": 3600,
                        "refresh_expires_in": 86400 * 30, "expire_time": now + 3600 * 1000,
                        "refresh_token_valid_time": now + 86400 * 30 * 1000, "user_nick": "es_books_shop",
                        "request_id": "req1"})

    def business(self, m, p, files):
        J = lambda k: json.loads(p[k])
        if m == "aliexpress.solution.merchant.profile.get":
            return wrap(m, {"shop_name": "Libros Test", "shop_id": 77, "country_code": "ES", "shop_type": "local_seller",
                            "product_posting_forbidden": False})
        if m == "aliexpress.freight.redefining.listfreighttemplate":
            return wrap(m, {"aeop_freight_template_d_t_o_list": {"aeopfreighttemplatedtolist": TEMPLATES},
                            "result_success": True})
        if m == "aliexpress.merchant.msr.list":
            return wrap(m, {"result": {"data": S.msr, "success": True}})
        if m == "aliexpress.merchant.manufacture.list":
            return wrap(m, {"result": {"data": [{"id": 777, "name": "Editorial Ejemplo"}], "success": True}})
        if m == "aliexpress.product.eancode.info.query":
            info = EANS.get(p["ean_code"])
            return wrap(m, {"result": info or {"success": False, "error_code": "EAN_NOT_FOUND",
                                               "error_message": "ean code not found"}})
        if m == "aliexpress.solution.seller.category.tree.query":
            tree = {0: [{"children_category_id": 7, "is_leaf_category": False, "level": 1,
                         "multi_language_names": json.dumps({"es": "Libros", "en": "Books"})},
                        {"children_category_id": 8, "is_leaf_category": False, "level": 1,
                         "multi_language_names": json.dumps({"es": "Electrónica", "en": "Electronics"})}],
                    7: [{"children_category_id": 200001, "is_leaf_category": True, "level": 2,
                         "multi_language_names": json.dumps({"es": "Libros infantiles", "en": "Children's books"})}]}
            return wrap(m, {"children_category_list": {"category_info": tree.get(int(p["category_id"]), [])},
                            "is_success": True})
        if m == "aliexpress.category.redefining.getchildattributesresultbypostcateidandpath":
            return wrap(m, {"result": {"success": True, "attributes": {"aeop_attribute_dto": [
                {"id": 2, "names": "Marca", "required": True, "sku": False, "visible": True,
                 "values": {"aeop_attr_value_dto": [{"id": 201512802, "names": "NINGUNA"}]}},
                {"id": 1001, "names": "Idioma", "required": False, "key_attribute": True, "visible": True,
                 "attribute_show_type_value": "input"}]}}})
        if m == "aliexpress.photobank.redefining.uploadimageforsdk":
            if not files.get("image_bytes"):
                return err(15, "image_bytes missing", "isv.param-missing")
            S.uploads += 1
            return wrap(m, {"result": {"is_success": True, "status": "SUCCESS",
                                       "photobank_url": f"https://ae01.alicdn.com/kf/up-{S.uploads}-{p['file_name']}"}})
        if m == "aliexpress.solution.product.post":
            S.posts += 1
            b = J("post_product_request")
            S.last_post = b
            for f in ("language", "freight_template_id", "weight", "main_image_urls_list", "package_length",
                      "package_width", "package_height", "sku_info_list", "inventory_deduction_strategy"):
                if b.get(f) in (None, "", []):
                    return err(15, f"Required parameter {f} is missing", "isv.param-missing")
            if b["freight_template_id"] not in [t["template_id"] for t in TEMPLATES]:
                return err(15, "freight template not found", "isv.freight-template-not-exist")
            if b.get("aliexpress_category_id") not in LEAF:
                return err(15, "category is not a leaf category", "isv.category-not-leaf")
            if b["inventory_deduction_strategy"] not in ("place_order_withhold", "payment_success_deduct"):
                return err(15, "invalid inventory_deduction_strategy", "isv.param-invalid")
            if not all(str(u).startswith("https://ae01.alicdn.com/") for u in b["main_image_urls_list"]) or \
                    len(b["main_image_urls_list"]) > 6:
                return err(15, "Main image check failed: image URL is not a valid platform link",
                           "isv.F00-00-10006-001")
            if not b.get("msr_eu_id"):
                return err(15, "Due to EU GPSR compliance requirements, EU Responsible Person information must be "
                               "included", "isv.-99999")
            subj = (b.get("multi_language_subject_list") or [{}])[0].get("subject", "")
            if not 1 <= len(subj) <= 218:
                return err(15, "subject length must be 1-218", "isv.param-invalid")
            have = {x.get("aliexpress_attribute_name_id") for x in b.get("attribute_list") or []}
            missing = REQUIRED_ATTRS.get(b["aliexpress_category_id"], set()) - have
            if missing:
                return err(15, f"The required attribute value {sorted(missing)} of the product cannot be empty",
                           "isv.707010019")
            sku = b["sku_info_list"][0]
            if not sku.get("sku_code") or not isinstance(sku.get("price"), str) or float(sku["price"]) < 0.01:
                return err(15, "sku price/sku_code invalid", "isv.param-invalid")
            if any(x["sku"] == sku["sku_code"] for x in S.products.values()):
                return err(15, "SKU code conflict", "isv.200018")
            pid = 1005000000000 + len(S.products)
            S.products[pid] = {"subject": subj, "status": "offline", "sku": sku["sku_code"], "price": sku["price"],
                               "inventory": sku["inventory"], "category": b["aliexpress_category_id"]}
            return wrap(m, {"result": {"product_id": pid, "success": True}})
        if m in ("aliexpress.postproduct.redefining.onlineaeproduct", "aliexpress.postproduct.redefining.offlineaeproduct"):
            ids = [int(x) for x in p["product_ids"].split(";")]
            bad = [i for i in ids if i not in S.products]
            for i in ids:
                if i in S.products:
                    S.products[i]["status"] = "onSelling" if "online" in m else "offline"
            res = {"success": not bad, "modify_count": len(ids) - len(bad)}
            if bad:
                res["error_details"] = {"error_detail": [{"error_code": "13001", "error_message": "product not found",
                                                          "product_ids": bad}]}
            return wrap(m, {"result": res})
        if m in ("aliexpress.solution.batch.product.price.update", "aliexpress.solution.batch.product.inventory.update"):
            lst = J("mutiple_product_update_list")
            if len(lst) > 20:
                return err(15, "max 20 products", "isv.param-invalid")
            ok, failed = [], []
            for item in lst:
                prod = S.products.get(item["product_id"])
                if not prod:
                    failed.append({"product_id": item["product_id"], "error_code": "x", "error_message": "no product"})
                    continue
                for s in item["multiple_sku_update_list"]:
                    if "price" in s:
                        prod["price"] = s["price"]
                    if "inventory" in s:
                        prod["inventory"] = s["inventory"]
                ok.append({"product_id": item["product_id"]})
            return wrap(m, {"update_success": not failed, "update_successful_list": {"synchronize_product_response_dto": ok},
                            "update_failed_list": {"synchronize_product_response_dto": failed} if failed else None})
        if m == "aliexpress.solution.product.list.get":
            q = J("aeop_a_e_product_list_query")
            items = [{"product_id": pid, "subject": x["subject"], "product_min_price": x["price"],
                      "currency_code": "EUR", "ws_display": "user_offline" if x["status"] == "offline" else None}
                     for pid, x in S.products.items() if x["status"] == q["product_status_type"]]
            size, page = int(q.get("page_size", 20)), int(q.get("current_page", 1))
            return wrap(m, {"result": {"success": True, "total_page": max(1, -(-len(items) // size)),
                                       "current_page": page, "product_count": len(items),
                                       "aeop_a_e_product_display_d_t_o_list": {
                                           "item_display_dto": items[(page - 1) * size: page * size]}}})
        return err(15, f"no mock for {m}", "isv.unknown")


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def run(args, cwd, env_extra=None, allowed=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALIEXPRESS_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = "import aliexpress_push as m\nfrom aliexpress_push import main"
    if allowed is not None:
        code += f"\nm.ALLOWED_CONDITIONS.clear(); m.ALLOWED_CONDITIONS.update({sorted(allowed)!r})"
    code += f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def by_sku(sku):
    return next(((pid, x) for pid, x in S.products.items() if x["sku"] == sku), (None, {}))


def test_aliexpress():
    print("\n=== aliexpress_push.py (AliExpress Open Platform mock) ===")
    sys.path.insert(0, str(ROOT))
    import aliexpress_push as ae
    # The two worked examples from AliExpress's "call API with HTTP requests" guide (secret "helloworld").
    biz = {"access_token": "test", "aliexpress_category_id": "200135143", "app_key": "123456",
           "method": "aliexpress.logistics.redefining.getonlinelogisticsinfo", "sign_method": "sha256",
           "timestamp": "1517820392000"}
    sysp = {"app_key": "12345678", "code": "3_500102_JxZ05Ux3cnnSSUm6dCxYg6Q26", "sign_method": "sha256",
            "timestamp": "1517820392000"}
    check("signature matches both examples in AliExpress's docs",
          ae.sign("helloworld", biz) == "F7F7926B67316C9D1E8E15F7E66940ED3059B1638C497D77973F30046EFB5BBB"
          and ae.sign("helloworld", sysp, "/auth/token/create") ==
          "35607762342831B6A417A0DED84B79C05FEFBF116969C48AD6DC00279A9F4D81")

    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["auth-url", "--redirect", "https://example.com/cb"], tmp, ENV)
        check("auth-url prints the authorization link", rc == 0 and "oauth/authorize" in out
              and f"client_id={APP_KEY}" in out and "redirect_uri=https%3A%2F%2Fexample.com%2Fcb" in out, out)

        rc, out = run(["check"], tmp, ENV)
        check("no authorization yet -> clear message", rc != 0 and "No AliExpress authorization yet" in out, out[-200:])

        rc, out = run(["token", "code-bad"], tmp, ENV)
        check("invalid authorization code reported", rc != 0 and "InvalidCode" in out, out[-200:])

        rc, out = run(["token", "code-ok"], tmp, ENV)
        tok = json.loads((Path(tmp) / "aliexpress_token.json").read_text(encoding="utf-8"))
        check("token: code exchanged (signed /rest call), tokens saved but not printed",
              rc == 0 and tok["access_token"] == "a-1" and tok["refresh_token"] == "r-1" and "a-1" not in out
              and "r-1" not in out and S.bad_sigs == 0, out[-300:])

        rc, out = run(["check"], tmp, dict(ENV, ALIEXPRESS_APP_SECRET="wrong"))
        check("wrong app secret -> signature error shown", rc != 0 and "IncompleteSignature" in out, out[-200:])

        rc, out = run(["check"], tmp, ENV)
        check("check: shop, default shipping template and GPSR person listed", rc == 0 and "Libros Test" in out
              and "716259613" in out and "(default)" in out and "EU Rep SL" in out, out[-500:])

        rc, out = run(["ean", "978-84-09-59192-3"], tmp, ENV)
        rc2, out2 = run(["ean", "9780000000002"], tmp, ENV)
        check("ean: known ISBN shows category/attributes, unknown ISBN says so",
              "category 200001" in out and "Brand Name=NONE" in out and "not in AliExpress's EAN database" in out2,
              out + out2)

        rc, out = run(["categories", "--find", "libro"], tmp, ENV)
        rc2, out2 = run(["categories", "--parent", "7"], tmp, ENV)
        check("categories: search and browse to a leaf category", "Libros" in out and "Electr" not in out
              and "200001" in out2 and "(leaf)" in out2, out + out2)

        rc, out = run(["attributes", "200001"], tmp, ENV)
        check("attributes: required attribute and its values listed",
              "REQUIRED" in out and "Marca" in out and "201512802" in out, out)

        rc, out = run(["push", pf, "--dry-run", "--category", "200001"], tmp)
        prev = json.loads((Path(tmp) / "aliexpress_products.json").read_text(encoding="utf-8"))
        b = prev[0] if prev else {}
        check("dry run without credentials builds the payload, sends nothing",
              rc == 0 and len(prev) == 1 and S.posts == 0 and "lookup skipped" in out
              and b["sku_info_list"][0] == {"sku_code": "T-NEW", "price": "8.50", "inventory": 1, "ean_code": "9788409591923"}
              and b["language"] == "es" and b["aliexpress_category_id"] == 200001
              and b["multi_language_subject_list"][0]["subject"].startswith("Dragona")
              and "Dragona" in json.dumps(b["multi_language_description_list"], ensure_ascii=False), out[-400:])
        check("only new books are pushed; used / no-ISBN / sold-out skipped with reasons",
              "not sold on AliExpress" in out and "sold out at source" in out and "no ISBN/EAN" in out, out[-500:])

        rc, out = run(["push", pf, "--dry-run"], tmp, ENV)
        prev = json.loads((Path(tmp) / "aliexpress_products.json").read_text(encoding="utf-8"))
        b = prev[0] if prev else {}
        check("dry run with credentials: EAN data, default shipping template and GPSR person used",
              S.posts == 0 and b.get("aliexpress_category_id") == 200001
              and b.get("main_image_urls_list") == ["https://ae01.alicdn.com/kf/ean-dragona.jpg"]
              and b.get("attribute_list") == [{"aliexpress_attribute_name_id": 2, "aliexpress_attribute_value_id": 201512802}]
              and b.get("freight_template_id") == 716259613 and b.get("msr_eu_id") == 555
              and b.get("manufacturer_id") == 777, str(b)[:600])

        S.throttle_once, S.bad_sigs = True, 0
        rc, out = run(["push", pf, "--yes", "--category", "200001"], tmp, ENV, allowed=ALLOW_ALL)
        state = json.loads((Path(tmp) / "aliexpress_posted.json").read_text(encoding="utf-8"))
        check("push creates products from EAN data and puts them online (rate limit retried)",
              set(state) == {"T-NEW", "T-USED"} and by_sku("T-NEW")[1].get("status") == "onSelling"
              and state["T-NEW"]["status"] == "online" and "rate limit" in out and S.bad_sigs == 0, out[-600:])
        check("unknown ISBN: own photo uploaded to the photo bank, category fallback used",
              S.uploads == 1 and S.last_post["aliexpress_category_id"] == 200001
              and S.last_post["main_image_urls_list"][0].startswith("https://ae01.alicdn.com/kf/up-1"), str(S.last_post)[:300])
        check("missing required attribute rejected with AliExpress's message, not recorded",
              "! T-REFURB: isv.707010019" in out and "T-REFURB" not in state, out[-400:])

        rc, out = run(["push", pf, "--yes", "--category", "200001", "--attribute", "2=201512802"], tmp, ENV,
                      allowed=ALLOW_ALL)
        state = json.loads((Path(tmp) / "aliexpress_posted.json").read_text(encoding="utf-8"))
        check("--attribute fills the required attribute; photo not uploaded twice; nothing duplicated",
              "T-REFURB" in state and S.uploads == 1 and "already pushed" in out
              and sum(1 for x in S.products.values() if x["sku"] == "T-NEW") == 1, out[-400:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        pid, prod = by_sku("T-NEW")
        check("--resend updates price (half-up: 8.075 -> 8.08) and stock in batches",
              prod.get("price") == "8.08" and prod.get("inventory") == 5 and "~ T-NEW: 8.08 EUR x5" in out, str(prod))

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-NEW":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(sold_file), "--yes", "--offline-sold-out"], tmp, ENV)
        state = json.loads((Path(tmp) / "aliexpress_posted.json").read_text(encoding="utf-8"))
        check("--offline-sold-out takes a sold-out product offline",
              by_sku("T-NEW")[1]["status"] == "offline" and state["T-NEW"]["status"] == "offline"
              and "taken offline" in out, out[-300:])

        rc, out = run(["status"], tmp, ENV)
        check("status: online / offline per product, foreign products flagged",
              rc == 0 and "T-NEW: offline" in out and "T-USED: ONLINE" in out and "not pushed by this script" in out,
              out[-400:])

        rc, out = run(["push", pf, "--yes", "--resend"], tmp, ENV)
        check("back in stock + --resend puts it online again",
              by_sku("T-NEW")[1]["status"] == "onSelling" and "T-NEW: back online" in out, out[-300:])

        tf = Path(tmp) / "aliexpress_token.json"
        t = json.loads(tf.read_text(encoding="utf-8"))
        t["expire_time"] = int(time.time() * 1000) - 1000
        tf.write_text(json.dumps(t), encoding="utf-8")
        before = S.refreshes
        rc, out = run(["status"], tmp, ENV)
        t2 = json.loads(tf.read_text(encoding="utf-8"))
        check("expired access token refreshed automatically and saved",
              rc == 0 and S.refreshes == before + 1 and t2["access_token"] != t["access_token"], out[-200:])

        t2["expire_time"], t2["refresh_token"] = 0, "r-revoked"
        tf.write_text(json.dumps(t2), encoding="utf-8")
        rc, out = run(["status"], tmp, ENV)
        check("revoked refresh token -> clear 'authorize again' message", rc != 0 and "Authorize again" in out, out[-200:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        S.msr = []
        rc, out = run(["push", pf, "--yes", "--limit", "1"], tmp, dict(ENV, ALIEXPRESS_REFRESH_TOKEN="r-0"))
        check("ALIEXPRESS_REFRESH_TOKEN (Bitwarden) works without a token file",
              S.refreshes == 1 and (Path(tmp) / "aliexpress_token.json").exists(), out[-300:])
        check("no GPSR responsible person: warned, AliExpress's rejection shown, nothing recorded",
              "no EU responsible person" in out and "! T-NEW: isv.-99999" in out
              and not (Path(tmp) / "aliexpress_posted.json").exists(), out[-400:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_aliexpress()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
