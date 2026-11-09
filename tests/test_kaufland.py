#!/usr/bin/env python3
"""
End-to-end test for kaufland_push.py, run against a LOCAL MOCK of the
Kaufland Seller API v2. The mock checks every request's HMAC signature,
headers and timestamp like the real API, and serves /info/storefront,
/products/ean/{ean}, /units/ (create/update + list) and /units/status.
Nothing is sent to Kaufland, and your real kaufland_posted.json is not
touched (each test runs in a temporary folder).

    python tests/test_kaufland.py
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/v2"
CLIENT = "c" * 32
SECRET = "0123456789abcdef" * 4
ENV = {"KAUFLAND_API_URL": BASE, "KAUFLAND_CLIENT_KEY": CLIENT, "KAUFLAND_SECRET_KEY": SECRET}
STOREFRONTS = ["de", "at", "cz", "sk", "pl", "fr", "it", "es", "nl"]
# Kaufland catalog in the mock (per storefront). T-REFURB's ISBN is deliberately missing.
CATALOG = {"9788409591923": 1001, "9788410413290": 1002}
CONDITIONS = {"NEW", "USED___AS_NEW", "USED___VERY_GOOD", "USED___GOOD", "USED___ACCEPTABLE",
              "REFURBISHED___AS_NEW", "REFURBISHED___VERY_GOOD", "REFURBISHED___GOOD", "REFURBISHED___ACCEPTABLE"}


class S:
    units = {}          # id_unit -> unit
    posts = 0
    bad_sigs = 0
    throttle_once = False
    signed_uris = []

    @classmethod
    def reset(cls):
        cls.units, cls.posts, cls.bad_sigs, cls.throttle_once, cls.signed_uris = {}, 0, 0, False, []


def problem(kind, msg, errors=None):
    return {"type": f"/problems/{kind}", "message": msg, "errors": errors or []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def authorised(self, method, body):
        h = self.headers
        for name in ("Shop-Client-Key", "Shop-Timestamp", "Shop-Signature", "User-Agent", "Accept"):
            if not h.get(name):
                self.send(400, problem("missing-or-invalid-request-header", f"missing header {name}"))
                return False
        if h["Shop-Client-Key"] != CLIENT:
            self.send(401, problem("invalid-credentials", "Invalid client key"))
            return False
        if abs(int(h["Shop-Timestamp"]) - time.time()) > 300:
            self.send(401, problem("unauthorized", "Timestamp out of range"))
            return False
        uri = f"http://127.0.0.1:{PORT}{self.path}"
        expected = hmac.new(SECRET.encode(), "\n".join([method, uri, body, h["Shop-Timestamp"]]).encode(),
                            hashlib.sha256).hexdigest()
        if h["Shop-Signature"] != expected:
            S.bad_sigs += 1
            self.send(401, problem("corrupted-signature", "Signature mismatch"))
            return False
        S.signed_uris.append(uri)
        return True

    def handle_any(self, method):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            if not self.authorised(method, raw):
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            if method == "GET" and p == "/v2/info/storefront":
                return self.send(200, {"data": [{"storefront": s} for s in STOREFRONTS]})
            if method == "GET" and p.startswith("/v2/products/ean/"):
                if q.get("storefront") not in STOREFRONTS:
                    return self.send(400, problem("storefront-not-configured", "storefront missing"))
                ean = p.rsplit("/", 1)[1]
                if ean in CATALOG:
                    return self.send(200, {"data": {"id_product": CATALOG[ean], "eans": [ean]}})
                return self.send(404, problem("not-found", "Product not found"))
            if method == "POST" and p == "/v2/units/":
                S.posts += 1
                if S.throttle_once:
                    S.throttle_once = False
                    return self.send(429, {"message": "429 Too Many Requests"})
                b = json.loads(raw)
                errs = []
                if b.get("storefront") not in STOREFRONTS:
                    errs.append({"field": "storefront", "message": "not valid"})
                if b.get("condition") not in CONDITIONS:
                    errs.append({"field": "condition", "message": "not valid"})
                if not isinstance(b.get("listing_price"), int) or b["listing_price"] <= 0:
                    errs.append({"field": "listing_price", "message": "must be a positive integer (cents)"})
                if not isinstance(b.get("amount"), int) or not 0 <= b["amount"] <= 99999:
                    errs.append({"field": "amount", "message": "0..99999"})
                if len(b.get("note") or "") > 250:
                    errs.append({"field": "note", "message": "max 250"})
                if not b.get("ean") and not b.get("id_product"):
                    errs.append({"field": "ean", "message": "ean or id_product required"})
                if errs:
                    return self.send(400, problem("validation-error", "Validation Failed", errs))
                if b["ean"] not in CATALOG:
                    return self.send(422, problem("action-not-allowed", "Product does not exist"))
                existing = next((uid for uid, x in S.units.items()
                                 if x["id_offer"] == b["id_offer"] and x["storefront"] == b["storefront"].upper()), None)
                uid = existing or 5000 + len(S.units) + 1
                S.units[uid] = dict(b, id_unit=uid, id_product=CATALOG[b["ean"]], storefront=b["storefront"].upper(),
                                    status="AVAILABLE", is_live=b["amount"] > 0)
                return self.send(201, {"data": S.units[uid]})
            if method == "GET" and p == "/v2/units/":
                sf = (q.get("storefront") or "").upper()
                allu = [x for x in S.units.values() if x["storefront"] == sf]
                off, lim = int(q.get("offset", 0)), int(q.get("limit", 20))
                return self.send(200, {"data": allu[off:off + lim],
                                       "pagination": {"offset": off, "limit": lim, "total": len(allu)}})
            if method == "POST" and p == "/v2/units/status":
                ids = json.loads(raw).get("unit_ids", [])
                if len(ids) > 20:
                    return self.send(400, problem("validation-error", "max 20 unit_ids"))
                return self.send(207, {"data": [{"id_unit": i, "status_code": 200, "reasons": ["stock_update_needed"]}
                                                for i in ids if i in S.units]})
            self.send(404, {"message": f"no mock for {method} {u.path}"})
        except Exception as e:
            self.send(500, {"message": repr(e)})


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def run(args, cwd, env_extra=None, conditions=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAUFLAND_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = "import kaufland_push as m\nfrom kaufland_push import main"
    if conditions is not None:
        code += f"\nm.CONDITION_MAP.clear(); m.CONDITION_MAP.update({conditions!r})"
    code += f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def test_kaufland():
    print("\n=== kaufland_push.py (Kaufland Seller API v2 mock) ===")
    sys.path.insert(0, str(ROOT))
    import kaufland_push
    sig = kaufland_push.Kaufland.signature("a7d0cb1da1ddbc86c96ee5fedd341b7d8ebfbb2f5c83cfe0909f4e57f05dd403",
                                           "POST", "https://sellerapi.kaufland.com/v2/units/", "", 1411055926)
    check("signature matches the test vector from Kaufland's docs",
          sig == "da0b65f51c0716c1d3fa658b7eaf710583630a762a98c9af8e9b392bd9df2e2a", sig)

    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, dict(ENV, KAUFLAND_SECRET_KEY="f" * 64))
        check("wrong secret key -> 401 with a clear message", rc != 0 and "401 Unauthorized" in out
              and "Signature mismatch" in out, out[-300:])

        rc, out = run(["check"], tmp, ENV)
        check("check: signed request accepted, storefronts listed incl. es", rc == 0 and "Keys accepted" in out
              and "es" in out and "!" not in out, out[-300:])

        rc, out = run(["check", "--storefront", "xx"], tmp, ENV)
        check("unknown storefront is flagged", "storefront 'xx' is not in the list" in out, out[-300:])

        rc, out = run(["push", pf, "--dry-run"], tmp)
        prev = Path(tmp) / "kaufland_units.json"
        check("dry run without credentials builds the preview, sends nothing",
              rc == 0 and prev.exists() and S.posts == 0 and "catalog check skipped" in out, out[-300:])

        rc, out = run(["push", pf, "--dry-run"], tmp, ENV)
        units = json.loads(prev.read_text(encoding="utf-8"))
        check("dry run with credentials checks the catalog", "9780000000002 is not in the Kaufland catalog" in out
              and [u["id_offer"] for u in units] == ["T-NEW", "T-USED"] and S.posts == 0, out[-400:])
        new = next((u for u in units if u["id_offer"] == "T-NEW"), {})
        check("unit body: EAN, condition NEW, price in cents, amount, handling time, storefront es",
              new.get("ean") == "9788409591923" and new.get("condition") == "NEW" and new.get("listing_price") == 850
              and new.get("amount") == 1 and new.get("handling_time") == 3 and new.get("storefront") == "es", str(new))
        used = next((u for u in units if u["id_offer"] == "T-USED"), {})
        check("used book: ISBN normalised, condition USED___VERY_GOOD",
              used.get("ean") == "9788410413290" and used.get("condition") == "USED___VERY_GOOD", str(used))
        check("no-ISBN / sold-out / no-price products skipped",
              "sold out at source" in out and "no price" in out and out.count("no ISBN/EAN") == 6, out[-500:])

        S.throttle_once, S.bad_sigs = True, 0
        rc, out = run(["push", pf, "--yes", "--quantity", "2", "--handling-days", "1",
                       "--note", "Envío rápido"], tmp, ENV)
        live = {u["id_offer"]: u for u in S.units.values()}
        check("push creates units (429 retried)", rc == 0 and set(live) == {"T-NEW", "T-USED"}
              and "retrying" in out and S.bad_sigs == 0, out[-400:])
        check("price/qty/handling/note sent correctly",
              live.get("T-NEW", {}).get("listing_price") == 850 and live["T-NEW"]["amount"] == 2
              and live["T-NEW"]["handling_time"] == 1 and live["T-NEW"]["note"] == "Envío rápido", str(live.get("T-NEW")))
        state = json.loads((Path(tmp) / "kaufland_posted.json").read_text(encoding="utf-8"))
        check("kaufland_posted.json records units per storefront", set(state) == {"ES:T-NEW", "ES:T-USED"}
              and state["ES:T-NEW"]["id_unit"], str(state))
        check("signed URIs include the query string", any("?storefront=es" in uri for uri in S.signed_uris))

        posts = S.posts
        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        check("re-run sends nothing new", S.posts == posts and "already pushed" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95", "--quantity", "5"], tmp, ENV)
        check("--resend updates the same units (no duplicates)", len(S.units) == 2
              and live["T-NEW"]["id_unit"] == next(u for u in S.units.values() if u["id_offer"] == "T-NEW")["id_unit"]
              and next(u for u in S.units.values() if u["id_offer"] == "T-NEW")["listing_price"] == 808,
              str(list(S.units.values())))

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-USED":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(sold_file), "--yes", "--zero-sold-out"], tmp, ENV)
        u = next(u for u in S.units.values() if u["id_offer"] == "T-USED")
        check("--zero-sold-out sets stock 0 for a pushed book now sold out",
              u["amount"] == 0 and "set to stock 0" in out, out[-300:])

        rc, out = run(["status"], tmp, ENV)
        check("status shows live and not-live units with reasons",
              rc == 0 and "T-NEW: LIVE" in out and "T-USED: not live" in out and "stock_update_needed" in out, out[-400:])

        rc, out = run(["push", pf, "--yes", "--resend", "--no-match-check"], tmp, ENV)
        check("server-side rejection shown with its message", "! T-REFURB: 422 Product does not exist" in out, out[-400:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0"], tmp, ENV)
        check("validation errors shown field by field",
              "! T-NEW: 400 Validation Failed (listing_price:" in out, out[-400:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["push", pf, "--yes", "--no-match-check"], tmp, ENV)
        state = json.loads((Path(tmp) / "kaufland_posted.json").read_text(encoding="utf-8"))
        check("rejected units are not recorded as pushed", set(state) == {"ES:T-NEW", "ES:T-USED"}
              and "! T-REFURB" in out, str(state))

        rc, out = run(["push", pf, "--yes", "--storefront", "DE", "--limit", "1"], tmp, ENV)
        state = json.loads((Path(tmp) / "kaufland_posted.json").read_text(encoding="utf-8"))
        check("--storefront de pushes to another country separately", "DE:T-NEW" in state
              and any(u["storefront"] == "DE" for u in S.units.values()), str(state))

        posts = S.posts
        rc, out = run(["push", pf, "--yes", "--storefronts", "es,fr,cz,pl"], tmp, ENV)
        state = json.loads((Path(tmp) / "kaufland_posted.json").read_text())
        unit = lambda sf, sku: next((u for u in S.units.values() if u["storefront"] == sf and u["id_offer"] == sku), {})
        check("--storefronts: FR, CZ and PL listed in one run, ES not sent again",
              rc == 0 and {"FR:T-NEW", "CZ:T-NEW", "PL:T-NEW", "FR:T-USED"} <= set(state)
              and "--- kaufland.cz (CZK) ---" in out and not any(k.startswith("ES:") and v.get("pushed_at") is None
                                                                for k, v in state.items()), out[-400:])
        check("kaufland.cz price converted to CZK, kaufland.pl to PLN, kaufland.fr stays EUR",
              unit("CZ", "T-NEW").get("listing_price") == 21250 and unit("PL", "T-NEW").get("listing_price") == 3613
              and unit("FR", "T-NEW").get("listing_price") == 850,
              f'{unit("CZ", "T-NEW").get("listing_price")} {unit("PL", "T-NEW").get("listing_price")} {unit("FR", "T-NEW").get("listing_price")}')
        rc, out = run(["push", pf, "--yes", "--storefronts", "es,fr,cz,pl"], tmp, ENV)
        check("--storefronts re-run sends nothing new", "offer(s) to push" in out and S.posts == posts + 6, out[-300:])
        rc, out = run(["push", pf, "--yes", "--storefronts", "xx"], tmp, ENV)
        check("unknown storefront in --storefronts refused", rc != 0 and "Unknown Kaufland storefront" in out, out[-200:])

    with tempfile.TemporaryDirectory() as tmp:
        rc, out = run(["push", pf, "--dry-run", "--storefronts", "eur"], tmp, {})
        prev = json.loads((Path(tmp) / "kaufland_units.json").read_text(encoding="utf-8"))
        check("--storefronts eur: one preview with the 7 euro storefronts (dry run, no keys)",
              rc == 0 and sorted({u["storefront"] for u in prev}) == ["at", "de", "es", "fr", "it", "nl", "sk"],
              str(sorted({u["storefront"] for u in prev})))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_kaufland()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
