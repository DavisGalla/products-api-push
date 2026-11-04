#!/usr/bin/env python3
"""
End-to-end test for ebay_push.py, run against a LOCAL MOCK of the eBay Sell
APIs (OAuth token, policies, location, taxonomy, inventory, offer, publish).
Nothing is sent to eBay, and your real ebay_posted.json is not touched (each
test runs in a temporary folder).

    python tests/test_ebay.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765  # test_products.json image URLs point here
BASE = f"http://127.0.0.1:{PORT}"
EBAY_ENV = {"EBAY_CLIENT_ID": "cid", "EBAY_CLIENT_SECRET": "csecret", "EBAY_REFRESH_TOKEN": "rtok"}

class S:  # recorded mock state
    ebay_inventory = {}
    ebay_offers = {}
    ebay_published = {}
    ebay_publish_calls = 0

    @classmethod
    def reset(cls):
        cls.ebay_inventory, cls.ebay_offers, cls.ebay_published = {}, {}, {}
        cls.ebay_publish_calls = 0


# ---------------------------------------------------------------- mock server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data=None, ctype="application/json"):
        body = b"" if data is None else data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def do_DELETE(self): self.route("DELETE")

    def route(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        u = urlparse(self.path)
        try:
            if u.path.startswith("/img/"):
                return self.send(200, b"\xff\xd8\xff\xe0 fake jpeg", "image/jpeg")
            if u.path.startswith(("/identity/", "/sell/", "/commerce/")):
                return self.ebay(method, u, body)
            self.send(404, {"error": "no route"})
        except Exception as e:  # surface mock bugs as 500s
            self.send(500, {"error": repr(e)})

    # ---- eBay ------------------------------------------------------------
    def ebay(self, method, u, body):
        p, q = u.path, parse_qs(u.query)
        if p == "/identity/v1/oauth2/token":
            import base64
            want = "Basic " + base64.b64encode(b"cid:csecret").decode()
            if self.headers.get("Authorization") != want:
                return self.send(401, {"error": "invalid_client"})
            form = parse_qs(body.decode())
            grant = form["grant_type"][0]
            if grant == "refresh_token" and form.get("refresh_token") != ["rtok"]:
                return self.send(400, {"error": "invalid_grant"})
            return self.send(200, {"access_token": "USER" if grant == "refresh_token" else "APP", "expires_in": 7200})
        if p.startswith("/commerce/taxonomy/"):
            if self.headers.get("Authorization") != "Bearer APP":
                return self.send(401, {"errors": [{"message": "needs application token"}]})
            return self.send(200, {"categorySuggestions": [{"category": {"categoryId": "261186", "categoryName": "Libros"}}]})
        if self.headers.get("Authorization") != "Bearer USER":
            return self.send(401, {"errors": [{"message": "needs user token"}]})
        if method == "GET" and p.startswith("/sell/account/v1/"):
            kind = p.rsplit("/", 1)[1]
            key = {"fulfillment_policy": "fulfillmentPolicies", "payment_policy": "paymentPolicies",
                   "return_policy": "returnPolicies"}[kind]
            pid = {"fulfillment_policy": "fulfillmentPolicyId", "payment_policy": "paymentPolicyId",
                   "return_policy": "returnPolicyId"}[kind]
            return self.send(200, {key: [{pid: kind[0].upper() + "1"}]})
        if method == "GET" and p == "/sell/inventory/v1/location":
            return self.send(200, {"locations": [{"merchantLocationKey": "WH1"}]})
        m = re.fullmatch(r"/sell/inventory/v1/inventory_item/(.+)", p)
        if method == "PUT" and m:
            if not self.headers.get("Content-Language"):
                return self.send(400, {"errors": [{"message": "Content-Language required"}]})
            it = json.loads(body)
            prod = it.get("product", {})
            errs = []
            if len(prod.get("title", "")) > 80:
                errs.append("title > 80 chars")
            if not prod.get("imageUrls"):
                errs.append("imageUrls required")
            if it.get("condition") not in ("NEW", "LIKE_NEW", "USED_EXCELLENT", "USED_VERY_GOOD", "USED_GOOD",
                                           "USED_ACCEPTABLE", "SELLER_REFURBISHED"):
                errs.append("bad condition")
            if errs:
                return self.send(400, {"errors": [{"message": e} for e in errs]})
            S.ebay_inventory[m.group(1)] = it
            return self.send(204)
        if method == "GET" and p == "/sell/inventory/v1/offer":
            offers = [o for o in S.ebay_offers.values() if o["sku"] == q.get("sku", [None])[0]]
            return self.send(200, {"offers": offers}) if offers else self.send(404, {"errors": [{"errorId": 25713}]})
        if method == "POST" and p == "/sell/inventory/v1/offer":
            o = json.loads(body)
            if o.get("sku") not in S.ebay_inventory:
                return self.send(400, {"errors": [{"message": "inventory item not found"}]})
            oid = f"OFFER-{len(S.ebay_offers) + 1}"
            S.ebay_offers[oid] = dict(o, offerId=oid)
            return self.send(201, {"offerId": oid})
        m = re.fullmatch(r"/sell/inventory/v1/offer/([^/]+)", p)
        if method == "PUT" and m:
            S.ebay_offers[m.group(1)] = dict(json.loads(body), offerId=m.group(1))
            return self.send(204)
        m = re.fullmatch(r"/sell/inventory/v1/offer/([^/]+)/publish", p)
        if method == "POST" and m:
            S.ebay_publish_calls += 1
            o = S.ebay_offers.get(m.group(1))
            pol = (o or {}).get("listingPolicies", {})
            if not o or not o.get("merchantLocationKey") or not all(
                    pol.get(k) for k in ("fulfillmentPolicyId", "paymentPolicyId", "returnPolicyId")):
                return self.send(400, {"errors": [{"message": "offer incomplete"}]})
            lid = str(110000000000 + len(S.ebay_published))
            S.ebay_published[o["sku"]] = {"listingId": lid, "offer": o, "item": S.ebay_inventory[o["sku"]]}
            return self.send(200, {"listingId": lid})
        self.send(404, {"errors": [{"message": f"unknown ebay endpoint {method} {p}"}]})


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- helpers

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def run(wrapper, args, cwd, env_extra=None, show=True):
    """Run a pusher in a subprocess (so its sys.exit/argparse behave normally)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("EBAY_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = wrapper + f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    if show:
        print("    $ " + " ".join(args))
        for line in out.strip().splitlines()[:40]:
            print("    | " + line)
    return r.returncode, out


EBAY_WRAPPER = f"""import ebay_push as m
_orig = m.Ebay.__init__
def _init(self, *a, **k):
    _orig(self, *a, **k); self.host = {BASE!r}
m.Ebay.__init__ = _init
from ebay_push import main"""

RAW = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
BY_ID = {(r.get("id") or r.get("sku")): r for r in RAW}
PUSHABLE = {"T-NEW", "T-USED", "T-REFURB", "T-LONG", "S-1", "T-SHORT"}  # sold/no-img/no-price never


# ---------------------------------------------------------------- tests

def test_ebay():
    print("\n=== ebay_push.py (eBay Sell API mock) ===")
    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(EBAY_WRAPPER, [pf, "--dry-run"], tmp)
        check("dry-run without credentials works", rc == 0 and "dry run without credentials" in out, out[-300:])

        rc, out = run(EBAY_WRAPPER, [pf, "--market", "EBAY_ES", "--yes"], tmp, EBAY_ENV)
        pub = S.ebay_published
        check("sold-out / no-image / no-price skipped",
              not {"T-SOLD", "T-NOIMG", "T-NOPRICE"} & set(pub))
        check("all pushable items published", set(pub) == PUSHABLE,
              f"published {sorted(pub)}, expected {sorted(PUSHABLE)}  (missing: {sorted(PUSHABLE - set(pub))})")
        check("titles <= 80 chars", all(len(v["item"]["product"]["title"]) <= 80 for v in pub.values()))
        if "T-USED" in pub:
            asp = pub["T-USED"]["item"]["product"]["aspects"]
            check("ISBN sent without hyphens", asp.get("ISBN") == ["9788410413290"], f"got {asp.get('ISBN')}")
            check("price formatted", pub["T-USED"]["offer"]["pricingSummary"]["price"] == {"value": "12.50", "currency": "EUR"})
        check("category suggested + policies + location set",
              all(v["offer"]["categoryId"] == "261186" and v["offer"]["merchantLocationKey"] == "WH1" for v in pub.values()))
        conds = {s: v["item"]["condition"] for s, v in pub.items()}
        bad = {s: c for s, c in conds.items() if BY_ID[s].get("condition") not in (None, "NewCondition") and c == "NEW"}
        check("used/refurbished items not listed as NEW", not bad, f"listed as NEW: {sorted(bad)}")

        calls = S.ebay_publish_calls
        rc, out = run(EBAY_WRAPPER, [pf, "--market", "EBAY_ES", "--yes"], tmp, EBAY_ENV)
        check("re-run does not publish again", S.ebay_publish_calls == calls, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = start_server()
    try:
        test_ebay()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
