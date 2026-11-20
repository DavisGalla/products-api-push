#!/usr/bin/env python3
"""
Safety tests for stock_sync.py: the cases where a double sale or a wrongly emptied listing could slip through.

  * a pusher that reports "! stock update failed" but exits normally: the change is sent again, not forgotten
  * an unreadable (half-written) books.json: the run stops, books_synced.json is not overwritten
  * books that share an ISBN are one stock: a copy sold under one SKU takes down its twin SKU, and a second
    sale of the twin is a double sale
  * the eBay order reader refuses sandbox keys before anything is sent

Fake order readers and a fake pusher runner stand in for the marketplaces; everything runs in temporary
folders and nothing touches the network or the project folder.

    python tests/test_stock_safety.py
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="stock_safety_"))
os.environ["STOCK_SYNC_DIR"] = str(TMP)
os.environ["PUSH_ALL_RUNNING"] = "1"
for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN"):
    os.environ[k] = "dummy"
sys.path.insert(0, str(ROOT))
import push_all  # noqa: E402
import stock_sync  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def now(minutes=0):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


_n = [0]


def fresh(books, enabled=("ebay",)):
    """A new data folder; books.json scraped 3 hours ago; the given pushers enabled in the config."""
    _n[0] += 1
    d = TMP / f"s{_n[0]}"
    d.mkdir()
    for attr, name in (("LEDGER_FILE", "stock_ledger.json"), ("SYNCED_FILE", "books_synced.json"),
                       ("CHANGES_FILE", "books_sync_changes.json"), ("ORDERS_FILE", "orders_report.json")):
        setattr(stock_sync, attr, d / name)
    b = d / "books.json"
    b.write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
    t = time.time() - 3 * 3600
    os.utime(b, (t, t))
    cfg = d / "push_all_config.json"
    cfg.write_text(json.dumps({"json_file": str(b), "pushers": {n: {"enabled": True, "args": []} for n in enabled}}),
                   encoding="utf-8")
    push_all.CONFIG_FILE = cfg
    return d, b


class FakeRun:
    """Stands in for subprocess.run: records pusher command lines, answers with a scripted output."""
    def __init__(self, stdout="ok\n", rc=0):
        self.cmds, self.stdout, self.rc = [], stdout, rc

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        return type("P", (), {"stdout": self.stdout, "returncode": self.rc})()


def sync(books_path, reader=None, channel="ebay", **flags):
    stock_sync.readers = lambda: [(channel, reader or (lambda since: []), [])]
    a = argparse.Namespace(source=str(books_path), dry_run=False, no_apply=False, verbose=False, **flags)
    buf, code = io.StringIO(), None
    with contextlib.redirect_stdout(buf):
        try:
            stock_sync.cmd_sync(a)
        except SystemExit as e:
            code = e.code
    return buf.getvalue(), code


def sale_of(sku, key="ebay:E1:1", channel="ebay", ean=None, minutes=-60, **kw):
    return lambda since: [stock_sync.sale(channel, key, sku, 1, False, stock_sync.iso(now(minutes)), ean=ean, **kw)]


def ledger_of(d):
    return json.loads((d / "stock_ledger.json").read_text(encoding="utf-8"))


def synced_of(d):
    return {b["id"]: b for b in json.loads((d / "books_synced.json").read_text(encoding="utf-8"))}


stock_sync.time.sleep = lambda s: None
BOOK = {"id": "B1", "title": "Uno", "price_eur": 10}

# ---------------------------------------------------------------------------------------------------------
print("\n=== a pusher that reports problems but exits normally: the change is sent again ===")
d, books = fresh([BOOK])
fake = FakeRun(stdout="   ! stock update failed: 429 throttled\n", rc=0)
stock_sync.subprocess.run = fake
out, code = sync(books, sale_of("B1"))
check("run 1: pusher started, its problem is reported, the run does not fail",
      len(fake.cmds) == 1 and "PROBLEMS" in out and "reported problems" in out and code is None, out[-300:])
check("the change is NOT recorded as sent (marketplaces still have stock 1)", ledger_of(d)["sent"].get("B1") == 1,
      str(ledger_of(d)["sent"]))
out, code = sync(books, sale_of("B1"))
check("run 2: the same change is sent again", len(fake.cmds) == 2 and "1 stock change(s) to send" in out, out[-300:])
fake.stdout = "all fine\n"
out, code = sync(books, sale_of("B1"))
check("run 3: the pusher is happy now, the change is recorded as sent",
      len(fake.cmds) == 3 and ledger_of(d)["sent"].get("B1") == 0, str(ledger_of(d)["sent"]))
out, code = sync(books, sale_of("B1"))
check("run 4: nothing left to send", len(fake.cmds) == 3 and "0 stock change(s) to send" in out, out[-300:])

d, books = fresh([BOOK])
fake = FakeRun(stdout="boom\n", rc=1)
stock_sync.subprocess.run = fake
out, code = sync(books, sale_of("B1"))
sync(books, sale_of("B1"))
check("a pusher that exits with an error is still retried and the run still fails",
      code == 1 and len(fake.cmds) == 2 and ledger_of(d)["sent"].get("B1") == 1, f"{code} {len(fake.cmds)}")

# ---------------------------------------------------------------------------------------------------------
print("\n=== an unreadable books.json must not wipe books_synced.json ===")
good = [{"id": f"G{i}", "title": f"Libro {i}", "price_eur": 9, "isbn": f"97884095919{i}3"[:13]} for i in range(5)]
d, books = fresh(good)
stock_sync.subprocess.run = FakeRun()
sync(books)
check("a good file: books_synced.json has all 5 books", len(synced_of(d)) == 5)
ledger_before = (d / "stock_ledger.json").read_text(encoding="utf-8")
text = books.read_text(encoding="utf-8")
books.write_text(text[: len(text) // 2], encoding="utf-8")                 # the scraper is mid-write
out, code = sync(books, sale_of("G0"))
check("a cut-off file: the run stops with a clear message", code and "unreadable" in str(code), str(code))
check("... books_synced.json and the ledger are unchanged",
      len(synced_of(d)) == 5 and (d / "stock_ledger.json").read_text(encoding="utf-8") == ledger_before)
for label, content in (("an empty list", "[]"), ("an empty object", "{}"), ("a products object without books", '{"products": []}'),
                       ("text that is not JSON", "<html>")):
    books.write_text(content, encoding="utf-8")
    out, code = sync(books)
    check(f"{label}: stops too, nothing overwritten", code and len(synced_of(d)) == 5, str(code))
books.write_text(text, encoding="utf-8")
out, code = sync(books)
check("the finished file is read normally on the next run", code is None and len(synced_of(d)) == 5, str(code))

# ---------------------------------------------------------------------------------------------------------
print("\n=== books with the same ISBN are one stock ===")
TWIN_ISBN = "9788410413290"
books_list = [{"id": "D1", "title": "Twin 1", "isbn": TWIN_ISBN}, {"id": "D2", "title": "Twin 2", "isbn": "978-84-10413-29-0"},
              {"id": "U1", "title": "Other book", "isbn": "9788409591923"}, {"id": "N1", "title": "No ISBN"}]
d, books = fresh(books_list)
stock_sync.subprocess.run = FakeRun()
sync(books, sale_of("D1"))
s = synced_of(d)
check("a sale of D1 takes down its twin D2 as well (same ISBN, written differently)",
      s["D1"].get("sold_out") and s["D2"].get("sold_out"), str({k: v.get("sold_out") for k, v in s.items()}))
check("other books are untouched", not s["U1"].get("sold_out") and not s["N1"].get("sold_out"))

d, books = fresh(books_list)
stock_sync.subprocess.run = FakeRun()
sync(books, sale_of("", key="bol:O1:I1", channel="bol", ean=TWIN_ISBN))
s = synced_of(d)
check("a sale reported by EAN only (bol style) takes down both twins", s["D1"].get("sold_out") and s["D2"].get("sold_out"))

d, books = fresh(books_list)
stock_sync.subprocess.run = FakeRun()
sync(books, sale_of("U1"))
s = synced_of(d)
check("a sale of a book with a unique ISBN affects only that book",
      s["U1"].get("sold_out") and not s["D1"].get("sold_out") and not s["D2"].get("sold_out") and not s["N1"].get("sold_out"))
d, books = fresh(books_list)
stock_sync.subprocess.run = FakeRun()
sync(books, sale_of("N1"))
check("a book without an ISBN is its own stock", synced_of(d)["N1"].get("sold_out") and not synced_of(d)["D1"].get("sold_out"))

# a second sale of the twin copy is a double sale, and is not accepted automatically
accepted = []
stock_sync.accept_order = lambda ch, oid, lines: (accepted.append((ch, oid)), None)[1]


def twin_sales(sku_a, sku_b):
    """An order for sku_a on a Mirakl shop, then an order for sku_b on Cdiscount; both wait for acceptance."""
    def reader(since):
        first = stock_sync.sale("casadellibro", "casadellibro:M1:L1", sku_a, 1, False, stock_sync.iso(now(-120)),
                                order="M1", needs_accept=True)
        first["line_id"] = "L1"
        second = stock_sync.sale("cdiscount", "cdiscount:C1:L1", sku_b, 1, False, stock_sync.iso(now(-60)),
                                 order="C1", needs_accept=True)
        return [first, second]
    return reader


d, books = fresh(books_list)
stock_sync.subprocess.run = FakeRun()
accepted.clear()
out, code = sync(books, twin_sales("D1", "D2"))
check("sold under D1 on one marketplace and under D2 on another: the second is a DOUBLE SALE, not accepted",
      "DOUBLE SALE" in out and "cdiscount order C1" in out and ("cdiscount", "C1") not in accepted, out[-400:])
check("... while the first order is accepted", ("casadellibro", "M1") in accepted, str(accepted))

two = [{"id": "T1", "title": "T1", "isbn": TWIN_ISBN, "stock": 2}, {"id": "T2", "title": "T2", "isbn": TWIN_ISBN}]
d, books = fresh(two)
stock_sync.subprocess.run = FakeRun()
accepted.clear()
out, code = sync(books, twin_sales("T1", "T2"))
check("when one of the twins really has 2 copies, two sales are both fine (pool = the best-stocked member)",
      "DOUBLE SALE" not in out and ("casadellibro", "M1") in accepted and ("cdiscount", "C1") in accepted, out[-300:])

# ---------------------------------------------------------------------------------------------------------
print("\n=== the eBay order reader refuses sandbox keys ===")
import ebay_push  # noqa: E402
import requests  # noqa: E402
posts = []
real_post = requests.post


def fake_post(url, **kw):
    posts.append(url)
    raise AssertionError("nothing may be sent to eBay with sandbox keys")


requests.post = fake_post
ebay_push.requests.post = fake_post
try:
    os.environ["EBAY_CLIENT_ID"] = "App-name-SBX-0123456789-abcdef01"
    d, books = fresh([BOOK])
    out, code = sync(books, stock_sync.read_ebay, channel="ebay")
    check("sandbox App ID: the eBay channel reports the mix-up and nothing is sent",
          not posts and "SANDBOX key" in out and "ebay: ERROR" in out, out[-300:])
    check("... and the other channels would still run (the error is per channel)", code is None, str(code))
finally:
    requests.post = real_post
    ebay_push.requests.post = real_post
    os.environ["EBAY_CLIENT_ID"] = "dummy"

failed = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
for n in failed:
    print(f"  FAIL  {n}")
sys.exit(1 if failed else 0)
