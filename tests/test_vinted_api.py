#!/usr/bin/env python3
"""
End-to-end test for vinted_api_push.py, run against a LOCAL MOCK of the
Vinted Pro API. Nothing is sent to Vinted, and your real posted_api.json is
not touched (each test runs in a temporary folder).

    python tests/test_vinted_api.py

The mock verifies the X-Vpi-Hmac-Sha256 signature exactly as the docs
describe and applies the documented field rules.
"""
import hashlib
import hmac
import json
import os
import re
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
PORT = 8765  # test_products.json image URLs point here
BASE = f"http://127.0.0.1:{PORT}"
VINTED_TOKEN = "test-access,test-signing"

# ---------------------------------------------------------------- mock data

ONTOLOGIES = {
    "enumerations": {
        "catalogs": [
            {"id": 2309, "title": "Entretenimiento", "catalogs": [
                {"id": 2312, "title": "Libros", "catalogs": [
                    {"id": 2320, "title": "Infantiles", "catalogs": []},
                    {"id": 2321, "title": "Ficción", "catalogs": []},
                ]},
            ]},
        ],
        "colors": [{"id": 1, "title": "Negro"}],
        "item_attributes": [{
            "id": "condition", "title": "Estado", "is_required": True,
            "selection_type": "single", "selection_limit": 1,
            "options": [{"id": 6, "title": "Nuevo con etiquetas"}, {"id": 1, "title": "Nuevo sin etiquetas"},
                        {"id": 2, "title": "Muy bueno"}, {"id": 3, "title": "Bueno"},
                        {"id": 4, "title": "Satisfactorio"}],
        }],
        "package_sizes": [{"id": 1, "title": "Pequeño", "weight_description": "< 1 kg"},
                          {"id": 2, "title": "Mediano", "weight_description": "< 5 kg"}],
        "size_groups": [], "statuses": [],
    },
    "validation_rules": {"max_photos": 20, "min_price": 1, "max_price": 5000},
    "version": {"country": "ES", "currency": "EUR", "last_updated_on": "2026-11-01"},
}
LEAF_CATALOGS = {2320, 2321}
CONDITION_OPTION_IDS = {6, 1, 2, 3, 4}
VINTED_FIELDS = {"brand", "catalog_id", "color_ids", "currency", "description", "is_unisex", "isbn",
                 "item_attributes", "manufacturer", "manufacturer_labelling", "measurement_length",
                 "measurement_width", "package_size_id", "photo_urls", "price", "size_id", "status_id",
                 "title", "video_game_rating_id", "item_reference", "is_draft"}


class S:  # recorded mock state
    vinted_items = {}

    @classmethod
    def reset(cls):
        cls.vinted_items = {}


def vinted_item_errors(it):
    errs = []
    for k in ("brand", "catalog_id", "currency", "description", "package_size_id", "photo_urls",
              "price", "title", "item_reference", "is_draft"):
        if it.get(k) is None or it.get(k) in ("", []):
            errs.append(f"{k}: required")
    if unknown := set(it) - VINTED_FIELDS:
        errs.append(f"unknown fields {sorted(unknown)}")
    if not 5 <= len(it.get("title") or "") <= 100:
        errs.append("title: must be 5-100 chars")
    if not 5 <= len(it.get("description") or "") <= 2000:
        errs.append("description: must be 5-2000 chars")
    if it.get("currency") not in ("EUR", "GBP"):
        errs.append("currency: must be EUR or GBP")
    if it.get("catalog_id") not in LEAF_CATALOGS:
        errs.append("catalog_id: must be a leaf catalog")
    if it.get("package_size_id") not in (1, 2):
        errs.append("package_size_id: unknown")
    rules = ONTOLOGIES["validation_rules"]
    if not isinstance(it.get("price"), (int, float)) or not rules["min_price"] <= it["price"] <= rules["max_price"]:
        errs.append("price: out of range")
    if not all(str(u).startswith("http") for u in it.get("photo_urls") or []):
        errs.append("photo_urls: must be URLs")
    for a in it.get("item_attributes") or []:
        if a.get("item_attribute_id") == "condition" and not set(a.get("item_attribute_option_ids", [])) <= CONDITION_OPTION_IDS:
            errs.append("condition: unknown option")
    return errs


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
            if u.path.startswith("/api/"):
                return self.vinted(method, u, body)
            self.send(404, {"error": "no route"})
        except Exception as e:  # surface mock bugs as 500s
            self.send(500, {"error": repr(e)})

    # ---- Vinted Pro API --------------------------------------------------
    def vinted(self, method, u, body):
        access, signing = VINTED_TOKEN.split(",")
        if self.headers.get("X-Vpi-Access-Key") != access:
            return self.send(401, {"error": "invalid access key"})
        m = re.fullmatch(r"t=(\d+),v1=([0-9a-f]{64})", self.headers.get("X-Vpi-Hmac-Sha256", ""))
        if not m:
            return self.send(401, {"error": "malformed signature header"})
        ts, sig = m.groups()
        if abs(time.time() - int(ts)) > 300:
            return self.send(401, {"error": "timestamp too old"})
        msg = f"{ts}.{method}.{self.path}.{access}.".encode() + body
        if not hmac.compare_digest(sig, hmac.new(signing.encode(), msg, hashlib.sha256).hexdigest()):
            return self.send(401, {"error": "invalid signature"})

        if method == "GET" and u.path == "/api/v1/ontologies":
            return self.send(200, ONTOLOGIES)
        if method == "POST" and u.path in ("/api/v1/items", "/api/v1/items/validate"):
            items = json.loads(body)["items"]
            if len(items) > 100:
                return self.send(400, {"error": "max 100 items"})
            results = [(it, vinted_item_errors(it)) for it in items]
            if u.path.endswith("/validate"):
                return self.send(200, {"items": [{"item_reference": it.get("item_reference"), "valid": not e,
                                                  "errors": e} for it, e in results]})
            if any(e for _, e in results):
                return self.send(400, {"errors": {it.get("item_reference"): e for it, e in results if e}})
            out = []
            for it in items:
                iid = str(uuid.uuid4())
                S.vinted_items[iid] = it
                out.append({"item_id": iid, "item_reference": it["item_reference"]})
            return self.send(202, {"items": out})
        m = re.fullmatch(r"/api/v1/items/([0-9a-f-]{36})/status", u.path)
        if method == "GET" and m:
            if m.group(1) not in S.vinted_items:
                return self.send(404, {"error": "not found"})
            return self.send(200, {"status": "ACTIVE", "errors": [],
                                   "marketplace_item_id": 1000 + list(S.vinted_items).index(m.group(1))})
        self.send(404, {"error": "unknown vinted endpoint"})

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
    env = {k: v for k, v in os.environ.items() if not k.startswith("VINTED_")}
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


VINTED_API_WRAPPER = f"import vinted_api_push as m\nm.PROD_URL = m.SANDBOX_URL = {BASE!r}\nfrom vinted_api_push import main"


# ---------------------------------------------------------------- tests

def test_vinted_api():
    print("\n=== vinted_api_push.py (Vinted Pro API mock) ===")
    expected = {"T-NEW", "T-USED", "T-REFURB", "T-LONG", "S-1"}  # sold/no-img/no-price/short skipped
    pf = str(PRODUCTS_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(VINTED_API_WRAPPER, ["ontologies", "--token", VINTED_TOKEN], tmp)
        check("ontologies: signed request accepted", rc == 0 and "catalog_id=2320" in out, out[-300:])
        check("ontologies: cached to .vinted_ontologies.json", (Path(tmp) / ".vinted_ontologies.json").exists())

        rc, out = run(VINTED_API_WRAPPER, ["ontologies", "--token", "test-access,WRONG-KEY"], tmp)
        check("wrong signing key is rejected (401) and reported", rc != 0 and "401" in out, out[-300:])

        args = ["push", pf, "--token", VINTED_TOKEN, "--catalog-id", "2320", "--package-size-id", "1"]
        rc, out = run(VINTED_API_WRAPPER, args + ["--validate-only"], tmp)
        check("validate-only creates nothing", rc == 0 and not S.vinted_items, out[-300:])
        check(f"validate-only: {len(expected)}/{len(expected)} valid", f"{len(expected)}/{len(expected)} valid" in out, out[-300:])

        rc, out = run(VINTED_API_WRAPPER, args, tmp)
        refs = {it["item_reference"] for it in S.vinted_items.values()}
        check("push: correct items created", refs == expected, f"created {sorted(refs)}, expected {sorted(expected)}")
        check("push: sold-out / no-image / no-price / short-title skipped",
              all(f"SKIP" in out and r not in refs for r in ("T-SOLD", "T-NOIMG", "T-NOPRICE", "T-SHORT")))
        items = {it["item_reference"]: it for it in S.vinted_items.values()}
        check("push: every item passes the documented field rules",
              all(not vinted_item_errors(it) for it in items.values()))
        if "T-LONG" in items:
            check("long title/description truncated to 100/2000",
                  len(items["T-LONG"]["title"]) == 100 and len(items["T-LONG"]["description"]) == 2000)
        cond = {r: (it.get("item_attributes") or [{}])[0].get("item_attribute_option_ids") for r, it in items.items()}
        want = {"T-NEW": [1], "T-USED": [2], "T-REFURB": [3], "T-LONG": [1], "S-1": [1]}
        check("condition mapped to the right option ids", cond == want, f"got {cond}, want {want}")
        if "T-USED" in items:
            check("price, currency, photos, ISBN passed through",
                  items["T-USED"]["price"] == 12.5 and items["T-USED"]["currency"] == "EUR"
                  and len(items["T-USED"]["photo_urls"]) == 2 and items["T-USED"]["isbn"] == "978-84-10413-29-0")
        state = json.loads((Path(tmp) / "posted_api.json").read_text(encoding="utf-8"))
        check("posted_api.json records every created item", set(state) == expected)

        n = len(S.vinted_items)
        rc, out = run(VINTED_API_WRAPPER, args, tmp)
        check("re-run does not push duplicates", len(S.vinted_items) == n and "0 item(s) to push" in out, out[-300:])

        rc, out = run(VINTED_API_WRAPPER, ["status", "--token", VINTED_TOKEN], tmp)
        state = json.loads((Path(tmp) / "posted_api.json").read_text(encoding="utf-8"))
        check("status: all items ACTIVE with a Vinted id",
              rc == 0 and all(v.get("status") == "ACTIVE" and v.get("marketplace_item_id") for v in state.values()))

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(VINTED_API_WRAPPER, ["push", pf, "--token", VINTED_TOKEN, "--catalog-id", "2312",
                                           "--package-size-id", "1"], tmp)
        check("non-leaf catalog: API errors shown, nothing created",
              not S.vinted_items and "leaf catalog" in out, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = start_server()
    try:
        test_vinted_api()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
