#!/usr/bin/env python3
"""
End-to-end test for temu_push.py, run against a LOCAL MOCK of the Temu Open
Platform (Partner API) router. The mock recomputes the MD5 signature of every
request exactly as Temu documents it, checks app key, access token, timestamp
and data_type, and implements the calls the script uses: token info, shipping
templates, categories, category recommendation, category template
(attributes / variations), variation id, image upload, goods add (with the
documented validation rules), stock edit, price change, sale status and the
goods list. Nothing is sent to Temu, and your real temu_posted.json is not
touched (each test runs in a temporary folder).

    python tests/test_temu.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
URL = f"http://127.0.0.1:{PORT}/openapi/router"
APP_KEY = "f9d5cc9313893a20d5aa85c654e8f503"
SECRET = "test-temu-secret"
TOKEN = "test-temu-access-token"
ENV = {"TEMU_API_URL": URL, "TEMU_APP_KEY": APP_KEY, "TEMU_APP_SECRET": SECRET, "TEMU_ACCESS_TOKEN": TOKEN}
ALLOW_ALL = {"NewCondition", "UsedCondition", "RefurbishedCondition"}
BOOKS_CAT = 44936
LANGUAGE_PROP = {"pid": 12, "templatePid": 9002, "refPid": 12, "vid": 381, "value": "Español"}
SPEC_ID = 266386378
TEMPLATE = {
    "inputMaxSpecNum": 1, "singleSpecValueNum": 1,
    "userInputParentSpecList": [{"parentSpecId": 1001, "parentSpecName": "Estilo"}],
    "templateInfo": {"templateId": 77, "goodsSpecProperties": [], "goodsProperties": [
        {"name": "ISBN", "required": True, "isSale": False, "controlType": 0, "pid": 501, "templatePid": 9001,
         "refPid": 77, "values": []},
        {"name": "Idioma", "required": True, "isSale": False, "controlType": 1, "pid": 12, "templatePid": 9002,
         "refPid": 12, "values": [{"vid": 381, "value": "Español"}, {"vid": 382, "value": "Inglés"}]},
        {"name": "Autor", "required": False, "isSale": False, "controlType": 0, "pid": 30, "templatePid": 9003,
         "refPid": 30, "values": []}]},
}


def expected_sign(params):
    parts = [str(k) + json.dumps(v, ensure_ascii=False, separators=(",", ":")).strip('"')
             for k, v in sorted(params.items())]
    return hashlib.md5((SECRET + "".join(parts) + SECRET).encode()).hexdigest().upper()


class S:
    @classmethod
    def reset(cls):
        cls.goods = {}                  # goodsId -> dict
        cls.adds = cls.uploads = cls.spec_calls = cls.bad_sigs = cls.price_changes = 0
        cls.throttle_once = False
        cls.last_add = None
        cls.types = []


S.reset()


def fail(code, msg):
    return {"success": False, "errorCode": code, "errorMsg": msg, "requestId": "req-1"}


def ok(result):
    return {"success": True, "errorCode": 1000000, "errorMsg": "", "requestId": "req-1", "result": result}


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
        self.send({"message": "only POST is supported"}, 405)

    def do_POST(self):
        try:
            if self.path != "/openapi/router":
                return self.send({"message": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            sent = body.pop("sign", None)
            if not sent:
                return self.send(fail(3000040, "there is no sign in body."))
            if body.get("app_key") != APP_KEY:
                return self.send(fail(3000026, "app_key not exists."))
            if sent != expected_sign(body):
                S.bad_sigs += 1
                return self.send(fail(3000001, "SIGN_UNVALID"))
            if abs(int(body.get("timestamp", 0)) - time.time()) > 300:
                return self.send(fail(3000012, "timestamp is expired."))
            if str(body.get("data_type", "")).upper() != "JSON":
                return self.send(fail(3000014, "data_type is invalid"))
            if body.get("access_token") != TOKEN:
                return self.send(fail(3000031, "access_token not exists."))
            t = body["type"]
            S.types.append(t)
            if S.throttle_once:
                S.throttle_once = False
                return self.send(fail(4000004, "RATE_LIMIT_EXCEED_EXCEPTION"))
            return self.send(self.api(t, body))
        except Exception as e:
            self.send({"message": repr(e)}, 500)

    def api(self, t, b):
        if t == "bg.open.accesstoken.info.get":
            return ok({"mallId": 634418, "regionId": 109, "expiredTime": int(time.time() + 86400 * 300) * 1000,
                       "apiScopeList": ["bg.local.goods.add", "bg.local.goods.image.upload", "bg.local.goods.stock.edit",
                                        "bg.local.goods.sale.status.set", "bg.local.goods.list.query"]})
        if t == "bg.freight.template.list.query":
            return ok({"templateList": [{"templateId": "LFT-15630053225717890848", "templateName": "Envío estándar"}]})
        if t == "bg.local.goods.cats.get":
            tree = {0: [{"catId": 3000, "catName": "Books", "leaf": False, "availableStatus": 0, "catType": 1},
                        {"catId": 4000, "catName": "Toys & Games", "leaf": False, "availableStatus": 0, "catType": 1}],
                    3000: [{"catId": BOOKS_CAT, "catName": "Children's Books", "leaf": True, "availableStatus": 0, "catType": 1},
                           {"catId": 44937, "catName": "Comics", "leaf": True, "availableStatus": 1, "catType": 1}]}
            return ok({"goodsCatsList": tree.get(int(b.get("parentCatId", 0)), [])})
        if t == "bg.local.goods.category.recommend":
            return ok({"catId": BOOKS_CAT, "catIdList": [BOOKS_CAT, 44937]})
        if t == "bg.local.goods.template.get":
            return ok(TEMPLATE) if b.get("catId") == BOOKS_CAT else fail(150010124, "The catId not a leaf category")
        if t == "bg.local.goods.spec.id.get":
            S.spec_calls += 1
            if b.get("catId") != BOOKS_CAT or b.get("parentSpecId") != 1001 or not b.get("childSpecName"):
                return fail(150010125, "Non-existent parentSpecId")
            return ok({"specId": SPEC_ID})
        if t == "bg.local.goods.image.upload":
            url = b.get("fileUrl", "")
            if not url.startswith("http"):
                return fail(150010003, "Invalid Request Parameters")
            if "refurb" in url:
                return fail(150011066, "The input image is incorrect, the aspect ratio is not 1:1.")
            S.uploads += 1
            return ok({"url": f"https://img.kwcdn.com/local-image/s109/{S.uploads}.jpg", "autoCropUrls": []})
        if t == "bg.local.goods.add":
            return self.add(b)
        if t == "bg.local.goods.stock.edit":
            g = S.goods.get(b.get("goodsId"))
            if not g:
                return fail(150011112, "The product does not exist.")
            for x in b.get("skuStockTargetList") or []:
                if x["skuId"] != g["skuId"] or not 0 <= x["stockTarget"] <= 999999:
                    return fail(150010003, "Invalid Request Parameters")
                g["stock"] = x["stockTarget"]
            return ok({})
        if t == "bg.local.goods.priceorder.change.sku.price":
            g = S.goods.get(b.get("goodsId"))
            S.price_changes += 1
            sku = b["changeSkuPriceDTOList"][0]["skuChangePriceBaseDTOList"][0]
            if not g or sku["skuId"] != g["skuId"] or sku["newSupplierPrice"]["currency"] != "EUR":
                return ok({"failedSkuList": [sku["skuId"]], "failedSkuReasonMap": {str(sku["skuId"]): "invalid sku"}})
            g["price"] = sku["newSupplierPrice"]["amount"]
            return ok({"successSkuList": [sku["skuId"]], "successPriceOrderList": [{"skuIdList": [sku["skuId"]],
                                                                                     "priceOrderSn": "PO-1"}]})
        if t == "bg.local.goods.sale.status.set":
            g = S.goods.get(b.get("goodsId"))
            if not g:
                return fail(150011112, "The product does not exist.")
            g["sub"] = 2001 if b.get("onsale") == 1 else 3002
            return ok({})
        if t == "bg.local.goods.list.query":
            ids = b.get("goodsIdList") or list(S.goods)
            return ok({"goodsList": [{"goodsId": gid, "goodsShowSubStatus": g["sub"], "price": g["price"],
                                      "currency": "EUR", "outGoodsSn": g["sku"],
                                      "skuInfoList": [{"skuId": g["skuId"], "stock": g["stock"], "skuSn": g["sku"]}]}
                                     for gid, g in S.goods.items() if gid in ids], "total": len(S.goods)})
        return fail(3000003, "type not exists")

    def add(self, b):
        S.adds += 1
        S.last_add = b
        basic, promise, skus = b.get("goodsBasic") or {}, b.get("goodsServicePromise") or {}, b.get("skuList") or []
        name = basic.get("goodsName") or ""
        if not name:
            return fail(150010009, "Enter the product name")
        if len(name) > 500 or any(c in name for c in "~!*$?_{}#<>|;^®©™"):
            return fail(150010011, "Only use letters, numbers and common punctuation for product name")
        if basic.get("catId") != BOOKS_CAT:
            return fail(150010042, "Category unavailable")
        if promise.get("costTemplateId") != "LFT-15630053225717890848":
            return fail(150010031, "Shipping template not found")
        if promise.get("fulfillmentType") != 1 or not 1 <= int(promise.get("shipmentLimitDay") or 0) <= 30:
            return fail(150010018, "Invalid handling time")
        props = {p.get("templatePid"): p for p in (b.get("goodsProperty") or {}).get("goodsProperties") or []}
        for req in [x for x in TEMPLATE["templateInfo"]["goodsProperties"] if x["required"]]:
            got = props.get(req["templatePid"])
            if not got or (req["values"] and got.get("vid") not in [v["vid"] for v in req["values"]]) or \
                    (not req["values"] and not str(got.get("value") or "").strip()):
                return fail(150011010, f"The keyword attribute [{req['name']}] is required, please fill in accurately "
                                       "and appropriately")
        if not skus:
            return fail(150010016, "Add at least one SKU")
        s = skus[0]
        if s.get("specIdList") != [SPEC_ID]:
            return fail(150010017, "Some SKU specifications are empty")
        price = (s.get("price") or {}).get("basePrice") or {}
        if price.get("currency") != "EUR" or not isinstance(price.get("amount"), str) or \
                len(price["amount"].split(".")[-1]) > 2 or float(price["amount"]) < 0.01:
            return fail(150011018, "Price currency EUR can have at most 2 decimal points.")
        if not 0 <= int(s.get("quantity", -1)) <= 999999:
            return fail(150010028, "Quantity must be between 0 and 1000000")
        if s.get("weightUnit") != "g" or float(s.get("weight") or 0) <= 0 or s.get("volumeUnit") != "cm":
            return fail(150010026, "Weight must be greater than 0")
        imgs = s.get("images") or []
        if not 1 <= len(imgs) <= 10 or not all(i.startswith("https://img.kwcdn.com/") for i in imgs):
            return fail(150011106, "The image URL is unsupported or inaccessible.")
        if s.get("externalProductType") != 3 or len(str(s.get("externalProductId") or "")) != 13:
            return fail(150011019, "The input externalProductId is incorrect, please modify it.")
        if any(g["sku"] == s.get("outSkuSn") for g in S.goods.values()):
            return fail(150010090, "SKU duplicated")
        gid, sku_id = 604890541711850 + len(S.goods), 77600126949823 + len(S.goods)
        S.goods[gid] = {"sku": s.get("outSkuSn"), "skuId": sku_id, "price": price["amount"], "stock": s["quantity"],
                        "sub": 4001, "name": name}
        return ok({"goodsId": gid, "productType": 1, "skuInfoList": [{"skuId": sku_id, "outSkuSn": s.get("outSkuSn"),
                                                                      "specList": [{"specId": SPEC_ID, "parentSpecId": 1001}]}]})


RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None, allowed=None, props=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TEMU_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = "import temu_push as m\nfrom temu_push import main"
    if allowed is not None:
        code += f"\nm.ALLOWED_CONDITIONS.clear(); m.ALLOWED_CONDITIONS.update({sorted(allowed)!r})"
    if props is not None:
        code += f"\nm.PROPERTIES[:] = {props!r}"
    code += f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def by_sku(sku):
    return next(((gid, g) for gid, g in S.goods.items() if g["sku"] == sku), (None, {}))


def test_temu():
    print("\n=== temu_push.py (Temu Partner API mock) ===")
    sys.path.insert(0, str(ROOT))
    import temu_push as tm
    doc = {"access_token": "2nifvmpyymvypwmcms5ct4uqqudrwgpmzbcnmkt1jzjkuaf3x56iixym",
           "app_key": "f9d5cc9313893a20d5aa85c654e8f503", "data_type": "JSON",
           "sendRequestList": [{"orderSendInfoList": [{"quantity": 1, "orderSn": "211-21905473070712792",
                                                       "parentOrderSn": "PO-211-21905452099192792",
                                                       "goodsId": 601099548666279, "skuId": 17592352673534}],
                                "carrierId": "699272611", "trackingNumber": "270324232756"}],
           "sendType": 0, "timestamp": 1711009072, "type": "bg.logistics.shipment.confirm"}
    check("signature matches the worked example in Temu's docs",
          tm.sign("c7e0a1a63542be4de3cb5488f9fba8149e8fc290", doc) == "4CCF219942D4180C6DDA3CE36C1B838F")
    check("product names are cleaned of characters Temu rejects",
          tm.clean_name("Libro ~ especial! <edición> ® #1") == "Libro especial edición 1", tm.clean_name("Libro ~ especial! <edición> ® #1"))

    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, dict(ENV, TEMU_APP_SECRET="wrong"))
        check("wrong app secret -> signature error with a hint", rc != 0 and "Check TEMU_APP_SECRET" in out, out[-200:])

        rc, out = run(["check"], tmp, dict(ENV, TEMU_ACCESS_TOKEN="expired"))
        check("bad access token -> 'authorize again' message", rc != 0 and "Authorize the app again" in out, out[-200:])

        rc, out = run(["check"], tmp, ENV)
        check("check: token, shop, permissions and shipping template listed", rc == 0 and "634418" in out
              and "LFT-15630053225717890848" in out and "MISSING" not in out and "CATEGORY_ID is not set" in out,
              out[-400:])

        rc, out = run(["categories", "--find", "book"], tmp, ENV)
        rc2, out2 = run(["categories", "--parent", "3000"], tmp, ENV)
        check("categories: search and browse to a sellable leaf category", "3000" in out and "Toys" not in out
              and f"{BOOKS_CAT}" in out2 and "(leaf)" in out2 and "[not sellable] Comics" in out2, out + out2)

        rc, out = run(["recommend", "Dragona. Libro para bebes con rimas"], tmp, ENV)
        check("recommend: Temu's suggested category shown", f"suggests category {BOOKS_CAT}" in out, out)

        rc, out = run(["attributes", str(BOOKS_CAT)], tmp, ENV)
        check("attributes: required ones, ready-to-paste values, ISBN auto-filled, custom variation",
              "REQUIRED  Idioma" in out and '"vid": 381' in out and "filled with the ISBN automatically" in out
              and "custom values allowed under Estilo" in out, out)

        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = json.loads((Path(tmp) / "temu_products.json").read_text(encoding="utf-8"))
        g = prev[0] if prev else {}
        sku = (g.get("skuList") or [{}])[0]
        check("dry run without credentials/category builds the payload with a placeholder, sends nothing",
              rc == 0 and len(prev) == 1 and S.adds == 0 and "placeholder" in out
              and g["goodsBasic"]["goodsName"].startswith("Dragona") and g["goodsBasic"]["outGoodsSn"] == "T-NEW"
              and sku.get("price") == {"listPriceType": 1, "basePrice": {"amount": "8.50", "currency": "EUR"}}
              and sku.get("externalProductType") == 3 and sku.get("externalProductId") == "9788409591923"
              and sku.get("weightUnit") == "g" and "ISBN: 9788409591923" in g.get("goodsDesc", ""), out[-400:])
        check("only new books with images are pushed; others skipped with reasons",
              "not sold on Temu" in out and "sold out at source" in out and "no ISBN/EAN" in out, out[-500:])

        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        check("real push without CATEGORY_ID stops with instructions", rc != 0 and "CATEGORY_ID is not set" in out
              and S.adds == 0, out[-300:])

        rc, out = run(["push", pf, "--yes", "--category", str(BOOKS_CAT)], tmp, ENV)
        check("missing required attribute detected before sending", rc != 0 and "needs: Idioma" in out
              and S.adds == 0, out[-300:])

        S.throttle_once, S.bad_sigs = True, 0
        rc, out = run(["push", pf, "--yes", "--category", str(BOOKS_CAT)], tmp, ENV, allowed=ALLOW_ALL,
                      props=[LANGUAGE_PROP])
        state = json.loads((Path(tmp) / "temu_posted.json").read_text(encoding="utf-8"))
        check("push creates products (rate limit retried, signatures valid)", set(state) == {"T-NEW", "T-USED"}
              and state["T-NEW"]["goods_id"] and state["T-NEW"]["sku_id"] and "retrying" in out
              and S.bad_sigs == 0, out[-600:])
        props = {p["templatePid"]: p for p in S.last_add["goodsProperty"]["goodsProperties"]}
        check("ISBN attribute filled automatically, PROPERTIES and generated variation sent",
              props.get(9001, {}).get("value") == "9788410413290" and props.get(9002, {}).get("vid") == 381
              and S.last_add["skuList"][0]["specIdList"] == [SPEC_ID] and S.spec_calls == 1, str(S.last_add)[:500])
        check("photos uploaded to Temu first; Temu's image rejection shown, product not recorded",
              S.uploads >= 2 and all(i.startswith("https://img.kwcdn.com/") for i in S.last_add["skuList"][0]["images"])
              and "! T-REFURB: 150011066" in out and "T-REFURB" not in state, out[-500:])

        uploads, adds = S.uploads, S.adds
        rc, out = run(["push", pf, "--yes", "--category", str(BOOKS_CAT)], tmp, ENV, allowed=ALLOW_ALL,
                      props=[LANGUAGE_PROP])
        check("re-run: nothing duplicated, variation id and photos taken from the cache",
              S.adds == adds + 0 and S.uploads == uploads and S.spec_calls == 1 and "already pushed" in out, out[-400:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        gid, g = by_sku("T-NEW")
        check("--resend: stock set to 5, new price 8.08 (half-up) sent for Temu's price review",
              g.get("stock") == 5 and g.get("price") == "8.08" and "~ T-NEW: 8.08 EUR x5" in out, str(g))

        changes = S.price_changes
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        check("--resend with nothing changed sends no price change", S.price_changes == changes, out[-200:])

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-NEW":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(sold_file), "--yes", "--offline-sold-out"], tmp, ENV)
        check("--offline-sold-out takes a sold-out product off sale", by_sku("T-NEW")[1]["sub"] == 3002
              and "taken off sale" in out, out[-300:])

        by_sku("T-USED")[1]["sub"] = 2001
        rc, out = run(["status"], tmp, ENV)
        check("status: Temu's review / sale status per product", rc == 0 and "T-NEW: off sale" in out
              and "T-USED: ON SALE" in out and "stock=" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend"], tmp, ENV)
        check("back in stock + --resend puts it on sale again", by_sku("T-NEW")[1]["sub"] == 2001
              and "T-NEW: back on sale" in out, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_temu()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
