#!/usr/bin/env python3
"""
End-to-end test for abebooks_push.py, run against a LOCAL MOCK of the AbeBooks
Inventory Update API (inventoryUpdateRequest / inventoryUpdateResponse, codes
600-606 and 1xx). Nothing is sent to AbeBooks / IberLibro, and your real
abebooks_posted.json is not touched (each test runs in a temporary folder).

    python tests/test_abebooks.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
USER, KEY = "BOOKSELLER_1", "12345678901234567890"
ENV = {"ABEBOOKS_USERNAME": USER, "ABEBOOKS_API_KEY": KEY}
CONDITIONS = {"New", "As New", "Fine", "Very Good", "Good", "Fair", "Poor"}


class S:
    inventory = {}     # vendorBookID -> dict of fields
    requests = 0
    fail_next = False  # return a retryable 105 once
    raw_bodies = []

    @classmethod
    def reset(cls):
        cls.inventory, cls.requests, cls.fail_next, cls.raw_bodies = {}, 0, False, []


def response(code, books=(), message="Successful transaction"):
    root = ET.Element("inventoryUpdateResponse", {"version": "1.0"})
    ET.SubElement(root, "code").text = code
    ET.SubElement(root, "message", {"lang": "en"}).text = message
    lst = ET.SubElement(root, "AbebookList")
    for vid, c, msg, tt in books:
        b = ET.SubElement(lst, "Abebook")
        ET.SubElement(b, "code").text = c
        ET.SubElement(b, "message").text = msg
        ET.SubElement(b, "transactionType").text = tt.upper()
        ET.SubElement(b, "vendorBookID").text = vid
    return ET.tostring(root, encoding="ISO-8859-1")


def request_error(code, msg):
    root = ET.Element("requestError", {"version": "1.0"})
    ET.SubElement(root, "code").text = code
    ET.SubElement(root, "message", {"lang": "en"}).text = msg
    return ET.tostring(root, encoding="ISO-8859-1")


def check_book(b):
    """Return (code, message) like the real server would for one Abebook."""
    tt = (b.findtext("transactionType") or "").lower()
    vid = b.findtext("vendorBookID") or ""
    if tt not in ("add", "update", "delete"):
        return "603", "Illegal Book Transaction"
    if not 1 <= len(vid) <= 15:
        return "601", "Book id is not valid"
    if tt == "delete":
        return "600", "Book deleted"
    price = b.find("price")
    if price is None or not re.fullmatch(r"\d{1,8}\.\d{2}", price.text or "") or float(price.text) <= 0 \
            or not re.fullmatch(r"[A-Z]{3}", price.get("currency") or ""):
        return "604", "Bad Price"
    if not any((b.findtext(t) or "").strip() for t in ("author", "title", "publisher")):
        return "606", "Bad Book Data"
    for tag, limit in (("title", 750), ("author", 750), ("publisher", 750), ("description", 4000), ("isbn", 15)):
        if len(b.findtext(tag) or "") > limit:
            return "606", f"Bad Book Data ({tag} too long)"
    if b.findtext("bookCondition") and b.findtext("bookCondition") not in CONDITIONS:
        return "606", "Bad Book Data (bookCondition)"
    q = b.find("quantity")
    if q is not None and q.get("amount") and (not q.get("amount").isdigit() or int(q.get("amount")) > 999):
        return "606", "Bad Book Data (quantity)"
    pics = b.find("pictureList")
    if pics is not None and len(pics.findall("pictureURL")) > 5:
        return "606", "Bad Book Data (pictures)"
    return "600", "Book added or updated"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=ISO-8859-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        S.raw_bodies.append(body)
        if not body.startswith(b"<?xml version='1.0' encoding='ISO-8859-1'?>") and \
                not body.startswith(b'<?xml version="1.0" encoding="ISO-8859-1"?>'):
            return self.send(request_error("104", "Invalid XML (missing ISO-8859-1 header)"))
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return self.send(request_error("104", "Invalid XML"))
        if S.fail_next:
            S.fail_next = False
            return self.send(request_error("105", "Unhandled exception"))
        if root.tag != "inventoryUpdateRequest" or root.get("version") != "1.0":
            return self.send(request_error("104", "Invalid XML"))
        action = root.find("action")
        if action is None or action.get("name") != "bookupdate":
            return self.send(request_error("109", "Unknown Action"))
        if (action.findtext("username"), action.findtext("password")) != (USER, KEY):
            return self.send(request_error("110", "User is invalid. Either it is unknown or has an incorrect password"))
        books = root.find("AbebookList").findall("Abebook") if root.find("AbebookList") is not None else []
        if not books:
            return self.send(request_error("606", "Bad Book Data"))
        if len(books) > 100:
            return self.send(response("602", message="Book list too large"))
        S.requests += 1
        out = []
        for b in books:
            code, msg = check_book(b)
            vid, tt = b.findtext("vendorBookID") or "", (b.findtext("transactionType") or "").lower()
            if code == "600":
                if tt == "delete":
                    S.inventory.pop(vid, None)
                else:
                    rec = {c.tag: (c.text or "") for c in b if c.tag not in ("quantity", "pictureList", "price")}
                    rec["price"], rec["currency"] = b.find("price").text, b.find("price").get("currency")
                    rec["quantity"] = b.find("quantity").get("amount") if b.find("quantity") is not None else None
                    rec["pictures"] = [u.text for u in b.iter("pictureURL")]
                    S.inventory[vid] = rec
            out.append((vid, code, msg, tt))
        overall = "600" if all(c == "600" for _, c, _, _ in out) else "606"
        self.send(response(overall, out))


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def run(args, cwd, env_extra=None, extra_code=""):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ABEBOOKS_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = (f"import abebooks_push as m\nm.API_URL = {BASE!r}\nm.time.sleep = lambda s: None\n{extra_code}\n"
            f"from abebooks_push import main\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def test_abebooks():
    print("\n=== abebooks_push.py (AbeBooks Inventory Update API mock) ===")
    pf = str(PRODUCTS_FILE)
    expected = {"T-NEW", "T-USED", "T-REFURB", "T-LONG", "T-NOIMG", "T-SHORT", "S-1"}
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["push", pf, "--yes"], tmp)
        check("missing credentials reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["push", pf, "--dry-run"], tmp)
        xml_path = Path(tmp) / "abebooks_request.xml"
        check("dry-run works without credentials and sends nothing", rc == 0 and xml_path.exists()
              and S.requests == 0, out[-300:])
        if xml_path.exists():
            raw = xml_path.read_bytes()
            check("dry-run file hides the API key", b"API_KEY_HIDDEN" in raw and KEY.encode() not in raw)
            books = {b.findtext("vendorBookID"): b for b in ET.fromstring(raw).iter("Abebook")}
            check("books with title/price are listed (no ISBN needed)", set(books) == expected, str(sorted(books)))
            check("sold-out / no-price skipped", "sold out at source" in out and "no price" in out)
            n = books.get("T-NEW")
            if n is not None:
                check("title, publisher, price+currency, ISBN, condition, quantity written",
                      n.findtext("title") == "Dragona. Libro para bebés con rimas"
                      and n.findtext("publisher") == "Cuentos con Valores"
                      and n.findtext("price") == "8.50" and n.find("price").get("currency") == "EUR"
                      and n.findtext("isbn") == "9788409591923" and n.findtext("bookCondition") == "New"
                      and n.find("quantity").get("amount") == "1" and n.findtext("transactionType") == "add")
                check("pictures not sent unless --pictures", n.find("pictureList") is None)
            u = books.get("T-USED")
            check("used condition + ISBN hyphens removed",
                  u is not None and u.findtext("bookCondition") == "Very Good" and u.findtext("isbn") == "9788410413290")

        rc, out = run(["push", pf, "--yes"], tmp, dict(ENV, ABEBOOKS_API_KEY="wrong"))
        check("wrong API key reported clearly (110)", rc != 0 and "AbeBooks login failed (110)" in out)

        S.fail_next = True
        rc, out = run(["push", pf, "--yes", "--quantity", "2", "--pictures"], tmp, ENV)
        check("retryable server error (105) is retried", "retrying" in out, out[-300:])
        check("push: all books listed", set(S.inventory) == expected, str(sorted(S.inventory)))
        check("request uses ISO-8859-1 and accents survive",
              S.inventory.get("T-NEW", {}).get("title") == "Dragona. Libro para bebés con rimas"
              and b"beb\xe9s" in S.raw_bodies[-1])
        check("quantity and pictures sent", S.inventory.get("T-NEW", {}).get("quantity") == "2"
              and S.inventory["T-NEW"]["pictures"] == ["http://127.0.0.1:8765/img/new-1.jpg"])
        check("long description fits (<= 4000)", len(S.inventory.get("T-LONG", {}).get("description", "")) > 2000)
        state = json.loads((Path(tmp) / "abebooks_posted.json").read_text(encoding="utf-8"))
        check("abebooks_posted.json records every listed book", set(state) == expected)
        check("summary line", f"Done: {len(expected)} sent, 0 failed" in out, out[-200:])

        n = S.requests
        rc, out = run(["push", pf, "--yes"], tmp, ENV)
        check("re-run sends nothing", S.requests == n and "0 book(s) to send" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95"], tmp, ENV)
        check("--resend sends 'update' with new prices", S.inventory["T-NEW"]["transactionType"] == "update"
              and S.inventory["T-NEW"]["price"] == f"{8.5 * 0.95:.2f}", str(S.inventory.get("T-NEW")))

        # pretend T-SOLD was pushed earlier, then went out of stock at the source
        st = json.loads((Path(tmp) / "abebooks_posted.json").read_text(encoding="utf-8"))
        st["T-SOLD"] = {"isbn": "", "title": "Libro agotado en origen"}
        (Path(tmp) / "abebooks_posted.json").write_text(json.dumps(st), encoding="utf-8")
        S.inventory["T-SOLD"] = {"title": "Libro agotado en origen"}
        rc, out = run(["push", pf, "--yes", "--delete-sold-out"], tmp, ENV)
        st = json.loads((Path(tmp) / "abebooks_posted.json").read_text(encoding="utf-8"))
        check("--delete-sold-out removes the sold-out listing", "T-SOLD" not in S.inventory
              and "T-SOLD" not in st and "- T-SOLD: deleted" in out, out[-300:])

        rc, out = run(["delete", "S-1", "--yes"], tmp, ENV)
        check("delete command removes a listing", "S-1" not in S.inventory and "Done: 1 deleted" in out, out[-200:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        bad = [{"id": "ID-MUCH-LONGER-THAN-15", "title": "Libro con id largo", "price_eur": 5,
                "condition": "NewCondition"},
               {"id": "OK-1", "title": "Libro correcto", "price_eur": 5, "condition": "NewCondition"}]
        bf = Path(tmp) / "bad.json"
        bf.write_text(json.dumps(bad), encoding="utf-8")
        rc, out = run(["push", str(bf), "--dry-run"], tmp)
        check("too-long id caught locally", "longer than 15 characters" in out)
        # bypass the local check to see the server's per-book error handling
        rc, out = run(["push", str(bf), "--yes"], tmp, ENV, extra_code="m.problems_of = lambda p: []")
        st = json.loads((Path(tmp) / "abebooks_posted.json").read_text(encoding="utf-8"))
        check("server per-book error (601) shown, only good book recorded",
              "! ID-MUCH-LONGER-THAN-15: 601" in out and set(st) == {"OK-1"}, out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_abebooks()
    finally:
        srv.shutdown()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
