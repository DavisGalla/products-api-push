#!/usr/bin/env python3
"""
End-to-end test for amazon_push.py, run against a LOCAL MOCK of Amazon's
Login with Amazon token endpoint and the SP-API Catalog Items + Listings Items
APIs. Nothing is sent to Amazon, and your real amazon_posted.json is not
touched (each test runs in a temporary folder).

    python tests/test_amazon.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
MARKETS = {"ES": ("A1RKKUPIHCS9HS", "EUR"), "DE": ("A1PA6795UKMFR9", "EUR"), "FR": ("A13V1IB3VIYZZH", "EUR"),
           "IT": ("APJ6JRA9NG5V4", "EUR"), "NL": ("A1805IZSGTT6HS", "EUR"), "BE": ("AMEN7PMS2XZ99", "EUR"),
           "PL": ("A1C3SOZRARQ6R3", "PLN"), "SE": ("A2NODRKZP88ZB9", "SEK"), "IE": ("A28R8C7NBKEWEA", "EUR"),
           "UK": ("A1F83G8C2ARO7P", "GBP")}
CURRENCY_OF = {mid: cur for mid, cur in MARKETS.values()}
MKT_CODE = {mid: code for code, (mid, _) in MARKETS.items()}
SELLER = "ATESTSELLER1"
AMAZON_ENV = {"AMAZON_LWA_CLIENT_ID": "cid", "AMAZON_LWA_CLIENT_SECRET": "csecret",
              "AMAZON_REFRESH_TOKEN": "Atzr|rtok", "AMAZON_SELLER_ID": SELLER}
# The mock catalog: EAN -> (ASIN, title, markets that carry it).
# T-NEW is everywhere, T-USED only on ES/DE, T-REFURB's ISBN nowhere.
CATALOG = {"9788409591923": ("B0TESTNEW1", "Dragona. Libro para bebés con rimas", set(MARKETS)),
           "9788410413290": ("B0TESTUSED", "Colección de libros para bebés", {"ES", "DE"})}
CONDITIONS = {"new_new", "new_open_box", "used_like_new", "used_very_good", "used_good",
              "used_acceptable", "collectible_like_new", "collectible_very_good"}


class S:
    listings = {}         # (market code, sku) -> body
    throttle_once = True  # first catalog call returns 429 to exercise the retry
    catalog_calls = 0

    @classmethod
    def reset(cls):
        cls.listings, cls.throttle_once, cls.catalog_calls = {}, True, 0


def listing_issues(body, asin_expected, mkt):
    """Validate a putListingsItem body the way Amazon would for an offer-only listing."""
    iss = []
    def err(code, msg):
        iss.append({"code": code, "message": msg, "severity": "ERROR"})
    if body.get("requirements") != "LISTING_OFFER_ONLY":
        err("REQ", "requirements must be LISTING_OFFER_ONLY")
    if body.get("productType") != "ABIS_BOOK":
        err("PT", f"productType {body.get('productType')} does not match ASIN")
    at = body.get("attributes", {})
    for k in ("merchant_suggested_asin", "condition_type", "purchasable_offer", "fulfillment_availability"):
        if not at.get(k):
            err("MISSING", f"'{k}' is required")
    for k in ("merchant_suggested_asin", "condition_type", "purchasable_offer"):
        if at.get(k) and at[k][0].get("marketplace_id") != mkt:
            err("MKT", f"'{k}' needs marketplace_id {mkt}")
    if at.get("merchant_suggested_asin") and at["merchant_suggested_asin"][0]["value"] != asin_expected:
        err("ASIN", "unknown ASIN")
    if at.get("condition_type") and at["condition_type"][0]["value"] not in CONDITIONS:
        err("COND", f"invalid condition_type {at['condition_type'][0]['value']}")
    try:
        offer = at["purchasable_offer"][0]
        price = offer["our_price"][0]["schedule"][0]["value_with_tax"]
        if offer.get("currency") != CURRENCY_OF[mkt] or not price > 0:
            err("PRICE", f"price must be > 0 {CURRENCY_OF[mkt]}")
    except (KeyError, IndexError, TypeError):
        err("PRICE", "malformed purchasable_offer")
    try:
        fa = at["fulfillment_availability"][0]
        if fa.get("fulfillment_channel_code") != "DEFAULT" or not isinstance(fa.get("quantity"), int):
            err("QTY", "fulfillment_availability needs DEFAULT channel and integer quantity")
    except (KeyError, IndexError, TypeError):
        err("QTY", "malformed fulfillment_availability")
    return iss


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data=None):
        body = b"" if data is None else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")

    def route(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/auth/o2/token":
                f = parse_qs(body.decode())
                if f.get("client_id") != ["cid"] or f.get("client_secret") != ["csecret"]:
                    return self.send(401, {"error": "invalid_client"})
                if f.get("grant_type") != ["refresh_token"] or f.get("refresh_token") != ["Atzr|rtok"]:
                    return self.send(400, {"error": "invalid_grant"})
                return self.send(200, {"access_token": "Atza|TEST", "token_type": "bearer", "expires_in": 3600})

            if self.headers.get("x-amz-access-token") != "Atza|TEST":
                return self.send(403, {"errors": [{"code": "Unauthorized", "message": "Access denied"}]})
            mkt = (q.get("marketplaceIds") or [""])[0]
            if mkt not in CURRENCY_OF or len(q.get("marketplaceIds")) != 1:
                return self.send(400, {"errors": [{"code": "InvalidInput", "message": "bad marketplaceIds"}]})
            code = MKT_CODE[mkt]

            if method == "GET" and u.path == "/catalog/2022-04-01/items":
                S.catalog_calls += 1
                if S.throttle_once:
                    S.throttle_once = False
                    return self.send(429, {"errors": [{"code": "QuotaExceeded", "message": "throttled"}]})
                ident = q.get("identifiers", [""])[0]
                if q.get("identifiersType") not in (["EAN"], ["ISBN"]):
                    return self.send(400, {"errors": [{"code": "InvalidInput", "message": "identifiersType"}]})
                hit = CATALOG.get(ident)
                items = [] if not hit or code not in hit[2] else [{
                    "asin": hit[0],
                    "summaries": [{"marketplaceId": mkt, "itemName": hit[1]}],
                    "productTypes": [{"marketplaceId": mkt, "productType": "ABIS_BOOK"}],
                }]
                return self.send(200, {"numberOfResults": len(items), "items": items})

            m = re.fullmatch(r"/listings/2021-08-01/items/([^/]+)/([^/]+)", u.path)
            if m:
                seller, sku = m.group(1), unquote(m.group(2))
                if seller != SELLER:
                    return self.send(403, {"errors": [{"code": "Unauthorized", "message": "wrong seller"}]})
                if method == "PUT":
                    b = json.loads(body)
                    asin = (b.get("attributes", {}).get("merchant_suggested_asin") or [{}])[0].get("value")
                    known = {a for a, _, where in CATALOG.values() if code in where}
                    iss = listing_issues(b, asin if asin in known else None, mkt)
                    if iss:
                        return self.send(200, {"sku": sku, "status": "INVALID", "submissionId": "x", "issues": iss})
                    if q.get("mode") == ["VALIDATION_PREVIEW"]:
                        return self.send(200, {"sku": sku, "status": "VALID", "submissionId": "preview", "issues": []})
                    S.listings[(code, sku)] = b
                    return self.send(200, {"sku": sku, "status": "ACCEPTED",
                                           "submissionId": f"sub-{len(S.listings)}", "issues": []})
                if method == "GET":
                    if (code, sku) not in S.listings:
                        return self.send(404, {"errors": [{"code": "NOT_FOUND", "message": "SKU not found"}]})
                    return self.send(200, {"sku": sku, "issues": [], "summaries": [{
                        "marketplaceId": mkt, "status": ["BUYABLE", "DISCOVERABLE"],
                        "asin": S.listings[(code, sku)]["attributes"]["merchant_suggested_asin"][0]["value"]}]})
            self.send(404, {"errors": [{"code": "NotFound", "message": f"{method} {u.path}"}]})
        except Exception as e:
            self.send(500, {"errors": [{"code": "MockError", "message": repr(e)}]})


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


WRAPPER = f"""import amazon_push as m
m.SPAPI_HOST = m.SANDBOX_HOST = {BASE!r}
m.LWA_URL = {BASE + '/auth/o2/token'!r}
m.time.sleep = lambda s: None  # no waiting in tests
from amazon_push import main"""


def run(args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AMAZON_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = WRAPPER + f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def test_amazon():
    print("\n=== amazon_push.py (Amazon SP-API mock, EU marketplaces) ===")
    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["push", pf], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["push", pf, "--yes"], tmp, dict(AMAZON_ENV, AMAZON_REFRESH_TOKEN="wrong"))
        check("bad refresh token reported clearly", rc != 0 and "Login failed" in out and not S.listings)

        rc, out = run(["push", pf, "--markets", "ES,XX"], tmp, AMAZON_ENV)
        check("unknown market code rejected", rc != 0 and "Unknown market(s) XX" in out)

        # --- default market (ES) ---
        rc, out = run(["push", pf, "--validate-only"], tmp, AMAZON_ENV)
        check("validate-only lists nothing", rc == 0 and not S.listings, out[-300:])
        check("default market is ES only, both offers VALID",
              out.count("ES: VALID") == 2 and "DE:" not in out, out[-300:])
        check("throttled (429) request is retried", "retrying" in out)

        rc, out = run(["push", pf, "--yes", "--quantity", "2"], tmp, AMAZON_ENV)
        check("ES push: offers for catalog matches only",
              set(S.listings) == {("ES", "T-NEW"), ("ES", "T-USED")}, f"created {sorted(S.listings)}")
        check("book not on the store is skipped", "9780000000002 is not in the amazon.es catalog" in out)
        check("no-ISBN / sold-out / no-price products skipped",
              "sold out at source" in out and "no price" in out and out.count("no ISBN/EAN") == 6)
        new = S.listings[("ES", "T-NEW")]["attributes"]
        used = S.listings[("ES", "T-USED")]["attributes"]
        check("matched ASIN + condition (new_new / used_very_good)",
              new["merchant_suggested_asin"][0]["value"] == "B0TESTNEW1"
              and new["condition_type"][0]["value"] == "new_new"
              and used["condition_type"][0]["value"] == "used_very_good")
        check("EUR price + quantity",
              used["purchasable_offer"][0]["our_price"][0]["schedule"][0]["value_with_tax"] == 12.5
              and new["fulfillment_availability"][0]["quantity"] == 2)

        # --- all EU marketplaces ---
        rc, out = run(["push", pf, "--yes", "--markets", "all"], tmp, AMAZON_ENV)
        eu = [m for m in MARKETS if m != "UK"]
        want = {(m, "T-NEW") for m in eu} | {("ES", "T-USED"), ("DE", "T-USED")}
        check("'all' = 9 EU markets, UK excluded", set(S.listings) == want,
              f"missing {sorted(want - set(S.listings))}, extra {sorted(set(S.listings) - want)}")
        check("already-pushed ES offers not sent again", "ES: ACCEPTED" not in out, out[-400:])
        check("used book skipped where Amazon lacks it", "FR: SKIP" in out and ("FR", "T-USED") not in S.listings)
        se = S.listings[("SE", "T-NEW")]["attributes"]["purchasable_offer"][0]
        pl = S.listings[("PL", "T-NEW")]["attributes"]["purchasable_offer"][0]
        check("SEK / PLN prices converted with EUR_RATES",
              se["currency"] == "SEK" and se["our_price"][0]["schedule"][0]["value_with_tax"] == round(8.5 * 11.2, 2)
              and pl["currency"] == "PLN" and pl["our_price"][0]["schedule"][0]["value_with_tax"] == round(8.5 * 4.25, 2),
              f"SE {se}, PL {pl}")
        de = S.listings[("DE", "T-NEW")]["attributes"]
        check("each offer carries its own marketplace_id",
              de["purchasable_offer"][0]["marketplace_id"] == MARKETS["DE"][0]
              and de["condition_type"][0]["marketplace_id"] == MARKETS["DE"][0])
        state = json.loads((Path(tmp) / "amazon_posted.json").read_text(encoding="utf-8"))
        check("amazon_posted.json keyed per market",
              set(state) == {f"{m}:{k}" for m, k in want} and all(v["status"] == "ACCEPTED" for v in state.values()))

        n = len(S.listings)
        rc, out = run(["push", pf, "--yes", "--markets", "all"], tmp, AMAZON_ENV)
        check("re-run does not push accepted offers again",
              len(S.listings) == n and "ACCEPTED" not in out and out.count("already pushed") == 1, out[-400:])

        rc, out = run(["push", pf, "--yes", "--markets", "UK"], tmp, AMAZON_ENV)
        uk = S.listings.get(("UK", "T-NEW"), {}).get("attributes", {}).get("purchasable_offer", [{}])[0]
        check("UK only when named explicitly, priced in GBP", uk.get("currency") == "GBP")

        rc, out = run(["status"], tmp, AMAZON_ENV)
        check("status checks every recorded offer", rc == 0 and out.count("LIVE") == len(S.listings), out[-300:])
        rc, out = run(["status", "--markets", "DE"], tmp, AMAZON_ENV)
        check("status --markets filters", out.count("LIVE") == 2 and "ES:" not in out, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_amazon()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
