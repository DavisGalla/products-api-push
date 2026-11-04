#!/usr/bin/env python3
"""
End-to-end test for fnac_push.py, run against a LOCAL MOCK of the Fnac
Marketplace API v2.6 (XML services auth, offers_update, batch_status,
offers_query). Nothing is sent to Fnac, and your real fnac_posted.json is not
touched (each test runs in a temporary folder).

    python tests/test_fnac.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
NS = "http://www.fnac.com/schemas/mp-dialog.xsd"
PARTNER, SHOP, KEY = (str(uuid.UUID(int=i)) for i in (1, 2, 3))
TOKEN = str(uuid.UUID(int=99))
ENV = {"FNAC_API_URL": BASE + "/api.php", "FNAC_PARTNER_ID": PARTNER, "FNAC_SHOP_ID": SHOP, "FNAC_KEY": KEY}
# Fnac catalog in the mock: EAN -> product name. T-REFURB's ISBN is deliberately missing.
CATALOG = {"9788409591923": "Dragona. Libro para bebés con rimas",
           "9788410413290": "Colección de libros para bebés"}
STATES = {"11", "1", "2", "3", "4", "5", "6", "7", "8"}


def q(tag):
    return f"{{{NS}}}{tag}"


class S:
    offers = {}      # sku -> dict
    batches = {}     # batch_id -> {"results", "polls"}
    updates = 0

    @classmethod
    def reset(cls):
        cls.offers, cls.batches, cls.updates = {}, {}, 0


def xml_response(tag, status, children=()):
    root = ET.Element(q(tag), {"status": status})
    for c in children:
        root.append(c)
    return b'<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="utf-8")


def el(tag, text=None, **attrs):
    e = ET.Element(q(tag), attrs)
    if text is not None:
        e.text = str(text)
    return e


def error_el(msg, code="ERR", severity="ERROR"):
    return el("error", msg, code=code, severity=severity)


def validate_offer(o):
    errs = []
    pref = o.find(q("product_reference"))
    oref = o.find(q("offer_reference"))
    if oref is None or oref.get("type") != "SellerSku" or not (oref.text or "").strip():
        errs.append("offer_reference (SellerSku) required")
    if pref is None or pref.get("type") != "Ean":
        errs.append("product_reference type Ean required")
    elif pref.text not in CATALOG:
        errs.append("Product not found in Fnac catalog")
    try:
        price = float(o.findtext(q("price")))
        if not 0.89 < price <= 20000:
            errs.append("price out of range")
    except (TypeError, ValueError):
        errs.append("invalid price")
    if o.findtext(q("product_state")) not in STATES:
        errs.append("invalid product_state")
    qty = o.findtext(q("quantity"))
    if not (qty or "").isdigit() or int(qty) > 9999:
        errs.append("invalid quantity")
    for tag in ("description", "internal_comment"):
        if len(o.findtext(q(tag)) or "") > 255:
            errs.append(f"{tag} > 255 chars")
    return errs


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        service = urlparse(self.path).path.rsplit("/", 1)[-1]
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return self.send(b"<html>bad xml</html>", 400)
        if root.tag != q(service):
            return self.send(xml_response(f"{service}_response", "FATAL",
                                          [error_el(f"root must be {service} in mp-dialog namespace")]))
        if service == "auth":
            creds = {t: root.findtext(q(t)) for t in ("partner_id", "shop_id", "key")}
            if creds != {"partner_id": PARTNER, "shop_id": SHOP, "key": KEY}:
                return self.send(xml_response("auth_response", "ERROR", [error_el("Authentication failed", "AUTH")]))
            return self.send(xml_response("auth_response", "OK",
                                          [el("token", TOKEN), el("validity", "2099-01-01T00:00:00+01:00"),
                                           el("version", "2.6.0")]))
        resp_tag = {"offers_update": "offers_update_response", "batch_status": "batch_status_response",
                    "offers_query": "offers_query_response"}.get(service)
        if not resp_tag:
            return self.send(b"<html>unknown service</html>", 404)
        if (root.get("partner_id"), root.get("shop_id"), root.get("token")) != (PARTNER, SHOP, TOKEN):
            return self.send(xml_response(resp_tag, "ERROR", [error_el("Invalid token", "TOKEN")]))

        if service == "offers_update":
            offers = root.findall(q("offer"))
            if not offers:
                return self.send(xml_response(resp_tag, "ERROR", [error_el("no offer")]))
            S.updates += 1
            bid = str(uuid.uuid4())
            results = []
            for o in offers:
                sku = o.findtext(q("offer_reference"))
                errs = validate_offer(o)
                if not errs:
                    ean = o.findtext(q("product_reference"))
                    S.offers[sku] = {"offer_seller_id": sku, "product_name": CATALOG[ean], "ean": ean,
                                     "price": o.findtext(q("price")), "quantity": o.findtext(q("quantity")),
                                     "product_state": o.findtext(q("product_state")),
                                     "description": o.findtext(q("description")) or "",
                                     "internal_comment": o.findtext(q("internal_comment")) or "",
                                     "time_to_ship": o.findtext(q("time_to_ship"))}
                results.append((sku, errs))
            S.batches[bid] = {"results": results, "polls": 0}
            return self.send(xml_response(resp_tag, "OK", [el("batch_id", bid)]))

        if service == "batch_status":
            b = S.batches.get(root.findtext(q("batch_id")))
            if not b:
                return self.send(xml_response(resp_tag, "ERROR", [error_el("unknown batch")]))
            b["polls"] += 1
            if b["polls"] == 1:  # first poll: still running, exercises the wait loop
                return self.send(xml_response(resp_tag, "RUNNING", [el("batch_id", root.findtext(q("batch_id")))]))
            kids = [el("batch_id", root.findtext(q("batch_id")))]
            for sku, errs in b["results"]:
                o = el("offer", status="ERROR" if errs else "OK")
                o.append(el("offer_seller_id", sku))
                if not errs:
                    o.append(el("offer_fnac_id", f"FNAC-{sku}"))
                for e in errs:
                    o.append(error_el(e))
                kids.append(o)
            overall = "ERROR" if any(e for _, e in b["results"]) else "OK"
            return self.send(xml_response(resp_tag, overall, kids))

        if service == "offers_query":
            per = int(root.get("results_count") or 100)
            page = int(root.findtext(q("paging")) or 1)
            allo = list(S.offers.values())
            kids = [el("page", page), el("total_paging", max(1, -(-len(allo) // per))),
                    el("nb_total_per_page", per), el("nb_total_result", len(allo))]
            for off in allo[(page - 1) * per: page * per]:
                o = el("offer")
                for k in ("product_name", "offer_seller_id", "price", "quantity", "product_state", "description"):
                    o.append(el(k, off[k]))
                kids.append(o)
            return self.send(xml_response(resp_tag, "OK", kids))


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def run(args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FNAC_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = f"from fnac_push import main\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def test_fnac():
    print("\n=== fnac_push.py (Fnac Marketplace API v2.6 mock) ===")
    pf = str(PRODUCTS_FILE)
    fast = ["--poll", "0"]
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, dict(ENV, FNAC_KEY=str(uuid.UUID(int=7))))
        check("wrong key reported clearly", rc != 0 and "Fnac login failed" in out and "Authentication failed" in out)

        rc, out = run(["check"], tmp, ENV)
        check("check logs in and shows API version", rc == 0 and "2.6.0" in out, out[-300:])

        rc, out = run(["push", pf, "--dry-run"], tmp)  # no credentials needed for a dry run
        xml_path = Path(tmp) / "fnac_offers.xml"
        check("dry-run works without credentials and sends nothing", rc == 0 and xml_path.exists() and S.updates == 0,
              out[-300:])
        offers = ET.parse(xml_path).getroot().findall(q("offer")) if xml_path.exists() else []
        by_sku = {o.findtext(q("offer_reference")): o for o in offers}
        check("dry-run XML has the 3 products with an ISBN", set(by_sku) == {"T-NEW", "T-USED", "T-REFURB"},
              str(sorted(by_sku)))
        check("no-ISBN / sold-out / no-price products skipped",
              "sold out at source" in out and "no price" in out and out.count("no ISBN/EAN") == 6)
        if "T-USED" in by_sku:
            u = by_sku["T-USED"]
            check("EAN from hyphenated ISBN, type attributes, state, price",
                  u.findtext(q("product_reference")) == "9788410413290"
                  and u.find(q("product_reference")).get("type") == "Ean"
                  and u.find(q("offer_reference")).get("type") == "SellerSku"
                  and u.findtext(q("product_state")) == "2" and u.findtext(q("price")) == "12.50")
            check("conditions mapped (new 11 / used 2 / refurbished 1)",
                  by_sku["T-NEW"].findtext(q("product_state")) == "11"
                  and by_sku["T-REFURB"].findtext(q("product_state")) == "1")

        rc, out = run(["push", pf, "--yes", "--quantity", "3", "--comment", "Envío en 24h",
                       "--time-to-ship", "2"] + fast, tmp, ENV)
        check("push: batch sent and polled until finished", S.updates == 1 and "batch ERROR" in out, out[-400:])
        check("offers in the Fnac catalog accepted", set(S.offers) == {"T-NEW", "T-USED"}, str(sorted(S.offers)))
        check("product not on Fnac rejected with Fnac's message",
              "! T-REFURB: ERR Product not found in Fnac catalog" in out, out[-400:])
        new = S.offers.get("T-NEW", {})
        check("quantity, comment, internal comment, time to ship sent",
              new.get("quantity") == "3" and new.get("description") == "Envío en 24h"
              and new.get("internal_comment", "").startswith("Dragona") and new.get("time_to_ship") == "2", str(new))
        state = json.loads((Path(tmp) / "fnac_posted.json").read_text(encoding="utf-8"))
        check("only accepted offers recorded", set(state) == {"T-NEW", "T-USED"}
              and state["T-NEW"]["offer_fnac_id"] == "FNAC-T-NEW", str(state))
        check("summary line", "Done: 2 offer(s) accepted, 1 rejected" in out)

        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        check("re-run only retries the rejected offer", S.updates == 2 and out.count("already pushed") == 2
              and "1 offer(s) to push" in out, out[-400:])

        rc, out = run(["push", pf, "--yes", "--resend", "--quantity", "7", "--price-factor", "0.95"] + fast, tmp, ENV)
        check("--resend updates price and stock", S.offers["T-NEW"]["quantity"] == "7"
              and S.offers["T-NEW"]["price"] == f"{8.5 * 0.95:.2f}", str(S.offers.get("T-NEW")))

        rc, out = run(["status"], tmp, ENV)
        check("status lists the live offers", rc == 0 and "T-NEW: LIVE" in out and "T-USED: LIVE" in out
              and "T-REFURB" not in out, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_fnac()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
