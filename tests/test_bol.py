#!/usr/bin/env python3
"""
End-to-end test for bol_push.py, run against a LOCAL MOCK of the bol.com
Retailer API (Offers API v11). The mock serves the client-credentials login
(Basic auth), checks the bearer token and bol's versioned media types on
every call, and implements: product-ids lookup (catalog check), economic
operators, create offer (with the v11 schema rules and 409 on duplicates),
list/get/patch offers and not-for-sale reasons. Nothing is sent to bol.com,
and your real bol_posted.json is not touched (each test runs in a temporary
folder).

    python tests/test_bol.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
CLIENT, SECRET = "test-bol-client", "test-bol-secret"
ENV = {"BOL_API_URL": BASE, "BOL_LOGIN_URL": BASE, "BOL_CLIENT_ID": CLIENT, "BOL_CLIENT_SECRET": SECRET}
V11, V10, EO = "application/vnd.retailer.v11+json", "application/vnd.retailer.v10+json", "application/vnd.economic-operator.v1+json"
CATALOG = {"9788409591923": "9200000045327288", "9788410413290": "9300000012345678"}   # T-REFURB's ISBN missing
STATES = {"AS_NEW", "GOOD", "REASONABLE", "MODERATE"}


class S:
    @classmethod
    def reset(cls):
        cls.tokens = set()
        cls.token_requests = cls.creates = cls.patches = 0
        cls.offers = {}
        cls.operators = [{"id": "0c6573a2-a80c-48b7-a03e-d5939f1173fa", "name": "Libros Test SL", "status": "ACTIVE"}]
        cls.throttle_once = cls.expire_next = False


S.reset()


def problem(status, detail, violations=None):
    return {"type": "https://api.bol.com/problems", "title": "Error validating request", "status": status,
            "detail": detail, "violations": violations or []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, ctype=V11, headers=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def handle_any(self, method):
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            if u.path == "/token":
                return self.token(raw)
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in S.tokens:
                return self.send(401, problem(401, "Unauthorized request"), "application/json")
            if S.expire_next:
                S.expire_next = False
                S.tokens.discard(auth[7:])
                return self.send(401, problem(401, "JWT expired"), "application/json")
            p = u.path
            if p.startswith("/retailer/products/") and p.endswith("/product-ids"):
                if self.headers.get("Accept") != V10:
                    return self.send(406, problem(406, "Not acceptable"), "application/json")
                ean = p.split("/")[3]
                if ean in CATALOG:
                    return self.send(200, {"bolProductId": CATALOG[ean], "eanList": [{"ean": ean}]}, V10)
                return self.send(404, problem(404, "Product not found"), V10)
            if p == "/retailer/economic-operators":
                if self.headers.get("Accept") != EO:
                    return self.send(406, problem(406, "Not acceptable"), "application/json")
                return self.send(200, {"operators": S.operators, "page": {"pageNumber": 1}}, EO)
            if not p.startswith("/retailer/offers"):
                return self.send(404, problem(404, "not found"), "application/json")
            if self.headers.get("Accept") != V11 or (raw and self.headers.get("Content-Type") != V11):
                return self.send(415, problem(415, "Unsupported media type"), "application/json")
            if S.throttle_once:
                S.throttle_once = False
                return self.send(429, problem(429, "Too many requests"), V11, {"Retry-After": "0"})
            parts = p.strip("/").split("/")
            if method == "POST" and p == "/retailer/offers":
                return self.create(json.loads(raw))
            if method == "GET" and p == "/retailer/offers":
                eans = set(q.get("eans", []))
                return self.send(200, {"offers": [self.view(o) for o in S.offers.values() if not eans or o["ean"] in eans],
                                       "page": {"pageSize": 100, "nextCursor": None}})
            oid = parts[2] if len(parts) > 2 else None
            if oid not in S.offers:
                return self.send(404, problem(404, "Offer not found"))
            o = S.offers[oid]
            if method == "GET" and len(parts) == 3:
                return self.send(200, self.view(o))
            if method == "GET" and parts[-1] == "not-for-sale-reasons":
                reasons = []
                if o["stock"]["amount"] <= 0:
                    reasons.append({"category": "STOCK", "code": "4003-3",
                                    "description": "Out of stock or remaining stock is reserved for pending orders."})
                if not o.get("economicOperatorId"):
                    reasons.append({"category": "ASSORTMENT_POLICY", "code": "3001-99",
                                    "description": "Offline due to incomplete economic operator"})
                return self.send(200, {"offerId": oid, "countries": [{"countryCode": c["countryCode"], "reasons": reasons}
                                                                      for c in o["countryAvailabilities"]]})
            if method == "PATCH" and len(parts) == 3:
                S.patches += 1
                b = json.loads(raw)
                errs = self.validate(b, partial=True)
                if errs:
                    return self.send(400, problem(400, "Error validating request", errs))
                for k in ("pricing", "stock", "onHoldByRetailer", "reference", "fulfilment", "countryAvailabilities"):
                    if k in b:
                        o[k] = b[k]
                return self.send(204)
            return self.send(405, problem(405, "Method not allowed"))
        except Exception as e:
            self.send(500, problem(500, repr(e)), "application/json")

    def token(self, raw):
        S.token_requests += 1
        auth = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
        if auth != expected or parse_qs(raw).get("grant_type") != ["client_credentials"]:
            return self.send(401, {"error": "invalid_client"}, "application/json")
        tok = "jwt-" + uuid.uuid4().hex
        S.tokens.add(tok)
        self.send(200, {"access_token": tok, "token_type": "Bearer", "expires_in": 299, "scope": "retailer"},
                  "application/json")

    @staticmethod
    def validate(b, partial=False):
        errs = []
        if not partial or "pricing" in b:
            bp = (b.get("pricing") or {}).get("bundlePrices") or []
            if not bp or bp[0].get("quantity") != 1 or not isinstance(bp[0].get("unitPrice"), (int, float)) \
                    or not 1 <= bp[0]["unitPrice"] <= 9999 or round(bp[0]["unitPrice"], 2) != bp[0]["unitPrice"]:
                errs.append({"name": "pricing.bundlePrices", "reason": "unitPrice must be between 1 and 9999 with 2 decimals"})
        if "stock" in b:
            st = b["stock"]
            if not isinstance(st.get("amount"), int) or st["amount"] < 0 or not isinstance(st.get("managedByRetailer"), bool):
                errs.append({"name": "stock", "reason": "amount >= 0 and managedByRetailer required"})
        if not partial:
            c = b.get("condition") or {}
            if c.get("category") not in ("NEW", "SECONDHAND", "REFURBISHED") or \
                    (c["category"] == "SECONDHAND" and c.get("state") not in STATES):
                errs.append({"name": "condition", "reason": "invalid condition"})
            if not (b.get("countryAvailabilities")) or any(x.get("countryCode") not in ("NL", "BE") for x in b["countryAvailabilities"]):
                errs.append({"name": "countryAvailabilities", "reason": "allowed values: NL, BE"})
            f = b.get("fulfilment") or {}
            if f.get("method") not in ("FBR", "FBB") or f.get("schedule") not in (None, "BOL_DELIVERY_PROMISE", "MY_DELIVERY_PROMISE", "SHIPPING_VIA_BOL"):
                errs.append({"name": "fulfilment", "reason": "invalid fulfilment"})
            dp = f.get("deliveryPromise")
            if dp and not 0 <= dp.get("minimumDaysToCustomer", -1) <= dp.get("maximumDaysToCustomer", -1):
                errs.append({"name": "fulfilment.deliveryPromise", "reason": "minimum must be <= maximum"})
            if len(b.get("reference") or "") > 100 or not str(b.get("ean", "")).isdigit():
                errs.append({"name": "ean/reference", "reason": "invalid"})
        return errs

    def create(self, b):
        S.creates += 1
        errs = self.validate(b)
        if errs:
            return self.send(400, problem(400, "Error validating request", errs))
        if b["ean"] not in CATALOG:
            return self.send(400, problem(400, "Product not found for EAN", [{"name": "ean", "reason": f"EAN {b['ean']} is unknown"}]))
        if any(o["ean"] == b["ean"] and o["condition"] == b["condition"] for o in S.offers.values()):
            return self.send(409, problem(409, "An offer with this EAN and condition already exists"))
        oid = str(uuid.uuid4())
        S.offers[oid] = dict(b, offerId=oid)
        return self.send(201, {"offerId": oid})

    @staticmethod
    def view(o):
        selling = o["stock"]["amount"] > 0 and not o.get("onHoldByRetailer") and bool(o.get("economicOperatorId"))
        return {"offerId": o["offerId"], "ean": o["ean"], "reference": o.get("reference"),
                "lastModifiedDateTime": "2026-11-06T10:00:00+01:00", "onHoldByRetailer": o.get("onHoldByRetailer", False),
                "economicOperatorId": o.get("economicOperatorId"), "condition": o["condition"], "pricing": o["pricing"],
                "fulfilment": o["fulfilment"], "stock": dict(o["stock"], correctedStock=o["stock"]["amount"]),
                "countryAvailabilities": [{"countryCode": c["countryCode"], "forSale": selling} for c in o["countryAvailabilities"]],
                "product": {"bolProductId": CATALOG.get(o["ean"])}}


RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOL_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = f"from bol_push import main\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def by_ref(ref):
    return next((o for o in S.offers.values() if o.get("reference") == ref), {})


def test_bol():
    print("\n=== bol_push.py (bol.com Retailer API / Offers v11 mock) ===")
    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, dict(ENV, BOL_CLIENT_SECRET="wrong"))
        check("wrong client secret -> login failed with a hint", rc != 0 and "bol.com login failed (401)" in out, out[-200:])

        rc, out = run(["check"], tmp, ENV)
        check("check: token received (Basic auth), economic operator listed",
              rc == 0 and "Credentials accepted" in out and "Libros Test SL" in out, out[-300:])

        rc, out = run(["lookup", "978-84-09-59192-3"], tmp, ENV)
        rc2, out2 = run(["lookup", "9780000000002"], tmp, ENV)
        check("lookup: known ISBN found (v10 media type), unknown ISBN reported",
              "in the bol catalog (bol product id 9200000045327288)" in out and "NOT in the bol catalog" in out2, out + out2)

        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = json.loads((Path(tmp) / "bol_offers.json").read_text(encoding="utf-8"))
        new = next((x for x in prev if x["reference"] == "T-NEW"), {})
        used = next((x for x in prev if x["reference"] == "T-USED"), {})
        check("dry run without credentials builds the offers, sends nothing",
              rc == 0 and S.creates == 0 and "catalog check and economic operator lookup skipped" in out
              and new.get("pricing") == {"bundlePrices": [{"quantity": 1, "unitPrice": 8.5}]}
              and new.get("condition") == {"category": "NEW"}
              and new.get("countryAvailabilities") == [{"countryCode": "NL"}, {"countryCode": "BE"}]
              and new.get("fulfilment") == {"method": "FBR", "schedule": "MY_DELIVERY_PROMISE"}
              and new.get("stock") == {"amount": 1, "managedByRetailer": True}, out[-400:])
        check("used book: ISBN normalised, condition SECONDHAND / GOOD",
              used.get("ean") == "9788410413290" and used.get("condition") == {"category": "SECONDHAND", "state": "GOOD"}, str(used))
        check("no-ISBN / sold-out / no-price products skipped with reasons",
              "sold out at source" in out and "no price" in out and out.count("no ISBN/EAN") == 6, out[-500:])

        rc, out = run(["push", pf, "--dry-run", "--delivery-days", "2-4", "--countries", "BE"], tmp)
        prev = json.loads((Path(tmp) / "bol_offers.json").read_text(encoding="utf-8"))
        rc2, out2 = run(["push", pf, "--dry-run", "--countries", "DE"], tmp)
        check("--delivery-days and --countries change the offer; invalid country refused",
              prev[0]["fulfilment"] == {"method": "FBR", "schedule": "BOL_DELIVERY_PROMISE",
                                        "deliveryPromise": {"minimumDaysToCustomer": 2, "maximumDaysToCustomer": 4}}
              and prev[0]["countryAvailabilities"] == [{"countryCode": "BE"}] and rc2 != 0 and "--countries must be" in out2,
              str(prev[0]) + out2[-200:])

        rc, out = run(["push", pf, "--dry-run"], tmp, ENV)
        prev = json.loads((Path(tmp) / "bol_offers.json").read_text(encoding="utf-8"))
        check("dry run with credentials: catalog check skips unknown EAN, economic operator added",
              "9780000000002 is not in the bol catalog" in out and [x["reference"] for x in prev] == ["T-NEW", "T-USED"]
              and all(x.get("economicOperatorId") == S.operators[0]["id"] for x in prev) and S.creates == 0, out[-400:])

        S.throttle_once, before = True, S.token_requests
        rc, out = run(["push", pf, "--yes", "--quantity", "2"], tmp, ENV)
        state = json.loads((Path(tmp) / "bol_posted.json").read_text(encoding="utf-8"))
        check("push creates offers (429 + Retry-After retried, one token reused for the whole run)",
              rc == 0 and set(state) == {"T-NEW", "T-USED"} and by_ref("T-NEW").get("stock", {}).get("amount") == 2
              and "retrying" in out and S.token_requests == before + 1, out[-400:])
        check("bol_posted.json records offer id, price and stock", state["T-NEW"]["offer_id"] in S.offers
              and state["T-NEW"]["price"] == 8.5 and state["T-NEW"]["stock"] == 2, str(state))

        creates = S.creates
        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        check("re-run sends nothing new", S.creates == creates and "already pushed" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        o = by_ref("T-NEW")
        check("--resend PATCHes price 8.08 (half-up) and stock 5",
              o["pricing"]["bundlePrices"][0]["unitPrice"] == 8.08 and o["stock"]["amount"] == 5
              and "~ T-NEW: 8.08 EUR x5" in out, str(o))

        patches = S.patches
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        check("--resend with nothing changed sends no update", S.patches == patches and "T-NEW: unchanged" in out, out[-300:])

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-NEW":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(sold_file), "--yes", "--zero-sold-out"], tmp, ENV)
        check("--zero-sold-out sets stock 0 for a pushed book now sold out",
              by_ref("T-NEW")["stock"]["amount"] == 0 and "(sold out)" in out, out[-300:])

        rc, out = run(["status"], tmp, ENV)
        check("status: for sale per country, and bol's reason when not",
              rc == 0 and "T-USED: FOR SALE in NL, BE" in out and "T-NEW: not for sale" in out
              and "Out of stock" in out, out[-400:])

        S.expire_next, before = True, S.token_requests
        rc, out = run(["status"], tmp, ENV)
        check("token rejected mid-run -> new token fetched once, run continues",
              rc == 0 and S.token_requests == before + 2 and "T-USED: FOR SALE" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--no-match-check"], tmp, ENV)
        state = json.loads((Path(tmp) / "bol_posted.json").read_text(encoding="utf-8"))
        check("server rejection shown with bol's violations, not recorded",
              "! T-REFURB: 400 Product not found for EAN (ean: EAN 9780000000002 is unknown)" in out
              and "T-REFURB" not in state, out[-400:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        S.operators = []
        S.offers["existing"] = {"offerId": "existing", "ean": "9788409591923", "reference": "T-NEW",
                                "condition": {"category": "NEW"}, "pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": 9.0}]},
                                "countryAvailabilities": [{"countryCode": "NL"}], "fulfilment": {"method": "FBR"},
                                "stock": {"amount": 3, "managedByRetailer": True}}
        rc, out = run(["push", pf, "--yes", "--limit", "1"], tmp, ENV)
        state = json.loads((Path(tmp) / "bol_posted.json").read_text(encoding="utf-8"))
        check("offer that already exists on bol (409) is linked, not duplicated",
              state.get("T-NEW", {}).get("offer_id") == "existing" and "already on bol as offer existing" in out
              and len(S.offers) == 1, out[-300:])
        check("no economic operator -> warning before sending",
              "no economic operator (GPSR)" in out, out[-300:])
        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        rc, out = run(["status"], tmp, ENV)
        check("status shows 'incomplete economic operator' as the reason", "incomplete economic operator" in out, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_bol()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
