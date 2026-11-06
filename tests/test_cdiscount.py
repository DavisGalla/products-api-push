#!/usr/bin/env python3
"""
End-to-end test for cdiscount_push.py, run against a LOCAL MOCK of the
Octopia Seller API (Cdiscount). The mock serves the client-credentials
token, checks the bearer token and the SellerId / SalesChannelId headers,
and implements: /sellers, /products?gtin= (catalog check), the offer package
flow (create -> upload offer requests -> submit -> integration on a later
poll -> paginated per-offer results via the Link header) with the documented
offer-request rules, and /offers with sales channel feedback. Nothing is sent
to Octopia, and your real cdiscount_posted.json is not touched (each test runs
in a temporary folder).

    python tests/test_cdiscount.py
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
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
CLIENT, SECRET, SELLER = "test-octopia-client", "test-octopia-secret", "98979"
ENV = {"OCTOPIA_API_URL": BASE + "/seller/v2", "OCTOPIA_AUTH_URL": BASE + "/token",
       "OCTOPIA_CLIENT_ID": CLIENT, "OCTOPIA_CLIENT_SECRET": SECRET, "OCTOPIA_SELLER_ID": SELLER}
CATALOG = {"9788409591923", "9788410413290"}       # T-REFURB's ISBN deliberately missing
CONDITIONS = {"UsedLikeNew", "UsedVeryGoodState", "UsedAverageState", "New", "RefurbishedLikeNew",
              "RefurbishedVeryGoodState", "RefurbishedCorrectState"}
DELIVERY = {"NTHD", "THD", "EHD", "SHD", "FDHD", "SRHD", "WSHD", "IVP", "SVP", "PPMR", "PPLP"}
PAGE = 2                                            # results page size in the mock, to test Link pagination


class S:
    @classmethod
    def reset(cls):
        cls.tokens = set()
        cls.token_requests = cls.product_calls = 0
        cls.packages = {}            # id -> {type, state, requests, polls, result}
        cls.offers = {}              # reference -> offer request
        cls.throttle_once = cls.reject_next = cls.slow = False


S.reset()


def problem(status, title, detail=None, errors=None):
    return {"type": "https://tools.ietf.org/html/rfc7231", "status": status, "title": title,
            "detail": detail, "traceId": "00-test", "errors": errors or {}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, headers=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            if u.path == "/token":
                return self.token(raw)
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in S.tokens:
                return self.send(401, problem(401, "Unauthorized"))
            if self.headers.get("SellerId") != SELLER:
                return self.send(403, problem(403, "Forbidden", "SellerId not linked to this clientId"))
            if S.throttle_once:
                S.throttle_once = False
                return self.send(429, problem(429, "Too Many Requests"), {"Retry-After": "0"})
            p = u.path[len("/seller/v2"):] if u.path.startswith("/seller/v2") else u.path
            body = json.loads(raw) if raw else None
            return self.route(method, p, q, body)
        except Exception as e:
            self.send(500, problem(500, "Internal error", repr(e)))

    def token(self, raw):
        S.token_requests += 1
        f = {k: v[0] for k, v in parse_qs(raw).items()}
        if f.get("grant_type") != "client_credentials":
            return self.send(400, {"error": "unsupported_grant_type", "error_description": "Unsupported grant_type"})
        if f.get("client_id") != CLIENT or f.get("client_secret") != SECRET:
            return self.send(401, {"error": "invalid_client", "error_description": "Invalid client or Invalid client credentials"})
        tok = "jwt-" + uuid.uuid4().hex
        S.tokens.add(tok)
        self.send(200, {"access_token": tok, "expires_in": 7200, "refresh_expires_in": 0, "token_type": "Bearer",
                        "not-before-policy": 0, "scope": ""})

    def route(self, m, p, q, b):
        if m == "GET" and p == "/sellers":
            return self.send(200, {"seller_id": int(SELLER), "shop_name": "Libros Test", "seller_state": "Activated",
                                   "seller_sub_state": None})
        if m == "GET" and p == "/products":
            S.product_calls += 1
            gtins = [g for g in (q.get("gtin") or "").split(",") if g]
            if len(gtins) > 10:
                return self.send(400, problem(400, "Bad Request", errors={"gtin": ["10 GTIN max."]}))
            return self.send(200, {"items": [{"gtin": g, "label": "Libro", "reference": "AUC" + g} for g in gtins if g in CATALOG]})
        if m == "POST" and p == "/offer-packages":
            if self.headers.get("SalesChannelId") != "CDISFR":
                return self.send(400, problem(400, "Bad Request", errors={"SalesChannelId": ["required"]}))
            if (b or {}).get("packageType") not in ("Upsert", "Update", "Delete"):
                return self.send(400, problem(400, "Bad Request", errors={"packageType": ["invalid"]}))
            pid = f"PKG{len(S.packages) + 1:04d}"
            S.packages[pid] = {"type": b["packageType"], "state": "WaitingForCompletion", "requests": [], "polls": 0,
                               "result": None, "outcome": []}
            return self.send(201, None, {"Content-Location": f"offers-packages/{pid}"})
        parts = p.strip("/").split("/")
        if parts[0] != "offer-packages" and p != "/offers":
            return self.send(404, problem(404, "Not Found"))
        if p == "/offers" and m == "GET":
            if q.get("salesChannelId") != "CDISFR":
                return self.send(400, problem(400, "Bad Request", errors={"salesChannelId": ["required"]}))
            refs = set((q.get("sellerExternalReferences") or "").split(","))
            items = []
            for ref, o in S.offers.items():
                if ref not in refs:
                    continue
                active = o["quantity"] > 0
                item = {"sellerId": int(SELLER), "sellerExternalReference": ref, "condition": o["condition"],
                        "salesChannelId": "CDISFR", "offerId": f"{SELLER}_AUC{o['product']['gtin']}_6_CDISFR",
                        "offerState": "Active" if active else "Inactive", "product": o["product"],
                        "integrationPrice": {"price": o["price"]["price"], "currencyCode": "EUR"},
                        "deliveryOffers": [{"preparationTime": o["preparationTime"], "quantity": o["quantity"],
                                            "deliveryModes": o["deliveryModes"]}]}
                if q.get("expand") == "salesChannelFeedback":
                    item["salesChannelFeedback"] = {"isOnline": active, "bestOfferDetails": {"isBestOffer": active}}
                items.append(item)
            return self.send(200, {"items": items})
        pid = parts[1] if len(parts) > 1 else None
        pkg = S.packages.get(pid)
        if not pkg:
            return self.send(404, problem(404, "Not Found", "package not found"))
        if m == "POST" and len(parts) == 3 and parts[2] == "offer-requests":
            if pkg["state"] != "WaitingForCompletion":
                return self.send(400, problem(400, "Bad Request", "package is not waiting for completion"))
            if not isinstance(b, list) or not 1 <= len(b) <= 100:
                return self.send(400, problem(400, "Bad Request", errors={"body": ["1 to 100 offer requests per upload"]}))
            pkg["requests"] += b
            return self.send(201)
        if m == "PATCH" and len(parts) == 2:
            if (b or {}).get("state") != "Ready" or pkg["state"] != "WaitingForCompletion":
                return self.send(400, problem(400, "Bad Request", errors={"state": ["only Ready, from WaitingForCompletion"]}))
            pkg["state"] = "Ready"
            return self.send(204)
        if m == "GET" and len(parts) == 2:
            if pkg["state"] in ("Ready", "IntegrationPending"):
                pkg["polls"] += 1
                if S.slow:
                    pkg["state"] = "IntegrationPending"
                elif pkg["polls"] >= 2:
                    self.integrate(pkg)
                else:
                    pkg["state"] = "IntegrationPending"
            out = {"packageId": pid, "state": pkg["state"], "type": pkg["type"], "salesChannelId": "CDISFR",
                   "offerRequestCount": str(len(pkg["requests"]))}
            if pkg["result"]:
                out["result"] = pkg["result"]
            return self.send(200, out)
        if m == "GET" and len(parts) == 3 and parts[2] == "offer-requests-results":
            if pkg["state"] != "Integrated":
                return self.send(400, problem(400, "Bad Request", "package not integrated yet"))
            start = int(q.get("offset") or 0)
            page = pkg["outcome"][start:start + PAGE]
            headers = {}
            if start + PAGE < len(pkg["outcome"]):
                headers["Link"] = f'</seller/v2/offer-packages/{pid}/offer-requests-results?offset={start + PAGE}>; rel="next"'
            return self.send(200, {"itemsPerPage": len(page), "items": page}, headers)
        return self.send(405, problem(405, "Method Not Allowed"))

    @staticmethod
    def integrate(pkg):
        if S.reject_next:
            S.reject_next = False
            pkg["state"], pkg["result"] = "Rejected", {"message": "Package: Intégration du package interrompue - Veuillez le resoumettre", "resultCode": "10623"}
            return
        counts = {}
        for r in pkg["requests"]:
            counts[r.get("sellerExternalReference")] = counts.get(r.get("sellerExternalReference"), 0) + 1
        for r in pkg["requests"]:
            ref = r.get("sellerExternalReference")
            res, status = [], "Integrated"
            if counts[ref] > 1:
                status, res = "Duplicated", [{"resultCode": "8101", "message": "Duplicated seller external reference in the package"}]
            elif r.get("product", {}).get("gtin") not in CATALOG:
                status, res = "Rejected", [{"resultCode": "8301", "message": "Product not found for this GTIN"}]
            elif r.get("condition") not in CONDITIONS:
                status, res = "Rejected", [{"resultCode": "8302", "message": "Invalid condition"}]
            elif not isinstance(r.get("price", {}).get("price"), (int, float)) or r["price"]["price"] <= 0 or \
                    not any(t.get("code") == "VAT" for t in r["price"].get("taxes") or []):
                status, res = "Rejected", [{"resultCode": "8303", "message": "Price and VAT tax are mandatory"}]
            elif not r.get("deliveryModes") or any(d.get("code") not in DELIVERY or "cost" not in d for d in r["deliveryModes"]):
                status, res = "Rejected", [{"resultCode": "8304", "message": "Invalid delivery modes"}]
            elif not isinstance(r.get("preparationTime"), int) or not isinstance(r.get("quantity"), int) or r["quantity"] < 0:
                status, res = "Rejected", [{"resultCode": "8305", "message": "preparationTime and quantity are mandatory"}]
            else:
                res = [{"resultCode": "8001" if ref in S.offers else "8000",
                        "message": "Offer updated" if ref in S.offers else "Offer created"}]
                S.offers[ref] = r
            pkg["outcome"].append({"sellerExternalReference": ref, "integrationStatus": status, "results": res})
        pkg["state"] = "Integrated"


RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("OCTOPIA_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = f"from cdiscount_push import main\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def test_cdiscount():
    print("\n=== cdiscount_push.py (Octopia Seller API mock) ===")
    pf = str(PRODUCTS_FILE)
    fast = ["--poll", "0"]
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, dict(ENV, OCTOPIA_CLIENT_SECRET="wrong"))
        check("wrong client secret -> login failed with a hint", rc != 0 and "Octopia login failed (401)" in out, out[-200:])

        rc, out = run(["check"], tmp, dict(ENV, OCTOPIA_SELLER_ID="11111"))
        check("wrong seller id -> 403 with a hint", rc != 0 and "403 Forbidden" in out and "OCTOPIA_SELLER_ID" in out, out[-200:])

        rc, out = run(["check"], tmp, ENV)
        check("check: token + SellerId accepted, shop shown", rc == 0 and "Libros Test" in out and "Activated" in out, out[-200:])

        rc, out = run(["lookup", "978-84-09-59192-3"], tmp, ENV)
        rc2, out2 = run(["lookup", "9780000000002"], tmp, ENV)
        check("lookup: known and unknown ISBN", "in the Cdiscount catalog" in out and "NOT in the Cdiscount catalog" in out2,
              out + out2)

        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = json.loads((Path(tmp) / "cdiscount_offers.json").read_text(encoding="utf-8"))
        new = next((x for x in prev if x["sellerExternalReference"] == "T-NEW"), {})
        used = next((x for x in prev if x["sellerExternalReference"] == "T-USED"), {})
        check("dry run without credentials builds the offer requests, sends nothing",
              rc == 0 and not S.packages and "catalog check skipped" in out
              and new.get("product") == {"gtin": "9788409591923"} and new.get("condition") == "New"
              and new.get("price") == {"price": 8.5, "taxes": [{"code": "VAT", "value": 5.5}]}
              and new.get("deliveryModes") == [{"code": "THD", "cost": 4.99, "additionalCost": 1.0}]
              and new.get("preparationTime") == 2 and new.get("quantity") == 1, out[-400:])
        check("used book: ISBN normalised, condition UsedVeryGoodState",
              used.get("product") == {"gtin": "9788410413290"} and used.get("condition") == "UsedVeryGoodState", str(used))
        check("no-ISBN / sold-out / no-price products skipped with reasons",
              "sold out at source" in out and "no price" in out and out.count("no ISBN/EAN") == 6, out[-500:])

        rc, out = run(["push", pf, "--dry-run"], tmp, ENV)
        prev = json.loads((Path(tmp) / "cdiscount_offers.json").read_text(encoding="utf-8"))
        check("dry run with credentials checks the catalog (one call for all GTINs)",
              "9780000000002 is not in the Cdiscount catalog" in out and S.product_calls == 3 and not S.packages
              and [x["sellerExternalReference"] for x in prev] == ["T-NEW", "T-USED"], out[-400:])

        S.throttle_once = True
        before = S.token_requests
        rc, out = run(["push", pf, "--yes", "--quantity", "2"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "cdiscount_posted.json").read_text(encoding="utf-8"))
        pkg = S.packages.get("PKG0001", {})
        check("push: package created on CDISFR, uploaded, submitted, polled until integrated (429 retried)",
              rc == 0 and pkg.get("type") == "Upsert" and pkg.get("state") == "Integrated" and pkg.get("polls") >= 2
              and "retrying" in out and S.token_requests == before + 1, out[-500:])
        check("integrated offers recorded with price, stock and package id",
              set(state) == {"T-NEW", "T-USED"} and state["T-NEW"]["quantity"] == 2 and state["T-NEW"]["package_id"] == "PKG0001"
              and S.offers["T-NEW"]["quantity"] == 2, str(state))

        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        check("re-run sends nothing new (no package created)", len(S.packages) == 1 and "already pushed" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"] + fast, tmp, ENV)
        check("--resend sends an Upsert with price 8.08 (half-up) and stock 5; offers updated, not duplicated",
              S.offers["T-NEW"]["price"]["price"] == 8.08 and S.offers["T-NEW"]["quantity"] == 5 and len(S.offers) == 2
              and "Offer updated" in out, out[-400:])

        pkgs = len(S.packages)
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"] + fast, tmp, ENV)
        check("--resend with nothing changed creates no package", len(S.packages) == pkgs and "T-NEW: unchanged" in out,
              out[-300:])

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-NEW":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(sold_file), "--yes", "--zero-sold-out"] + fast, tmp, ENV)
        check("--zero-sold-out sets stock 0 for a pushed book now sold out",
              S.offers["T-NEW"]["quantity"] == 0 and "sold-out to set to stock 0" in out, out[-300:])

        rc, out = run(["status"], tmp, ENV)
        check("status: active/online per offer with price and stock",
              rc == 0 and "T-USED: ACTIVE  ONLINE" in out and "T-NEW: INACTIVE  not online" in out and "stock=0" in out,
              out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--no-match-check"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "cdiscount_posted.json").read_text(encoding="utf-8"))
        check("rejected offer request shown with Octopia's result code, not recorded",
              "! T-REFURB: Rejected - 8301 Product not found for this GTIN" in out and "T-REFURB" not in state, out[-400:])
        last = max(S.packages)
        check("results are read across pages (Link: rel=next)", len(S.packages[last]["outcome"]) > 2
              and "T-NEW" in out and "T-USED" in out and "T-REFURB" in out, out[-400:])

        S.reject_next = True
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.9"] + fast, tmp, ENV)
        check("rejected package reported with its reason, nothing recorded",
              "REJECTED: 10623" in out and S.offers["T-USED"]["price"]["price"] != 11.25, out[-300:])

        S.slow = True
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.9", "--wait", "0"] + fast, tmp, ENV)
        slow_pkg = max(S.packages)
        check("slow integration: package id printed, nothing recorded yet", f"still IntegrationPending" in out
              and f"package {slow_pkg}" in out, out[-300:])
        S.slow = False
        rc, out = run(["package", slow_pkg], tmp, ENV)
        rc, out = run(["package", slow_pkg], tmp, ENV)
        check("`package <id>` shows the package state and per-offer results later",
              f"Package {slow_pkg}: Integrated" in out and "T-USED: Integrated" in out, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_cdiscount()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
