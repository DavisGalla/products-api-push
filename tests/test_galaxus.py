#!/usr/bin/env python3
"""
End-to-end test for galaxus_push.py, run against a LOCAL MOCK of the Galaxus
SFTP server (a real SFTP server built with paramiko, on 127.0.0.1, with its
own throw-away host key and a temporary folder). The test checks the
password login, the pinned host key (no password is sent to an unknown
server), the tmp_ upload + rename, retries, and then imports the uploaded
feed the way the Galaxus docs describe the StockPricingData file (UTF-8,
";" separated, header row, strings in quotes, unique ProviderKey, valid GTIN,
"." decimals without thousands separators, price column with the currency
suffix). Nothing is sent to Galaxus, and your real galaxus_posted.json is
not touched (each test runs in a temporary folder).

    pip install paramiko
    python tests/test_galaxus.py
"""
import csv
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from decimal import Decimal
from pathlib import Path

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_FAILURE, SFTP_OK, SFTP_OP_UNSUPPORTED

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
USER, PASSWORD = "gx-test-user", "gx-test-password"
HOST_KEY = paramiko.RSAKey.generate(2048)
sys.path.insert(0, str(ROOT))
import galaxus_push as gx               # noqa: E402  (for the unit checks)


class S:
    @classmethod
    def reset(cls, root):
        cls.root = Path(root)
        cls.auths = 0
        cls.opened, cls.renames = [], []
        cls.fail_writes = 0             # fail the next N file opens for writing
        cls.no_posix_rename = False


# ------------------------------------------------------------ mock SFTP server

class Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        S.auths += 1
        return paramiko.AUTH_SUCCESSFUL if (username, password) == (USER, PASSWORD) else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Handle(SFTPHandle):
    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


class Files(SFTPServerInterface):
    def real(self, path):
        name = path.replace("\\", "/").strip("/")
        name = "" if name in ("", ".") else name.removeprefix("./")
        if "/" in name or name == "..":
            raise OSError(2, "no sub folders on the mock")
        return S.root / name if name else S.root

    def canonicalize(self, path):
        return "/" + path.replace("\\", "/").strip("/.")

    def list_folder(self, path):
        try:
            out = []
            for f in self.real(path).iterdir():
                attr = SFTPAttributes.from_stat(f.stat())
                attr.filename = f.name
                out.append(attr)
            return out
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(self.real(path).stat())
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            p = self.real(path)
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR))
            if writing:
                if S.fail_writes:
                    S.fail_writes -= 1
                    return SFTP_FAILURE
                S.opened.append(p.name)
            f = open(p, "wb" if writing else "rb")
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        h = Handle(flags)
        h.filename, h.readfile, h.writefile = str(p), f, f
        return h

    def remove(self, path):
        try:
            self.real(path).unlink()
            return SFTP_OK
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def rename(self, old, new):
        try:
            if self.real(new).exists():         # plain SFTP v3 rename does not overwrite
                return SFTP_FAILURE
            self.real(old).rename(self.real(new))
            S.renames.append((Path(old).name, Path(new).name, "rename"))
            return SFTP_OK
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def posix_rename(self, old, new):
        if S.no_posix_rename:
            return SFTP_OP_UNSUPPORTED
        try:
            os.replace(self.real(old), self.real(new))
            S.renames.append((Path(old).name, Path(new).name, "posix"))
            return SFTP_OK
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)


def serve(sock):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=session, args=(conn,), daemon=True).start()


def session(conn):
    t = paramiko.Transport(conn)
    t.add_server_key(HOST_KEY)
    t.set_subsystem_handler("sftp", SFTPServer, Files)
    try:
        t.start_server(server=Server())
    except (paramiko.SSHException, EOFError, OSError):
        return
    while t.is_active():
        t.join(0.2)


# ------------------------------------------------- the "Galaxus importer" side

def gtin_ok(g):
    return re.fullmatch(r"\d{8}|\d{12,14}", g) and \
        (10 - sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(g[:-1]))) % 10) % 10 == int(g[-1])


def import_feed(raw, currency="CHF"):
    """Parse the uploaded file like the documented StockPricingData import; return (rows, errors)."""
    errors = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], ["not UTF-8"]
    lines = text.splitlines()
    header = lines[0].split(";") if lines else []
    need = ["ProviderKey", "MainGtin", f"PriceExclVat_{currency}", "QuantityOnStock"]
    if not header or header[0] != "ProviderKey":
        errors.append("ProviderKey must be the first column")
    for n in need:
        if n not in header:
            errors.append(f"missing column {n}")
    if len(set(header)) != len(header):
        errors.append("duplicate column names")
    rows, keys = [], set()
    for no, line in enumerate(lines[1:], 2):
        if not line.strip():
            errors.append(f"line {no}: empty line")
            continue
        quoted = [bool(re.fullmatch(r'"(?:[^"]|"")*"', c)) for c in line.split(";")]
        r = dict(zip(header, next(csv.reader([line], delimiter=";", quotechar='"'))))
        if not quoted[header.index("ProviderKey")]:
            errors.append(f"line {no}: ProviderKey (text) not in quotes")
        k = r.get("ProviderKey", "")
        if not k or len(k) > 50 or not all(32 <= ord(c) <= 126 for c in k):
            errors.append(f"line {no}: bad ProviderKey {k!r}")
        if k in keys:
            errors.append(f"line {no}: duplicate ProviderKey {k}")
        keys.add(k)
        if not gtin_ok(r.get("MainGtin", "")):
            errors.append(f"line {no}: bad MainGtin {r.get('MainGtin')!r}")
        price = r.get(f"PriceExclVat_{currency}", "")
        if not re.fullmatch(r"\d{1,8}([.,]\d{1,4})?", price):
            errors.append(f"line {no}: bad price {price!r}")
        if not re.fullmatch(r"-?\d{1,5}", r.get("QuantityOnStock", "")):
            errors.append(f"line {no}: bad stock {r.get('QuantityOnStock')!r}")
        if "ManufacturerKey" in r and not 4 <= len(r["ManufacturerKey"]) <= 50:
            errors.append(f"line {no}: ManufacturerKey must be 4-50 characters")
        rows.append(r)
    return rows, errors


# ------------------------------------------------------------------ the tests

RESULTS = []
FEED = "StockPricingData_TestShop.csv"


def check(name, ok_, detail=""):
    RESULTS.append((name, bool(ok_)))
    print(f"  {'PASS' if ok_ else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok_ else ""))


def run(args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GALAXUS_")}
    env.update(PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
               PYTHONIOENCODING="utf-8", **(env_extra or {}))
    code = f"from galaxus_push import main\nimport sys\nsys.argv = {['x'] + list(args)!r}\nmain()\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    out = r.stdout + r.stderr
    print("    $ " + " ".join(args))
    for line in out.strip().splitlines()[:40]:
        print("    | " + line)
    return r.returncode, out


BOOKS = [
    {"id": "GX-NEW", "title": "Dragona", "price_eur": 8.5, "isbn": "9788409591923", "condition": "NewCondition"},
    {"id": "GX-ISBN10", "title": "Old ISBN", "price_eur": 20, "isbn": "84-206-5867-X", "condition": "NewCondition",
     "stock": 3},
    {"id": "GX-SOLD", "title": "Sold out", "price_eur": 10, "isbn": "9780000000002", "condition": "NewCondition",
     "sold_out": True},
    {"id": "GX-USED", "title": "Used", "price_eur": 5, "isbn": "9788410413290", "condition": "UsedCondition"},
    {"id": "GX-BADCHK", "title": "Bad check digit", "price_eur": 5, "isbn": "9788409591924", "condition": "NewCondition"},
    {"id": "GX-DUP", "title": "Same GTIN again", "price_eur": 9, "isbn": "978-84-09591-92-3", "condition": "NewCondition"},
    {"id": "GX-ÑOÑO", "title": "Non-ASCII key", "price_eur": 7, "isbn": "9780306406157", "condition": "NewCondition"},
    {"id": "GX-NOISBN", "title": "No ISBN", "price_eur": 7, "condition": "NewCondition"},
    {"id": "GX-NOPRICE", "title": "No price", "isbn": "9781861972712", "condition": "NewCondition"},
]


def unit_checks():
    check("ISBN-10 -> EAN-13, bad check digit rejected",
          gx.to_gtin("84-206-5867-X") == "9788420658674" and gx.to_gtin("9788409591924") is None
          and gx.to_gtin("978-84-09591-92-3") == "9788409591923")
    a = type("A", (), {"price_factor": 1.0, "chf_rate": 0.94})()
    ch = gx.price_excl_vat(8.5, a)                      # 8.50 EUR * 0.94 / 1.026 = 7.787 -> 7.79 CHF
    old = gx.MARKET
    gx.MARKET = "DE"
    de = gx.price_excl_vat(10.70, a)                    # 10.70 / 1.07 = 10.00 EUR, no CHF rate
    gx.MARKET = old
    check("price excl. VAT: CH 2.6% in CHF, DE 7% in EUR, half-up", ch == "7.79" and de == "10.00", f"{ch} {de}")
    check("text fields quoted, inner quotes doubled, no line breaks", gx.q('a "b"\nc') == '"a ""b"" c"')


def test_galaxus():
    unit_checks()
    with tempfile.TemporaryDirectory() as srv_dir, tempfile.TemporaryDirectory() as tmp:
        S.reset(srv_dir)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        threading.Thread(target=serve, args=(sock,), daemon=True).start()
        port = sock.getsockname()[1]
        env = {"GALAXUS_SFTP_HOST": "127.0.0.1", "GALAXUS_SFTP_PORT": str(port), "GALAXUS_SFTP_USER": USER,
               "GALAXUS_SFTP_PASSWORD": PASSWORD, "GALAXUS_PROVIDER": "TestShop",
               "GALAXUS_SFTP_HOSTKEY": f"ssh-rsa {HOST_KEY.get_base64()}"}
        pf = str(Path(tmp) / "books.json")
        Path(pf).write_text(json.dumps(BOOKS, ensure_ascii=False), encoding="utf-8")
        served = Path(srv_dir) / FEED

        rc, out = run(["push", pf, "--dry-run"], tmp)
        preview = (Path(tmp) / "galaxus_feed.csv")
        check("dry run without credentials writes galaxus_feed.csv, connects nowhere",
              rc == 0 and preview.exists() and S.auths == 0 and not served.exists(), out[-300:])

        rc, out = run(["check"], tmp, {k: v for k, v in env.items() if k != "GALAXUS_SFTP_HOSTKEY"})
        check("unknown host key: refused BEFORE the password is sent",
              rc != 0 and "unknown host key" in out and S.auths == 0, out[-300:])

        rc, out = run(["check"], tmp, dict(env, GALAXUS_SFTP_PASSWORD="wrong"))
        check("wrong password: clear message with the 3-hour IP block warning",
              rc != 0 and "rejected the user name/password" in out and "3 hours" in out, out[-300:])

        rc, out = run(["check"], tmp, env)
        check("check: login OK, feed file not there yet", rc == 0 and "SFTP login OK" in out
              and "is not there yet" in out, out[-300:])

        rc, out = run(["push", pf, "--yes"], tmp, env)
        raw = served.read_bytes() if served.exists() else b""
        rows, errors = import_feed(raw)
        by = {r["ProviderKey"]: r for r in rows}
        check("upload written as tmp_ file, then renamed over the feed name",
              S.opened == ["tmp_" + FEED] and S.renames == [("tmp_" + FEED, FEED, "posix")]
              and not (Path(srv_dir) / ("tmp_" + FEED)).exists(), f"{S.opened} {S.renames}")
        check("Galaxus import of the feed: no format errors", raw and not errors, errors)
        check("feed has exactly the valid new books (used, bad check digit, duplicate GTIN, non-ASCII key, "
              "no ISBN, no price skipped)", sorted(by) == ["GX-ISBN10", "GX-NEW", "GX-SOLD"], sorted(by))
        check("header: currency and VAT columns for galaxus.ch",
              raw.decode().splitlines()[0] == "ProviderKey;MainGtin;ManufacturerKey;PriceExclVat_CHF;VatRatePercentage;QuantityOnStock")
        check("prices CHF excl. 2.6% VAT, ISBN-10 converted, stock from source",
              by.get("GX-NEW", {}).get("PriceExclVat_CHF") == "7.79" and by.get("GX-ISBN10", {}).get("MainGtin") == "9788420658674"
              and by.get("GX-ISBN10", {}).get("QuantityOnStock") == "3"
              and by.get("GX-ISBN10", {}).get("PriceExclVat_CHF") == str((Decimal(20) * Decimal("0.94") / Decimal("1.026")).quantize(Decimal("0.01"))),
              by)
        check("sold-out book kept with stock 0 by default", by.get("GX-SOLD", {}).get("QuantityOnStock") == "0", by)
        state = json.loads((Path(tmp) / "galaxus_posted.json").read_text(encoding="utf-8"))
        upload = json.loads((Path(tmp) / "galaxus_upload.json").read_text(encoding="utf-8"))
        check("state: one entry per offer (push_all counts them), upload hash kept apart",
              sorted(state) == ["GX-ISBN10", "GX-NEW", "GX-SOLD"] and upload.get("file") == FEED
              and len(upload.get("sha256", "")) == 64, state)

        n = len(S.opened)
        rc, out = run(["push", pf, "--yes"], tmp, env)
        check("unchanged feed: nothing uploaded", rc == 0 and "nothing uploaded" in out and len(S.opened) == n, out[-200:])

        rc, out = run(["push", pf, "--yes", "--force"], tmp, env)
        check("--force uploads the unchanged feed again", rc == 0 and len(S.opened) == n + 1, out[-200:])

        BOOKS[0]["price_eur"] = 9.5
        Path(pf).write_text(json.dumps(BOOKS, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", pf, "--yes"], tmp, env)
        rows, errors = import_feed(served.read_bytes())
        by = {r["ProviderKey"]: r for r in rows}
        after = json.loads((Path(tmp) / "galaxus_posted.json").read_text(encoding="utf-8"))
        check("price change: re-uploaded, only that offer's state entry changes",
              "1 changed" in out and by["GX-NEW"]["PriceExclVat_CHF"] == "8.70" and not errors
              and [k for k in after if after[k] != state.get(k)] == ["GX-NEW"], out[-300:])

        S.no_posix_rename = True
        S.fail_writes = 1
        rc, out = run(["push", pf, "--yes", "--drop-sold-out"], tmp, env)
        rows, errors = import_feed(served.read_bytes())
        check("server without posix-rename: remove + rename fallback, after one failed try",
              rc == 0 and "retrying" in out and S.renames[-1] == ("tmp_" + FEED, FEED, "rename"), out[-300:])
        check("--drop-sold-out: removed from the assortment file and the state",
              sorted(r["ProviderKey"] for r in rows) == ["GX-ISBN10", "GX-NEW"] and "GX-SOLD: no longer in the feed" in out
              and "GX-SOLD" not in json.loads((Path(tmp) / "galaxus_posted.json").read_text(encoding="utf-8")),
              out[-300:])
        S.no_posix_rename = False

        empty = str(Path(tmp) / "empty.json")
        Path(empty).write_text(json.dumps([BOOKS[3]]), encoding="utf-8")
        size = served.stat().st_size
        rc, out = run(["push", empty, "--yes"], tmp, env)
        check("empty feed refused (would take every offer offline)", rc != 0 and "EMPTY" in out
              and served.stat().st_size == size, out[-200:])

        rc, out = run(["status"], tmp, env)
        check("status: last feed contents and the file on the server",
              rc == 0 and "2 offer(s)" in out and f"On the SFTP server: {FEED}" in out, out[-300:])

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "books.json").write_text(json.dumps(BOOKS, ensure_ascii=False), encoding="utf-8")
        rc, out = run(["push", str(Path(tmp, "books.json")), "--dry-run", "--limit", "1"], tmp)
        rows, errors = import_feed(Path(tmp, "galaxus_feed.csv").read_bytes())
        check("--limit 1: only one new offer in the feed", len(rows) == 1 and "1 new" in out, out[-200:])
    sock.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_galaxus()
    failed = [n for n, ok_ in RESULTS if not ok_]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
