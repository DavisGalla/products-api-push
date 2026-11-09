#!/usr/bin/env python3
"""
End-to-end test for booklooker_push.py, run against a LOCAL MOCK of the
booklooker REST API 2.0. The mock authenticates the API key (tokens expire),
checks the account (readyToSell / missing), receives the multipart article
file (UTF-8, tab-separated, booklooker template columns), processes it over
a few polls (FILE_RECEIVED -> UPLOAD_IN_PROGRESS -> UPLOAD_DONE) with the
documented error IDs (empty title, missing price, duplicate order number,
no VLB data for a new book, new-book price differs from the fixed price),
and serves file_status showErrors, article_status and DELETE article.
Nothing is sent to booklooker, and your real booklooker_posted.json is not
touched (each test runs in a temporary folder).

    python tests/test_booklooker.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRODUCTS_FILE = HERE / "test_products.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/2.0"
API_KEY = "test-booklooker-key"
ENV = {"BOOKLOOKER_API_URL": BASE, "BOOKLOOKER_API_KEY": API_KEY}
VLB = {"9788409591923": "8,50"}         # German fixed prices of new books known to the VLB catalog
sys.path.insert(0, str(ROOT))
import booklooker_push as blp           # noqa: E402  (for the official column list)


class S:
    @classmethod
    def reset(cls):
        cls.tokens = set()
        cls.auths = cls.uploads = 0
        cls.files = {}                  # filename -> {"polls", "status", "errors", "params", "rows"}
        cls.articles = {}               # Bestell-Nr -> row dict + status
        cls.ready, cls.missing = True, []
        cls.fail_once = cls.expire_next = cls.slow = False
        cls.deletes = []


S.reset()


def ok(value):
    return {"status": "OK", "returnValue": value}


def nok(code):
    return {"status": "NOK", "returnValue": code}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def handle_any(self, method):
        try:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            iface = u.path[len("/2.0/"):] if u.path.startswith("/2.0/") else ""
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if S.fail_once:
                S.fail_once = False
                return self.send({"message": "busy"}, 503)
            if iface == "authenticate":
                if method != "POST":
                    return self.send(nok("INVALID_REQUEST_METHOD"))
                if not q.get("apiKey"):
                    return self.send(nok("API_KEY_MISSING"))
                if q["apiKey"] != API_KEY:
                    return self.send(nok("AUTHENTICATION_FAILED"))
                S.auths += 1
                tok = uuid.uuid4().hex
                S.tokens.add(tok)
                return self.send(ok(tok))
            tok = q.get("token")
            if not tok:
                return self.send(nok("TOKEN_MISSING"))
            if tok not in S.tokens:
                return self.send(nok("TOKEN_UNKNOWN"))
            if S.expire_next:
                S.expire_next = False
                S.tokens.discard(tok)
                return self.send(nok("TOKEN_EXPIRED"))
            return self.api(method, iface, q, raw)
        except Exception as e:
            self.send({"message": repr(e)}, 500)

    def api(self, m, iface, q, raw):
        if iface == "account" and m == "GET":
            return self.send(ok({"sellerId": 4711, "username": "LibrosTestES", "readyToSell": S.ready, "missing": S.missing}))
        if iface == "file_import" and m == "POST":
            if q.get("fileType", "article") != "article" or q.get("dataType") != "0" or q.get("mediaType") != "0":
                return self.send(nok("INVALID_PARAMETERS"))
            if q.get("formatID") != "1":
                return self.send(nok("FORMAT_ID_MISMATCH"))
            ctype = self.headers.get("Content-Type") or ""
            msg = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
            part = next((p for p in msg.iter_parts() if p.get_param("name", header="content-disposition") == "file"), None)
            if part is None:
                return self.send(nok("FILE_MISSING"))
            name = part.get_filename() or ""
            if not re.search(r"\.(zip|txt|tab|csv|xls)$", name):
                return self.send(nok("INVALID_FILE_TYPE"))
            text = part.get_payload(decode=True).decode("utf-8" if q.get("encoding") == "UTF-8" else "cp1252")
            lines = [ln for ln in text.split("\n") if ln.strip()]
            S.uploads += 1
            S.files[name] = {"polls": 0, "status": "FILE_RECEIVED", "errors": [], "params": q,
                             "header": lines[0].split("\t"), "rows": [ln.split("\t") for ln in lines[1:]]}
            return self.send(ok("SUCCESS"))
        if iface == "file_status" and m == "GET":
            f = S.files.get(q.get("filename"))
            if not f:
                return self.send(nok("NOT_FOUND"))
            if q.get("showErrors") == "1":
                if f["status"] != "UPLOAD_DONE":
                    return self.send(nok("FILE_NOT_PROCESSED"))
                return self.send(ok("\n".join("\t".join(e) for e in f["errors"])))
            if f["status"] != "UPLOAD_DONE" and not S.slow:
                f["polls"] += 1
                if f["polls"] == 1:
                    f["status"] = "UPLOAD_IN_PROGRESS"
                elif f["polls"] >= 2:
                    self.process(f)
            elif S.slow:
                f["status"] = "UPLOAD_IN_PROGRESS"
            return self.send(ok(f["status"]))
        if iface == "article_status" and m == "GET":
            a = S.articles.get(q.get("orderNo"))
            return self.send(ok(a["status"]) if a else nok("NOT_FOUND"))
        if iface == "article" and m == "DELETE":
            if not q.get("orderNo"):
                return self.send(nok("INVALID_PARAMETERS"))
            if q["orderNo"] not in S.articles:
                return self.send(nok("NOT_FOUND"))
            S.articles[q["orderNo"]]["status"] = "DELETED"
            S.deletes.append(q["orderNo"])
            return self.send(ok("SUCCESS"))
        return self.send(nok("INVALID_INTERFACE"))

    @staticmethod
    def process(f):
        f["status"] = "UPLOAD_DONE"
        if f["header"] != blp.COLUMNS:
            f["errors"].append(["8", "0", "", "", "unknown columns"])
            return
        col = {c: i for i, c in enumerate(f["header"]) if c != "unbenutzt"}
        seen = {}
        for r in f["rows"]:
            seen[r[col["Bestell-Nr"]]] = seen.get(r[col["Bestell-Nr"]], 0) + 1
        for rec, r in enumerate(f["rows"], 1):
            get = lambda c: r[col[c]] if col[c] < len(r) else ""
            order, title, price, isbn, new = get("Bestell-Nr"), get("Titel"), get("Ihr Preis in €"), get("ISBN"), get("Neuware?")
            err = None
            if len(r) != len(blp.COLUMNS):
                err = ("8", "wrong number of fields")
            elif seen[order] > 1:
                err = ("1", "")
            elif not title:
                err = ("10", "")
            elif not re.fullmatch(r"\d+,\d{2}", price):
                err = ("11", f"price '{price}'")
            elif new == "ja" and isbn not in VLB:
                err = ("15", f"ISBN {isbn}")
            elif new == "ja" and VLB[isbn] != price:
                err = ("12", f"gebundener Preis {VLB[isbn]} EUR")
            if err:
                f["errors"].append([err[0], str(rec), order, title, err[1]])
                continue
            S.articles[order] = {"title": title, "price": price, "isbn": isbn, "zustand": get("Zustand (1-4)"),
                                 "neu": new, "status": "ACTIVE"}


RESULTS = []


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOOKLOOKER_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = f"from booklooker_push import main\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


def test_booklooker():
    print("\n=== booklooker_push.py (booklooker REST API 2.0 mock) ===")
    pf = str(PRODUCTS_FILE)
    fast = ["--poll", "0"]
    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        rc, out = run(["check"], tmp)
        check("missing API key reported clearly", rc != 0 and "Missing environment variables" in out)

        rc, out = run(["check"], tmp, dict(ENV, BOOKLOOKER_API_KEY="wrong"))
        check("wrong API key -> clear message", rc != 0 and "AUTHENTICATION_FAILED" in out, out[-200:])

        rc, out = run(["check"], tmp, ENV)
        check("check: account found and ready to sell books", rc == 0 and "LibrosTestES" in out and "ready to sell" in out,
              out[-200:])

        S.ready, S.missing = False, ["SHIPPING_COSTS_MISSING", "TAX_DATA_MISSING"]
        rc, out = run(["check"], tmp, ENV)
        check("check: reasons shown when the account can't sell yet",
              "NOT ready to sell" in out and "SHIPPING_COSTS_MISSING" in out and "TAX_DATA_MISSING" in out, out[-200:])
        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        check("push stops before uploading when the account isn't ready", rc != 0 and S.uploads == 0
              and "not ready to sell" in out, out[-200:])
        S.ready, S.missing = True, []

        rc, out = run(["push", pf, "--dry-run"], tmp)
        lines = (Path(tmp) / "booklooker_articles.txt").read_text(encoding="utf-8").split("\n")
        header = lines[0].split("\t")
        rows = {r.split("\t")[header.index("Bestell-Nr")]: dict(zip(header, r.split("\t"))) for r in lines[1:] if r}
        new, used = rows.get("T-NEW", {}), rows.get("T-USED", {})
        check("dry run without a key builds the article file in booklooker's template format, sends nothing",
              rc == 0 and header == blp.COLUMNS and len(header) == 27 and S.uploads == 0
              and all(len(r.split("\t")) == 27 for r in lines[1:] if r), out[-300:])
        check("new book row: ISBN, price 8,50, Zustand 1, Neuware ja, language, cover URL; no tabs/newlines in text",
              new.get("ISBN") == "9788409591923" and new.get("Ihr Preis in €") == "8,50" and new.get("Zustand (1-4)") == "1"
              and new.get("Neuware?") == "ja" and new.get("Sprache") == "Spanisch" and new.get("Cover-URL", "").startswith("http")
              and new.get("Titel", "").startswith("Dragona") and "\n" not in new.get("Beschreibung", ""), str(new)[:400])
        check("used book row: ISBN normalised, Zustand 2, Neuware nein",
              used.get("ISBN") == "9788410413290" and used.get("Zustand (1-4)") == "2" and used.get("Neuware?") == "nein", str(used))
        check("new books without ISBN, sold-out and price-less products skipped with reasons",
              "new books need an ISBN" in out and "sold out at source" in out and "no price" in out, out[-500:])

        S.fail_once, auths = True, S.auths
        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "booklooker_posted.json").read_text(encoding="utf-8"))
        f = S.files[max(S.files)]
        check("push: file uploaded (article, add/change, books, formatID 1, UTF-8), polled until UPLOAD_DONE",
              S.uploads == 1 and f["params"].get("encoding") == "UTF-8" and f["status"] == "UPLOAD_DONE" and f["polls"] >= 2
              and S.auths == auths + 1, out[-400:] + f" auths={S.auths - auths}")
        check("accepted offers recorded (used and new books), 503 retried",
              set(state) >= {"T-NEW", "T-USED", "T-REFURB"} and S.articles["T-NEW"]["neu"] == "ja"
              and state["T-NEW"]["price"] == "8,50", str(state)[:300])

        uploads = S.uploads
        rc, out = run(["push", pf, "--yes"] + fast, tmp, ENV)
        check("re-run uploads nothing", S.uploads == uploads and "already pushed" in out, out[-300:])

        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95"] + fast, tmp, ENV)
        state = json.loads((Path(tmp) / "booklooker_posted.json").read_text(encoding="utf-8"))
        check("--resend: used book re-priced (12,50 x 0.95 = 11,88 half-up), same order number overwritten",
              S.articles["T-USED"]["price"] == "11,88" and state["T-USED"]["price"] == "11,88"
              and sum(1 for k in S.articles if k == "T-USED") == 1, str(S.articles.get("T-USED")))
        check("new book below the German fixed price rejected (error 12), not recorded",
              "! T-NEW: error 12 (new-book price differs from the fixed price)" in out and state["T-NEW"]["price"] == "8,50",
              out[-400:])

        uploads = S.uploads
        rc, out = run(["push", pf, "--yes", "--resend", "--price-factor", "0.95"] + fast, tmp, ENV)
        check("--resend: only changed prices are re-sent (unchanged used books skipped)",
              "T-USED: unchanged" in out and S.uploads == uploads + 1, out[-300:])

        sold = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        for p in sold:
            if p.get("id") == "T-USED":
                p["sold_out"] = True
        sold_file = Path(tmp) / "sold.json"
        sold_file.write_text(json.dumps(sold, ensure_ascii=False), encoding="utf-8")
        S.expire_next = True
        rc, out = run(["push", str(sold_file), "--yes", "--delete-sold-out"] + fast, tmp, ENV)
        check("--delete-sold-out deletes the offer (token expiry handled by re-authenticating)",
              S.deletes == ["T-USED"] and S.articles["T-USED"]["status"] == "DELETED" and "deleted on booklooker" in out, out[-300:])

        rc, out = run(["status"], tmp, ENV)
        check("status: ACTIVE / DELETED per offer", rc == 0 and "T-NEW: ACTIVE" in out and "T-USED: DELETED" in out, out[-300:])

    with tempfile.TemporaryDirectory() as tmp:
        S.reset()
        S.slow = True
        rc, out = run(["push", pf, "--yes", "--wait", "0"] + fast, tmp, ENV)
        check("slow processing: nothing recorded, safe to run again", "still UPLOAD_IN_PROGRESS" in out
              and not (Path(tmp) / "booklooker_posted.json").exists(), out[-300:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_booklooker()
    finally:
        srv.shutdown()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
