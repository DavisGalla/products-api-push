#!/usr/bin/env python3
"""
End-to-end test for casadellibro_push.py, run against a LOCAL MOCK of the
Mirakl seller API that Casa del Libro's marketplace uses (A01 account, offer
states, P31 product lookup, OF01/OF02/OF03 offer import, OF21 offer list).
Nothing is sent to Casa del Libro, and your real casadellibro_posted.json is
not touched (each test runs in a temporary folder).

    python tests/test_casadellibro.py
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
API_KEY = "test-mirakl-key"
ENV = {"CASADELLIBRO_API_URL": BASE + "/api", "CASADELLIBRO_API_KEY": API_KEY}
# Casa del Libro catalog in the mock: EAN -> title. T-REFURB's ISBN is deliberately missing.
CATALOG = {"9788409591923": "Dragona. Libro para bebés con rimas",
           "9788410413290": "Colección de libros para bebés"}
STATES = {"11": "Nuevo", "1": "Usado - Como nuevo", "2": "Usado - Muy bueno"}
COLUMNS = ["sku", "product-id", "product-id-type", "description", "internal-description", "price",
           "quantity", "state", "leadtime-to-ship", "logistic-class", "update-delete"]


class S:
    offers = {}       # shop_sku -> offer
    imports = {}      # id -> {"rows", "errors", "polls"}
    uploads = 0

    @classmethod
    def reset(cls):
        cls.offers, cls.imports, cls.uploads = {}, {}, 0


def process_import(text):
    """Validate an OF01 CSV like Mirakl would; return (rows, {sku: error})."""
    lines = text.splitlines()
    if not lines or lines[0].split(";") != COLUMNS:
        return [], {"?": f"bad header: {lines[0] if lines else ''}"}
    rows, errors = list(csv.DictReader(io.StringIO(text), delimiter=";")), {}
    for r in rows:
        e = []
        if r["product-id-type"] != "EAN":
            e.append("product-id-type must be EAN")
        if r["product-id"] not in CATALOG:
            e.append("The product does not exist")
        if r["state"] not in STATES:
            e.append(f"unknown state {r['state']}")
        try:
            if float(r["price"]) <= 0:
                e.append("price must be > 0")
        except ValueError:
            e.append("invalid price")
        if not r["quantity"].isdigit():
            e.append("invalid quantity")
        if r["update-delete"] != "update":
            e.append("update-delete must be update")
        if e:
            errors[r["sku"]] = "; ".join(e)
    return rows, errors


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

    def route(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if self.headers.get("Authorization") != API_KEY:
                return self.send(401, {"message": "Unauthorized"})
            if method == "GET" and u.path == "/api/account":
                return self.send(200, {"shop_id": 4242, "shop_name": "Tienda de Prueba", "shop_state": "OPEN"})
            if method == "GET" and u.path == "/api/offers/states":
                return self.send(200, {"offer_states": [{"code": c, "label": l} for c, l in STATES.items()]})
            if method == "GET" and u.path == "/api/products":
                refs = q.get("product_references", [""])[0].split(",")
                if len(refs) > 100:
                    return self.send(400, {"message": "max 100 references"})
                prods = []
                for ref in refs:
                    typ, _, val = ref.partition("|")
                    if typ == "EAN" and val in CATALOG:
                        prods.append({"product_sku": f"CDL-{val}", "product_title": CATALOG[val],
                                      "product_references": [{"reference_type": "EAN", "reference": val}]})
                return self.send(200, {"products": prods, "total_count": len(prods)})
            if method == "POST" and u.path == "/api/offers/imports":
                msg = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                          for part in msg.iter_parts()}
                if fields.get("import_mode") != b"NORMAL":
                    return self.send(400, {"message": "import_mode must be NORMAL"})
                if "file" not in fields:
                    return self.send(400, {"message": "file missing"})
                rows, errors = process_import(fields["file"].decode("utf-8"))
                S.uploads += 1
                iid = 1000 + S.uploads
                S.imports[iid] = {"rows": rows, "errors": errors, "polls": 0}
                for r in rows:
                    if r["sku"] not in errors:
                        S.offers[r["sku"]] = {"shop_sku": r["sku"], "product_sku": f"CDL-{r['product-id']}",
                                              "price": float(r["price"]), "quantity": int(r["quantity"]),
                                              "state_code": r["state"], "active": True,
                                              "leadtime_to_ship": int(r["leadtime-to-ship"])}
                return self.send(201, {"import_id": iid})
            m = re.fullmatch(r"/api/offers/imports/(\d+)", u.path)
            if method == "GET" and m:
                imp = S.imports[int(m.group(1))]
                imp["polls"] += 1
                if imp["polls"] == 1:  # first poll: still running, exercises the wait loop
                    return self.send(200, {"import_id": int(m.group(1)), "status": "RUNNING"})
                err = len(imp["errors"])
                return self.send(200, {"import_id": int(m.group(1)), "status": "COMPLETE",
                                       "lines_read": len(imp["rows"]), "lines_in_success": len(imp["rows"]) - err,
                                       "lines_in_error": err, "has_error_report": err > 0})
            m = re.fullmatch(r"/api/offers/imports/(\d+)/error_report", u.path)
            if method == "GET" and m:
                imp = S.imports[int(m.group(1))]
                out = io.StringIO()
                w = csv.writer(out, delimiter=";")
                w.writerow(COLUMNS + ["error-message"])
                for r in imp["rows"]:
                    if r["sku"] in imp["errors"]:
                        w.writerow([r[c] for c in COLUMNS] + [imp["errors"][r["sku"]]])
                return self.send(200, out.getvalue().encode(), "text/csv")
            if method == "GET" and u.path == "/api/offers":
                allo = list(S.offers.values())
                off, mx = int(q.get("offset", ["0"])[0]), int(q.get("max", ["10"])[0])
                return self.send(200, {"offers": allo[off:off + mx], "total_count": len(allo)})
            self.send(404, {"message": f"no mock for {method} {u.path}"})
        except Exception as e:
            self.send(500, {"message": repr(e)})


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


WRAPPER = """import casadellibro_push as m
from casadellibro_push import main"""


USED_CODES = {"UsedCondition": "2", "RefurbishedCondition": "2"}  # as if filled in from `states`


def run(args, cwd, env_extra=None, codes=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CASADELLIBRO_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = WRAPPER + (f"\nm.STATE_CODES.update({codes!r})" if codes else "")
    code += f"\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def test_casadellibro():
    print("\n=== casadellibro_push.py (Mirakl seller API mock) ===")
    pf = str(PRODUCTS_FILE)
    fast = ["--poll", "0"]
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, dict(ENV, CASADELLIBRO_API_KEY="wrong"))
        check("wrong API key reported clearly (401)", rc != 0 and "401 Unauthorized" in out)

        rc, out = run(["check"], tmp, ENV)
        check("check shows the shop", rc == 0 and "Tienda de Prueba" in out, out[-300:])

        rc, out = run(["states"], tmp, ENV)
        check("states lists condition codes", rc == 0 and "11" in out and "Nuevo" in out, out[-300:])

        rc, out = run(["push", pf, "--dry-run"], tmp, ENV)
        csv_path = Path(tmp) / "casadellibro_offers.csv"
        check("dry-run writes the CSV and uploads nothing", rc == 0 and csv_path.exists() and S.uploads == 0, out[-300:])
        rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8")), delimiter=";")) if csv_path.exists() else []
        check("CSV has Mirakl OF01 columns, ';' separated", csv_path.exists()
              and csv_path.read_text(encoding="utf-8").splitlines()[0].split(";") == COLUMNS)
        check("only the new book in the catalog is in the CSV", [r["sku"] for r in rows] == ["T-NEW"],
              f"rows {[r['sku'] for r in rows]}")
        check("used book skipped: no state code configured", "no state code for condition 'UsedCondition'" in out)
        check("no-ISBN / sold-out / no-price products skipped",
              "sold out at source" in out and "no price" in out and out.count("no ISBN/EAN") == 6)
        if rows:
            r = rows[0]
            check("EAN, state 11, price, leadtime written",
                  r["product-id"] == "9788409591923" and r["product-id-type"] == "EAN" and r["state"] == "11"
                  and r["price"] == "8.50" and r["leadtime-to-ship"] == "3" and r["update-delete"] == "update")

        rc, out = run(["push", pf, "--dry-run"], tmp, ENV, codes=USED_CODES)
        rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8")), delimiter=";"))
        check("with used codes set: used book included with its code",
              {r["sku"]: r["state"] for r in rows} == {"T-NEW": "11", "T-USED": "2"}, str(rows))
        check("book not in the catalog skipped", "9780000000002 is not in the Casa del Libro catalog" in out)
        check("ISBN with hyphens normalised to EAN", any(r["product-id"] == "9788410413290" for r in rows))

        rc, out = run(["push", pf, "--yes", "--quantity", "2", "--leadtime", "2"] + fast, tmp, ENV)
        check("push: import uploaded, polled until COMPLETE", S.uploads == 1 and "COMPLETE" in out
              and S.imports[1001]["polls"] >= 2, out[-300:])
        check("offer is live with the right price/qty/leadtime",
              S.offers.get("T-NEW", {}).get("price") == 8.5 and S.offers["T-NEW"]["quantity"] == 2
              and S.offers["T-NEW"]["leadtime_to_ship"] == 2)
        state = json.loads((Path(tmp) / "casadellibro_posted.json").read_text(encoding="utf-8"))
        check("casadellibro_posted.json records the offer", set(state) == {"T-NEW"})

        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        check("re-run uploads nothing new", S.uploads == 1 and "already pushed" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--quantity", "5", "--price-factor", "0.95"] + fast, tmp, ENV)
        check("--resend re-uploads for price/stock sync", S.uploads == 2 and S.offers["T-NEW"]["quantity"] == 5
              and S.offers["T-NEW"]["price"] == round(8.5 * 0.95, 2), str(S.offers.get("T-NEW")))

        rc, out = run(["push", pf, "--yes", "--resend", "--no-match-check"] + fast, tmp, ENV, codes=USED_CODES)
        check("server-side errors shown from the error report",
              "! T-REFURB: The product does not exist" in out and "errors 1" in out, out[-400:])

        rc, out = run(["status"], tmp, ENV)
        check("status shows the live offer", rc == 0 and "T-NEW: LIVE" in out, out[-300:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["push", pf, "--yes", "--no-match-check"] + fast, tmp, ENV, codes=USED_CODES)
        state = json.loads((Path(tmp) / "casadellibro_posted.json").read_text(encoding="utf-8"))
        check("rejected lines are not recorded as pushed", set(state) == {"T-NEW", "T-USED"} and "! T-REFURB" in out,
              f"state {sorted(state)}")

        # stock sync: the pushed book sells out -> stock 0; it comes back -> stock restored
        sold = [dict(r, sold_out=True) if (r.get("id") or r.get("sku")) == "T-NEW" else r
                for r in json.loads(Path(pf).read_text(encoding="utf-8"))]
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold), encoding="utf-8")
        uploads = S.uploads
        rc, out = run(["push", str(sold_file), "--yes", "--zero-sold-out", "--no-match-check"] + fast, tmp, ENV,
                      codes=USED_CODES)
        state = json.loads((Path(tmp) / "casadellibro_posted.json").read_text(encoding="utf-8"))
        check("--zero-sold-out: sold-out offer re-sent with stock 0",
              S.uploads == uploads + 1 and S.offers.get("T-NEW", {}).get("quantity") == 0
              and state["T-NEW"]["quantity"] == 0, out[-300:])
        rc, out = run(["push", pf, "--yes", "--zero-sold-out", "--no-match-check"] + fast, tmp, ENV, codes=USED_CODES)
        state = json.loads((Path(tmp) / "casadellibro_posted.json").read_text(encoding="utf-8"))
        check("--zero-sold-out: back in stock -> quantity restored",
              S.offers.get("T-NEW", {}).get("quantity") == 1 and state["T-NEW"]["quantity"] == 1, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_casadellibro()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
