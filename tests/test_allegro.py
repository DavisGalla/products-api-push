#!/usr/bin/env python3
"""
End-to-end test for allegro_push.py, run against a LOCAL MOCK of the Allegro
REST API and its OAuth server. The mock implements the device-code flow
(pending -> token) and single-use refresh tokens, checks the bearer token and
Allegro's media type on every call, and serves: /me, shipping rates, return
policies, implied warranties, responsible producers, catalog search by GTIN,
product-offer creation (201, and 202 + operation polling) with the documented
rules, PATCH, publication commands (END / ACTIVATE) and the seller's offer
list. Nothing is sent to Allegro, and your real allegro_posted.json /
allegro_token.json are not touched (each test runs in a temporary folder).

    python tests/test_allegro.py
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
CLIENT, SECRET = "test-allegro-client", "test-allegro-secret"
ENV = {"ALLEGRO_API_URL": BASE, "ALLEGRO_AUTH_URL": BASE, "ALLEGRO_CLIENT_ID": CLIENT, "ALLEGRO_CLIENT_SECRET": SECRET}
MEDIA = "application/vnd.allegro.public.v1+json"
CATALOG = {"9788409591923": "prod-dragona", "9788410413290": "prod-coleccion"}     # T-REFURB's ISBN missing
HANDLING = {"PT0S", "PT24H", "P2D", "P3D", "P4D", "P5D", "P7D", "P10D", "P14D", "P21D", "P30D", "P60D"}


class S:
    @classmethod
    def reset(cls):
        cls.tokens, cls.refresh = {}, set()
        cls.device_polls = cls.creates = cls.patches = cls.refreshes = 0
        cls.offers = {}
        cls.operations = {}
        cls.commands = {}
        cls.producers = [{"id": "prod-555", "name": "Editorial Ejemplo"}]
        cls.throttle_once = False
        cls.async_next = False


S.reset()


def err(code, message, path=None):
    return {"errors": [{"code": code, "message": message, "userMessage": message, "path": path, "details": None}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, headers=None, ctype=MEDIA):
        body = json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b""
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

    def do_PUT(self):
        self.handle_any("PUT")

    def handle_any(self, method):
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            if u.path.startswith("/auth/oauth/"):
                return self.oauth(u.path, q, {k: v[0] for k, v in parse_qs(raw).items()})
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or S.tokens.get(auth[7:], 0) < time.time():
                return self.send(401, {"error": "invalid_token", "error_description": "Access token expired"}, ctype="application/json")
            if self.headers.get("Accept") != MEDIA or (raw and self.headers.get("Content-Type") != MEDIA):
                return self.send(406, err("NotAcceptable", "Use application/vnd.allegro.public.v1+json"))
            if S.throttle_once:
                S.throttle_once = False
                return self.send(429, err("TooManyRequests", "Too many requests"), {"Retry-After": "0"})
            return self.api(method, u.path, q, json.loads(raw) if raw else None)
        except Exception as e:
            self.send(500, err("InternalError", repr(e)))

    def oauth(self, path, q, form):
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode():
            return self.send(401, {"error": "invalid_client", "error_description": "Bad client credentials"}, ctype="application/json")
        if path == "/auth/oauth/device":
            return self.send(200, {"user_code": "cbt3zdu4g", "device_code": "dev-1", "expires_in": "3600", "interval": "0",
                                   "verification_uri": "https://allegro.pl/skojarz-aplikacje",
                                   "verification_uri_complete": "https://allegro.pl/skojarz-aplikacje?code=cbt3zdu4g"},
                             ctype="application/json")
        grant = form.get("grant_type")
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            if form.get("device_code") != "dev-1":
                return self.send(400, {"error": "Invalid device code"}, ctype="application/json")
            S.device_polls += 1
            if S.device_polls < 2:
                return self.send(400, {"error": "authorization_pending"}, ctype="application/json")
        elif grant == "refresh_token":
            if form.get("refresh_token") not in S.refresh:
                return self.send(400, {"error": "invalid_grant", "error_description": "Invalid refresh token"}, ctype="application/json")
            S.refresh.discard(form["refresh_token"])          # single use
            S.refreshes += 1
        else:
            return self.send(400, {"error": "unsupported_grant_type"}, ctype="application/json")
        n = len(S.tokens) + 1
        S.tokens[f"acc-{n}"] = time.time() + 43199
        S.refresh.add(f"ref-{n}")
        self.send(200, {"access_token": f"acc-{n}", "token_type": "bearer", "refresh_token": f"ref-{n}",
                        "expires_in": 43199, "scope": "allegro_api", "jti": str(uuid.uuid4())}, ctype="application/json")

    def api(self, m, p, q, b):
        if m == "GET" and p == "/me":
            return self.send(200, {"id": "48118888", "login": "LibrosTestES", "company": {"name": "Libros Test SL"},
                                   "baseMarketplace": {"id": "allegro-pl"}})
        if m == "GET" and p == "/sale/shipping-rates":
            return self.send(200, {"shippingRates": [{"id": "ship-1", "name": "Envío desde España"}]})
        if m == "GET" and p == "/after-sales-service-conditions/return-policies":
            return self.send(200, {"returnPolicies": [{"id": "ret-1", "name": "14 dni"}], "count": 1})
        if m == "GET" and p == "/after-sales-service-conditions/implied-warranties":
            return self.send(200, {"impliedWarranties": [{"id": "war-1", "name": "Rękojmia 2 lata"}], "count": 1})
        if m == "GET" and p == "/sale/responsible-producers":
            return self.send(200, {"responsibleProducers": S.producers, "count": len(S.producers), "totalCount": len(S.producers)})
        if m == "GET" and p == "/sale/products":
            phrase = (q.get("phrase") or [""])[0]
            if (q.get("mode") or [""])[0] != "GTIN":
                return self.send(422, err("ValidationError", "mode GTIN expected in this test"))
            pid = CATALOG.get(phrase)
            return self.send(200, {"products": [{"id": pid, "name": "Libro " + phrase, "category": {"id": "66773"}}] if pid else []})
        if m == "POST" and p == "/sale/product-offers":
            return self.create(b)
        if p.startswith("/sale/product-offers/"):
            parts = p.strip("/").split("/")
            oid = parts[2]
            if oid not in S.offers:
                return self.send(404, err("NotFound", "Offer not found"))
            if len(parts) == 5 and parts[3] == "operations":
                op = S.operations[parts[4]]
                op["polls"] += 1
                if op["polls"] < 2:
                    return self.send(202, {"offer": {"id": oid}, "operation": {"id": parts[4], "status": "IN_PROGRESS"}})
                return self.send(200, {"offer": {"id": oid}, "operation": {"id": parts[4], "status": "COMPLETED"}})
            o = S.offers[oid]
            if m == "GET":
                return self.send(200, o)
            if m == "PATCH":
                S.patches += 1
                if "sellingMode" in b:
                    price = b["sellingMode"]["price"]
                    if price.get("currency") != "PLN" or len(price.get("amount", "").split(".")[-1]) != 2:
                        return self.send(422, err("ValidationError", "Invalid price", "sellingMode.price"))
                    o["sellingMode"]["price"] = price
                if "stock" in b:
                    o["stock"]["available"] = b["stock"]["available"]
                return self.send(200, o)
        if p.startswith("/sale/offer-publication-commands/"):
            cid = p.rsplit("/", 1)[1]
            if m == "PUT":
                action = b["publication"]["action"]
                ids = [x["id"] for c in b["offerCriteria"] for x in c["offers"]]
                ok = [i for i in ids if i in S.offers]
                for i in ok:
                    S.offers[i]["publication"]["status"] = "ACTIVE" if action == "ACTIVATE" else "ENDED"
                S.commands[cid] = {"id": cid, "taskCount": {"total": len(ids), "success": len(ok), "failed": len(ids) - len(ok)}}
                return self.send(201, S.commands[cid])
            return self.send(200, S.commands[cid])
        if m == "GET" and p == "/sale/offers":
            ext = set(q.get("external.id") or [])
            return self.send(200, {"offers": [{"id": o["id"], "external": o["external"], "publication": o["publication"],
                                               "sellingMode": o["sellingMode"], "stock": dict(o["stock"], sold=0)}
                                              for o in S.offers.values() if not ext or o["external"]["id"] in ext],
                                   "count": len(S.offers), "totalCount": len(S.offers)})
        return self.send(404, err("NotFound", f"no mock for {m} {p}"))

    def create(self, b):
        S.creates += 1
        ps = (b.get("productSet") or [{}])[0]
        prod = ps.get("product") or {}
        problems = []
        if prod.get("idType") != "GTIN" or prod.get("id") not in CATALOG:
            return self.send(422, err("ProductNotFound", f"Product with GTIN {prod.get('id')} was not found", "productSet[0].product"))
        if (ps.get("safetyInformation") or {}).get("type") != "TEXT" or not ps["safetyInformation"].get("description"):
            problems.append(("productSet[0].safetyInformation", "Safety information is required (GPSR)"))
        if (ps.get("responsibleProducer") or {}).get("type") != "ID" or ps["responsibleProducer"].get("id") != "prod-555":
            problems.append(("productSet[0].responsibleProducer", "Responsible producer is required (GPSR)"))
        params = {x.get("id"): x for x in b.get("parameters") or []}
        if (params.get("11323") or {}).get("valuesIds", [None])[0] not in ("11323_1", "11323_2"):
            problems.append(("parameters", "Parameter 'Stan' is required"))
        sm = b.get("sellingMode") or {}
        price = sm.get("price") or {}
        if sm.get("format") != "BUY_NOW" or price.get("currency") != "PLN" or not isinstance(price.get("amount"), str) \
                or len(price["amount"].split(".")[-1]) != 2 or float(price["amount"]) <= 0:
            problems.append(("sellingMode.price", "Invalid price"))
        st = b.get("stock") or {}
        if st.get("unit") != "UNIT" or not isinstance(st.get("available"), int) or st["available"] < 1:
            problems.append(("stock", "Stock must be at least 1"))
        dl = b.get("delivery") or {}
        if dl.get("handlingTime") not in HANDLING or (dl.get("shippingRates") or {}).get("id") != "ship-1":
            problems.append(("delivery", "Invalid handling time or shipping rates"))
        asv = b.get("afterSalesServices") or {}
        if (asv.get("returnPolicy") or {}).get("id") != "ret-1" or (asv.get("impliedWarranty") or {}).get("id") != "war-1":
            problems.append(("afterSalesServices", "Return policy and implied warranty are required for business sellers"))
        if problems:
            return self.send(422, {"errors": [{"code": "ValidationError", "message": msg, "userMessage": msg, "path": path}
                                              for path, msg in problems]})
        oid = str(7700000000 + len(S.offers) + 1)
        S.offers[oid] = {"id": oid, "productSet": b["productSet"], "sellingMode": sm, "stock": dict(st),
                         "external": b.get("external") or {}, "publication": {"status": "ACTIVE"},
                         "validation": {"errors": []}}
        if S.async_next:
            S.async_next = False
            op = str(uuid.uuid4())
            S.operations[op] = {"polls": 0}
            return self.send(202, {"id": oid}, {"Location": f"{BASE}/sale/product-offers/{oid}/operations/{op}"})
        return self.send(201, S.offers[oid])


RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None, conds=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALLEGRO_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = "import allegro_push as m\nfrom allegro_push import main"
    if conds is not None:
        code += f"\nm.CONDITION_VALUES.update({conds!r})"
    code += f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def by_ext(sku):
    return next((o for o in S.offers.values() if o["external"].get("id") == sku), {})


def test_allegro():
    print("\n=== allegro_push.py (Allegro REST API mock) ===")
    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, ENV)
        check("no authorization yet -> clear message", rc != 0 and "Run `python allegro_push.py auth` first" in out, out[-200:])

        rc, out = run(["auth"], tmp, ENV)
        tok = json.loads((Path(tmp) / "allegro_token.json").read_text(encoding="utf-8"))
        check("auth: device code shown (link + readable code), polled until confirmed, tokens saved not printed",
              rc == 0 and "skojarz-aplikacje?code=cbt3zdu4g" in out and "cbt 3zd u4g" in out and S.device_polls == 2
              and tok["access_token"] == "acc-1" and "acc-1" not in out and "ref-1" not in out, out[-400:])

        rc, out = run(["check"], tmp, ENV)
        check("check: account, shipping rates, return policy, implied warranty and GPSR producer found",
              rc == 0 and "LibrosTestES" in out and "ship-1" in out and "ret-1" in out and "war-1" in out
              and "prod-555" in out, out[-500:])

        rc, out = run(["lookup", "978-84-09-59192-3"], tmp, ENV)
        rc2, out2 = run(["lookup", "9780000000002"], tmp, ENV)
        check("lookup: known and unknown ISBN", "prod-dragona" in out and "NOT in the Allegro catalog" in out2, out + out2)

        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = json.loads((Path(tmp) / "allegro_offers.json").read_text(encoding="utf-8"))
        new = next((x for x in prev if x["external"]["id"] == "T-NEW"), {})
        used = next((x for x in prev if x["external"]["id"] == "T-USED"), {})
        check("dry run without credentials builds the offers (price in PLN), sends nothing",
              rc == 0 and S.creates == 0 and "account settings skipped" in out
              and new.get("productSet", [{}])[0].get("product") == {"id": "9788409591923", "idType": "GTIN"}
              and new.get("sellingMode") == {"format": "BUY_NOW", "price": {"amount": "36.13", "currency": "PLN"}}
              and new.get("stock") == {"available": 1, "unit": "UNIT"} and new.get("publication") == {"status": "ACTIVE"}
              and new.get("parameters") == [{"id": "11323", "valuesIds": ["11323_1"]}]
              and new["productSet"][0]["safetyInformation"]["type"] == "TEXT", out[-400:])
        check("used book: condition 'Używany' (11323_2), ISBN normalised",
              used.get("parameters") == [{"id": "11323", "valuesIds": ["11323_2"]}]
              and used["productSet"][0]["product"]["id"] == "9788410413290", str(used)[:300])
        check("refurbished / no-ISBN / sold-out products skipped with reasons",
              "no Allegro condition for 'RefurbishedCondition'" in out and "sold out at source" in out
              and "no ISBN/EAN" in out, out[-500:])

        rc, out = run(["push", pf, "--dry-run"], tmp, ENV)
        prev = json.loads((Path(tmp) / "allegro_offers.json").read_text(encoding="utf-8"))
        check("dry run with credentials: account ids filled in, catalog checked",
              S.creates == 0 and prev and prev[0]["delivery"] == {"handlingTime": "P2D", "shippingRates": {"id": "ship-1"}}
              and prev[0]["afterSalesServices"] == {"returnPolicy": {"id": "ret-1"}, "impliedWarranty": {"id": "war-1"}}
              and prev[0]["productSet"][0]["responsibleProducer"] == {"type": "ID", "id": "prod-555"}, str(prev[:1])[:500])

        S.throttle_once, S.async_next = True, False
        rc, out = run(["push", pf, "--yes", "--quantity", "2", "--limit", "1"], tmp, ENV)
        S.async_next = True
        rc2, out2 = run(["push", pf, "--yes", "--quantity", "2"], tmp, ENV)
        state = json.loads((Path(tmp) / "allegro_posted.json").read_text(encoding="utf-8"))
        check("push creates offers: 201 directly and 202 + operation polling (429 retried)",
              set(state) == {"T-NEW", "T-USED"} and all(v["status"] == "ACTIVE" for v in state.values())
              and "retrying" in out and by_ext("T-USED").get("stock", {}).get("available") == 2, out[-300:] + out2[-300:])
        check("allegro_posted.json records offer id, PLN price and stock", state["T-NEW"]["offer_id"] in S.offers
              and state["T-NEW"]["price"] == "36.13" and state["T-NEW"]["stock"] == 2, str(state))

        creates = S.creates
        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        check("re-run sends nothing new", S.creates == creates and "already pushed" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        o = by_ext("T-NEW")
        check("--resend PATCHes price 34.32 PLN (8.50 x 0.95 x 4.25, half-up) and stock 5",
              o["sellingMode"]["price"]["amount"] == "34.32" and o["stock"]["available"] == 5 and "~ T-NEW: 34.32 PLN x5" in out,
              str(o.get("sellingMode")))

        patches = S.patches
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        check("--resend with nothing changed sends no PATCH", S.patches == patches, out[-300:])

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-NEW":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(sold_file), "--yes", "--end-sold-out"], tmp, ENV)
        check("--end-sold-out ends a sold-out offer (publication command)",
              by_ext("T-NEW")["publication"]["status"] == "ENDED" and "ended 1 sold-out offer" in out, out[-300:])

        rc, out = run(["status"], tmp, ENV)
        check("status: active / ended with PLN price and stock", rc == 0 and "T-NEW: ENDED" in out
              and "T-USED: ACTIVE" in out and "PLN" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend"], tmp, ENV)
        check("back in stock + --resend reactivates it", by_ext("T-NEW")["publication"]["status"] == "ACTIVE"
              and "reactivated 1 offer" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--no-match-check"], tmp, ENV, conds={"RefurbishedCondition": "11323_2"})
        state = json.loads((Path(tmp) / "allegro_posted.json").read_text(encoding="utf-8"))
        check("Allegro's validation error shown with its path, offer not recorded",
              "! T-REFURB: 422 Product with GTIN 9780000000002 was not found (productSet[0].product)" in out
              and "T-REFURB" not in state, out[-400:])

        tf = Path(tmp) / "allegro_token.json"
        t = json.loads(tf.read_text(encoding="utf-8"))
        t["expires_at"] = int(time.time()) - 5
        tf.write_text(json.dumps(t), encoding="utf-8")
        before = S.refreshes
        rc, out = run(["status"], tmp, ENV)
        t2 = json.loads(tf.read_text(encoding="utf-8"))
        check("expired token refreshed, and the NEW single-use refresh token saved",
              rc == 0 and S.refreshes == before + 1 and t2["refresh_token"] != t["refresh_token"]
              and t["refresh_token"] not in S.refresh, out[-200:])

        t2["expires_at"], t2["refresh_token"] = 0, t["refresh_token"]          # an already-used refresh token
        tf.write_text(json.dumps(t2), encoding="utf-8")
        rc, out = run(["status"], tmp, ENV)
        check("used/expired refresh token -> clear 'authorize again' message", rc != 0 and "Authorize again" in out, out[-200:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        S.producers = []
        S.refresh.add("ref-bw")
        rc, out = run(["push", pf, "--yes", "--limit", "1"], tmp, dict(ENV, ALLEGRO_REFRESH_TOKEN="ref-bw"))
        check("ALLEGRO_REFRESH_TOKEN (Bitwarden) works without a token file",
              S.refreshes == 1 and (Path(tmp) / "allegro_token.json").exists(), out[-300:])
        check("no responsible producer (GPSR) in the account -> stops before sending",
              rc != 0 and "responsible producer (GPSR)" in out and S.creates == 0, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_allegro()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
