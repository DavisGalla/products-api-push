#!/usr/bin/env python3
"""
End-to-end test for fruugo_push.py. Fruugo has no listing API: it downloads a
product feed from a static URL. So the mock is the retailer's side:

  * a local HTTP server on 127.0.0.1 that serves the scraped source images
    (long URLs, different sizes and formats) and the published web folder,
    the way Fruugo downloads the feed;
  * a local SFTP server (paramiko, throw-away host key) for the "upload to
    your web host" mode.

The published feed is then read back like Fruugo's CSV spec describes it
(comma separated, UTF-8, text in double quotes with doubled inner quotes,
mandatory fields, ISBN for books, image URLs <= 150 characters, images
>= 400x400 px, StockStatus values). Nothing is sent to Fruugo, and your real
fruugo_posted.json is not touched (each test runs in a temporary folder).

    python tests/test_fruugo.py
"""
import contextlib
import csv
import http.server
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import fruugo_push as fp                 # noqa: E402

RESULTS = []


def ok(cond, name):
    RESULTS.append((bool(cond), name))
    print(("PASS " if cond else "FAIL ") + name)


# ------------------------------------------------------------ test images

def png(w, h):
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg(w, h):
    """Minimal JPEG header (APP0 + SOF0) - enough for the size check."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


LONG = "/prod-new/uploads/correos-marketplace-shop/1/product/" + "x" * 120
IMAGES = {
    LONG + "-big.png": png(600, 500),
    LONG + "-photo.jpg": jpeg(800, 800),
    LONG + "-small.png": png(200, 200),
    LONG + "-second.png": png(450, 450),
}


class Web:
    root = None            # the published web folder (FRUUGO_FEED_DIR)
    image_hits = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in IMAGES:
            Web.image_hits.append(self.path)
            body, ctype = IMAGES[self.path], "image/png"
        elif self.path.startswith("/feeds/k7/"):
            f = Web.root / self.path[len("/feeds/k7/"):]
            if not f.is_file():
                self.send_response(404)
                self.end_headers()
                return
            body, ctype = f.read_bytes(), "text/csv"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", "Mon, 09 Nov 2026 10:00:00 GMT")
        self.end_headers()
        self.wfile.write(body)


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
FEED_URL = BASE + "/feeds/k7/fruugo.csv"


def book(i, **kw):
    b = {"id": f"B{i}", "title": f"Libro {i}", "price_eur": 10 + i, "brand": "Cuentos con Valores",
         "category": "Libros infantiles", "isbn": "978-84-10413-29-0", "condition": "NewCondition",
         "sold_out": False, "description": f"Descripción {i}. Más en https://example.com/x o info@example.com",
         "images": [BASE + LONG + "-big.png"]}
    b.update(kw)
    return b


BOOKS = [
    book(1),
    book(2, isbn="8408000003", images=[BASE + LONG + "-photo.jpg", BASE + LONG + "-second.png"],
         title='El "gran" libro, de cuentos', description='Línea 1\nLínea 2, con "comillas"'),
    book(3, isbn="9788408000000"),                                   # bad check digit
    book(4, isbn="9788499000046", images=[BASE + LONG + "-small.png"]),  # image too small
    book(5, isbn="9788499000008", sold_out=True),                    # new and sold out: not listed
    book(6, isbn="9788499000015", brand=""),                         # no brand
]
SETTINGS = dict(CATEGORY="Books > Children's Books", CATEGORY_MAP={}, LEAD_TIME_DAYS=None,
                PACKAGE_WEIGHT_G=None, COUNTRIES="", MIRROR_IMAGES="auto", NOTAVAILABLE_KEEP_DAYS=30)


def run(*argv, env=None, books=None, **settings):
    """Run fruugo_push.main() in-process; returns (output, exit message or None)."""
    for k, v in dict(SETTINGS, **settings).items():
        setattr(fp, k, v)
    for k in [k for k in os.environ if k.startswith("FRUUGO_")]:
        del os.environ[k]
    os.environ.update(env if env is not None else {"FRUUGO_FEED_URL": FEED_URL, "FRUUGO_FEED_DIR": str(Web.root)})
    if books is not None:
        Path("books.json").write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
    sys.argv = ["fruugo_push.py", *argv]
    buf, code = io.StringIO(), None
    with contextlib.redirect_stdout(buf):
        try:
            fp.main()
        except SystemExit as e:
            code = str(e.code)
    out = buf.getvalue() + (code or "")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return out, code


def feed_rows():
    text = (Web.root / "fruugo.csv").read_bytes().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text, newline="")))


# ------------------------------------------------------------- unit checks

def unit():
    ok(fp.to_gtin("8408000003") == "9788408000006" and fp.to_gtin("9788408000000") is None,
       "ISBN-10 -> ISBN-13, bad check digit rejected")
    ok(fp.image_info(png(600, 500)) == ("png", 600, 500) and fp.image_info(jpeg(800, 640)) == ("jpg", 800, 640)
       and fp.image_info(b"GIF89a" + struct.pack("<HH", 401, 402)) == ("gif", 401, 402)
       and fp.image_info(b"<html>") is None, "image size detection (PNG, JPEG, GIF, not an image)")
    t = fp.clean_text("Ver https://x.es/a y www.y.es o a.b@c.es\n\n  fin  " + "palabra " * 900, 5000)
    ok("http" not in t and "www." not in t and "@" not in t and len(t) <= 5000 and t.endswith("…"),
       "description: URLs and e-mails removed, cut to 5000 characters")
    rows = [{"ProductId": 'A"1', "Title": 'x, "y"', "Description": "a\nb", "StockQuantity": 3,
             "NormalPriceWithVAT": "4.12", "VATRate": "4"}]
    data = fp.feed_csv(rows)
    back = list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))
    ok(back[0]["Title"] == 'x, "y"' and back[0]["Description"] == "a\nb" and back[0]["ProductId"] == 'A"1'
       and b',3,' in data and b'"x, ""y"""' in data, "CSV quoting: commas, doubled quotes, line breaks; numbers bare")
    fp.LEAD_TIME_DAYS = 1
    os.environ["FRUUGO_FEED_URL"] = "https://feeds.example.com/" + "a" * 120 + "/fruugo.csv"
    probs = " ".join(fp.settings_problems())
    ok("LEAD_TIME_DAYS" in probs and "too long" in probs, "settings: lead time < 2 and too long feed URL refused")
    fp.LEAD_TIME_DAYS = None


# ---------------------------------------------------------- end-to-end, local

def local():
    out, code = run("push", "books.json", "--dry-run", env={}, books=BOOKS)
    ok(code is None and Path("fruugo_feed.csv").exists() and not list(Web.root.iterdir())
       and not Web.image_hits and not Path("fruugo_posted.json").exists(),
       "dry run: preview written, nothing published, no image downloads, no state")

    out, code = run("push", "books.json", "--yes", CATEGORY="")
    ok(code and "CATEGORY" in code and not list(Web.root.iterdir()), "real push without CATEGORY refused")

    out, code = run("check")
    ok("Ready to push." in out and "answers 404" in out, "check: target writable, feed URL answers 404 before first push")

    out, code = run("push", "books.json", "--yes")
    rows = feed_rows()
    ok(code is None and [r["SkuId"] for r in rows] == ["B1", "B2"], "push: valid books in the feed (" + out.count("SKIP") .__str__() + " skipped)")
    ok("invalid ISBN" in out and "at least 400x400" in out and "no brand" in out,
       "skips explained: bad ISBN, image < 400 px, no brand")
    ok("B5" not in {r["SkuId"] for r in rows}, "a new book that is already sold out is not listed")
    with open(Web.root / "fruugo.csv", newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    ok(header == fp.COLUMNS, "header row uses Fruugo's field names")
    r1, r2 = rows
    mandatory = ["ProductId", "SkuId", "Brand", "Category", "Imageurl1", "StockStatus", "StockQuantity",
                 "Title", "Description", "NormalPriceWithVAT", "VATRate"]
    ok(all(r[c] for r in rows for c in mandatory), "all mandatory fields filled")
    ok(r2["ISBN"] == "9788408000006" and r2["EAN"] == "" and r1["ISBN"] == "9788410413290",
       "books use the ISBN field (13 digits), EAN empty")
    ok(r1["NormalPriceWithVAT"] == "11.00" and r1["VATRate"] == "4" and r1["Currency"] == "EUR"
       and r1["Language"] == "es" and r1["StockStatus"] == "INSTOCK" and r1["StockQuantity"] == "1",
       "price with VAT (2 decimals), VAT rate, currency, language, stock")
    ok(r2["Title"] == 'El "gran" libro, de cuentos' and r2["Description"] == 'Línea 1\nLínea 2, con "comillas"'
       and "http" not in r1["Description"] and "@" not in r1["Description"],
       "title/description round-trip (quotes, commas, line breaks); URLs/e-mails removed")
    ok(r1["Attribute2"] == "Spanish" and r1["Attribute3"] == "Cuentos con Valores" and r1["Manufacturer"] == "Cuentos con Valores",
       "media attributes (language, publisher) and Manufacturer")
    imgs = [r[f"Imageurl{i}"] for r in rows for i in range(1, 6) if r[f"Imageurl{i}"]]
    ok(len(imgs) == 3 and all(len(u) <= 150 and u.startswith(BASE + "/feeds/k7/img/") for u in imgs)
       and r2["Imageurl1"].endswith(".jpg") and r2["Imageurl2"].endswith(".png"),
       "images mirrored next to the feed: short URLs (<= 150), extension from the real format")
    served = [fp.http_get(u)[2] for u in imgs]
    ok(served[0] == IMAGES[LONG + "-big.png"] and all(fp.image_info(b)[1] >= 400 for b in served),
       "mirrored image URLs serve the original images")
    ok("Verified:" in out, "feed downloaded back from the public URL and matched")

    hits, mtime = len(Web.image_hits), (Web.root / "fruugo.csv").stat().st_mtime_ns
    out, code = run("push", "books.json", "--yes")
    ok("unchanged" in out and len(Web.image_hits) == hits and (Web.root / "fruugo.csv").stat().st_mtime_ns == mtime,
       "second run: unchanged feed not republished, images (also rejected ones) not downloaded again")

    out, code = run("push", "books.json", "--yes", books=[dict(b, price_eur=b["price_eur"] + 1) for b in BOOKS])
    ok(feed_rows()[0]["NormalPriceWithVAT"] == "12.00" and "1 changed" not in out and "2 changed" in out,
       "price change republished")

    sold = [dict(b, sold_out=b["sold_out"] or b["id"] == "B2") for b in BOOKS]
    run("push", "books.json", "--yes", books=sold)
    r = {x["SkuId"]: x for x in feed_rows()}["B2"]
    ok(r["StockStatus"] == "OUTOFSTOCK" and r["StockQuantity"] == "0", "sold out -> OUTOFSTOCK, stock 0")
    run("push", "books.json", "--yes", "--drop-sold-out")
    ok({x["SkuId"]: x for x in feed_rows()}["B2"]["StockStatus"] == "NOTAVAILABLE",
       "--drop-sold-out -> NOTAVAILABLE")

    out, code = run("push", "books.json", "--yes", "--limit", "1",
                    books=BOOKS + [book(7, isbn="9788499000022"), book(8, isbn="9788499000039")])
    ok([x["SkuId"] for x in feed_rows()] == ["B1", "B2", "B7"], "--limit 1: only one new book added")

    out, code = run("push", "books.json", "--yes", books=[b for b in BOOKS if b["id"] != "B2"] + [book(7, isbn="9788499000022")])
    rows = {x["SkuId"]: x for x in feed_rows()}
    state = json.loads(Path("fruugo_posted.json").read_text(encoding="utf-8"))
    ok(rows["B2"]["StockStatus"] == "NOTAVAILABLE" and rows["B2"]["Imageurl1"] and state["B2"].get("gone_since")
       and "B2: no longer in the source" in out, "book gone from the source stays in the feed as NOTAVAILABLE")

    state["B2"]["gone_since"] = "2026-01-01T00:00:00"
    Path("fruugo_posted.json").write_text(json.dumps(state), encoding="utf-8")
    out, code = run("push", "books.json", "--yes")
    state = json.loads(Path("fruugo_posted.json").read_text(encoding="utf-8"))
    ok("B2" not in {x["SkuId"] for x in feed_rows()} and "B2" not in state and "dropped" in out,
       f"after NOTAVAILABLE_KEEP_DAYS the book is dropped from feed and state")

    before = (Web.root / "fruugo.csv").read_bytes()
    out, code = run("push", "books.json", "--yes", books=[book(9, isbn="9788499000053")])
    ok(code and "--allow-mass-removal" in code and (Web.root / "fruugo.csv").read_bytes() == before,
       "mass removal guard: refuses to retire most books at once")
    out, code = run("push", "books.json", "--yes", "--allow-mass-removal")
    ok(code is None and {x["SkuId"]: x for x in feed_rows()}["B7"]["StockStatus"] == "NOTAVAILABLE",
       "--allow-mass-removal publishes it")

    out, code = run("push", "books.json", "--yes", books=[book(3, isbn="123")])
    ok(code and "without any available product" in code, "feed without available products refused")

    out, code = run("status")
    ok("the last published feed" in out and "NOTAVAILABLE" in out, "status: online feed matches the last publish")
    (Web.root / "fruugo.csv").write_bytes(b"ProductId\r\n")
    out, code = run("status")
    ok("DIFFERENT" in out, "status: notices when the URL serves another file")


# -------------------------------------------------------- end-to-end, SFTP

def sftp():
    import paramiko
    from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
    from paramiko.sftp import SFTP_OK

    key = paramiko.RSAKey.generate(2048)
    other = paramiko.RSAKey.generate(1024)
    root = Path(tempfile.mkdtemp())
    log = {"auths": 0, "renames": 0}

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, u, p):
            log["auths"] += 1
            return paramiko.AUTH_SUCCESSFUL if (u, p) == ("web", "web-test-password") else paramiko.AUTH_FAILED

        def get_allowed_auths(self, u):
            return "password"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

    class Handle(SFTPHandle):
        def stat(self):
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))

    class Files(SFTPServerInterface):
        def real(self, p):
            p = p.replace("\\", "/").strip("/").removeprefix("./")
            return root / p if p not in ("", ".") else root

        def canonicalize(self, p):
            return "/" + p.strip("/.")

        def list_folder(self, p):
            out = []
            for f in self.real(p).iterdir():
                a = SFTPAttributes.from_stat(f.stat())
                a.filename = f.name
                out.append(a)
            return out

        def stat(self, p):
            try:
                return SFTPAttributes.from_stat(self.real(p).stat())
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

        lstat = stat

        def mkdir(self, p, attr):
            self.real(p).mkdir()
            return SFTP_OK

        def open(self, p, flags, attr):
            try:
                f = open(self.real(p), "wb" if flags & (os.O_WRONLY | os.O_RDWR) else "rb")
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            h = Handle(flags)
            h.filename, h.readfile, h.writefile = p, f, f
            return h

        def posix_rename(self, old, new):
            os.replace(self.real(old), self.real(new))
            log["renames"] += 1
            return SFTP_OK

    def serve(sock, host_key):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
            try:
                t.start_server(server=Server())
            except Exception:
                pass

    def start(host_key):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        threading.Thread(target=serve, args=(s, host_key), daemon=True).start()
        return s, s.getsockname()[1]

    pinned = f"ssh-rsa {key.get_base64()}"
    s1, port = start(key)
    env = {"FRUUGO_FEED_URL": FEED_URL, "FRUUGO_SFTP_HOST": "127.0.0.1", "FRUUGO_SFTP_PORT": str(port),
           "FRUUGO_SFTP_USER": "web", "FRUUGO_SFTP_PASSWORD": "web-test-password", "FRUUGO_SFTP_DIR": "feeds",
           "FRUUGO_SFTP_HOSTKEY": pinned}
    (root / "feeds").mkdir()

    s2, port2 = start(other)
    out, code = run("push", "books.json", "--yes", "--no-verify", env=dict(env, FRUUGO_SFTP_PORT=str(port2)), books=BOOKS)
    ok(code and "unknown host key" in code and log["auths"] == 0, "SFTP: unknown host key -> no login attempted")

    out, code = run("push", "books.json", "--yes", "--no-verify", env={k: v for k, v in env.items() if k != "FRUUGO_SFTP_HOSTKEY"})
    ok(code and "FRUUGO_SFTP_HOSTKEY" in code, "SFTP: refuses to run without a pinned host key")

    out, code = run("push", "books.json", "--yes", "--no-verify", env=env)
    files = sorted(p.relative_to(root / "feeds").as_posix() for p in (root / "feeds").rglob("*") if p.is_file())
    ok(code is None and "fruugo.csv" in files and sum(f.startswith("img/") for f in files) == 3
       and not any("tmp_" in f for f in files) and log["renames"] == 4,
       "SFTP: feed + 3 images uploaded via tmp_ file and rename, img/ folder created")
    ok(next(csv.DictReader(io.StringIO((root / "feeds/fruugo.csv").read_text(encoding="utf-8"), newline="")))["SkuId"] == "B1",
       "SFTP: uploaded feed is the Fruugo CSV")
    s1.close()
    s2.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        Web.root = Path(tmp) / "www"
        Web.root.mkdir()
        try:
            unit()
            local()
            try:
                for f in Path(tmp).glob("fruugo_*.json"):
                    f.unlink()
                sftp()
            except ImportError:
                ok(False, "SFTP checks need paramiko: pip install paramiko")
        finally:
            os.chdir(cwd)
    failed = [n for good, n in RESULTS if not good]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
