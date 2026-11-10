#!/usr/bin/env python3
"""
Test for order handling in stock_sync.py and the "Orders" section of
push_all's email:
  * orders that must be accepted are accepted automatically when the stock
    covers them: Mirakl OR21 PUT /api/orders/{id}/accept (local HTTP mock,
    every waiting line in the body), Cdiscount POST /orders/{id}/approval-status
    and eMAG order/acknowledge/{id} (fake clients);
  * first come, first served: a second sale of the same copy is a DOUBLE SALE
    and is NOT accepted;
  * marketplaces without an automatic accept (CDON) and failed accepts are
    flagged "accept by hand"; a failed accept is tried again next run;
  * orders_report.json: new orders + everything that needs a person;
    marked as reported only after the email went out;
  * push_all: Orders section in the text and HTML report, subject warns.
Nothing is sent anywhere.

    python tests/test_orders.py
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
RESULTS = []
ACCEPTS = []


def ok(cond, name):
    RESULTS.append((bool(cond), name))
    print(("PASS " if cond else "FAIL ") + name)


class Mirakl(BaseHTTPRequestHandler):
    fail_next = False

    def log_message(self, *a):
        pass

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        ok_auth = self.headers.get("Authorization") == "mirakl-key" and "json" in (self.headers.get("Content-Type") or "")
        ACCEPTS.append(("mirakl", self.path, body))
        if Mirakl.fail_next or not ok_auth:
            Mirakl.fail_next = False
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"message":"ORDER_INVALID_STATE"}')
            return
        self.send_response(204)
        self.end_headers()


class FakeOctopia:
    def call(self, method, path, params=None, body=None, channel=False):
        ACCEPTS.append(("cdiscount", path, body))
        return type("R", (), {"status_code": 201})(), {}


class FakeEmag:
    def call(self, path, data=None, method="POST", params=None):
        ACCEPTS.append(("emag", path, data))
        return True, None, []


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Mirakl)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update(STOCK_SYNC_DIR=tmp, PUSH_ALL_RUNNING="1",
                          CASADELLIBRO_API_URL=f"http://127.0.0.1:{httpd.server_address[1]}",
                          CASADELLIBRO_API_KEY="mirakl-key")
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            run(tmp)
        finally:
            os.chdir(cwd)
    failed = [n for good, n in RESULTS if not good]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


def run(tmp):
    import cdiscount_push
    import emag_push
    import push_all
    import stock_sync as ss
    cdiscount_push.Octopia, emag_push.Emag = FakeOctopia, FakeEmag
    books = Path(tmp) / "books.json"
    books.write_text(json.dumps([{"id": "A", "title": "Libro A"}, {"id": "B", "title": "Libro B"},
                                 {"id": "C", "title": "Libro C"}, {"id": "D", "title": "Libro D"}]), encoding="utf-8")
    t0 = ss.iso(ss.utcnow())
    lines = []

    def mirakl_line(order, line, sku, state="WAITING_ACCEPTANCE"):
        s = ss.sale("casadellibro", f"casadellibro:{order}:{line}", sku, 1, False, t0, order=order,
                    needs_accept=state == "WAITING_ACCEPTANCE")
        s["line_id"] = line
        return s

    lines += [mirakl_line("M1", "M1-1", "A")]                                                    # first sale of A
    lines += [ss.sale("cdiscount", "cdiscount:C9:L1", "A", 1, False, ss.iso(ss.utcnow()), order="C9",
                      needs_accept=True)]                                                       # A again: double sale
    lines += [ss.sale("emag", "emag:501:1", "B", 1, False, t0, order=501, needs_accept=True)]
    lines += [ss.sale("cdon", "cdon:D7", "C", 1, False, t0, order="D7", needs_accept=True)]
    lines += [ss.sale("amazon", "amazon:A1:1", "D", 1, False, t0, order="A1")]
    lines[1]["at"] = ss.iso(ss.utcnow().replace(microsecond=0)) if lines[1]["at"] <= t0 else lines[1]["at"]
    ss.readers = lambda: [("all", lambda since: [dict(x) for x in lines], [])]
    args = lambda **k: argparse.Namespace(source=str(books), dry_run=k.get("dry", False), no_apply=True, verbose=False)

    def sync(**k):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ss.cmd_sync(args(**k))
        return buf.getvalue()

    def report():
        return {r["key"]: r for r in json.loads(Path(tmp, "orders_report.json").read_text(encoding="utf-8"))}

    out = sync(dry=True)
    ok(not ACCEPTS and "(dry run) would accept" in out, "dry run: accepts are only shown")

    Mirakl.fail_next = True
    out = sync()
    rep = report()
    ok("automatic accept failed" in out and rep["casadellibro:M1:M1-1"]["needs_you"]
       and "ACCEPT BY HAND" in rep["casadellibro:M1:M1-1"]["status"], "a failed accept is reported for a person")
    ok(("cdiscount", "/orders/C9/approval-status", {"approval_status": "Accepted"}) not in ACCEPTS
       and "DOUBLE SALE" in rep["cdiscount:C9:L1"]["status"] and rep["cdiscount:C9:L1"]["needs_you"]
       and "DOUBLE SALE" in out, "second sale of the same copy: DOUBLE SALE, not accepted")
    ok(("emag", "order/acknowledge/501", {}) in ACCEPTS and rep["emag:501:1"]["status"] == "accepted automatically",
       "eMAG: new order acknowledged")
    ok("ACCEPT BY HAND" in rep["cdon:D7"]["status"] and rep["cdon:D7"]["needs_you"],
       "CDON: needs acceptance, no automatic accept -> by hand")
    ok(rep["amazon:A1:1"]["status"] == "ok (no acceptance needed)" and not rep["amazon:A1:1"]["needs_you"]
       and rep["amazon:A1:1"]["title"] == "Libro D", "Amazon: paid at checkout, listed with its title")

    ACCEPTS.clear()
    out = sync()
    m = [x for x in ACCEPTS if x[0] == "mirakl"]
    ok(m and m[0][1] == "/api/orders/M1/accept" and m[0][2] == {"order_lines": [{"accepted": True, "id": "M1-1"}]}
       and report()["casadellibro:M1:M1-1"]["status"] == "accepted automatically",
       "next run: Mirakl order accepted (OR21, every waiting line)")
    ACCEPTS.clear()
    sync()
    ok(not [x for x in ACCEPTS if x[0] in ("mirakl", "emag")], "accepted orders are not accepted again")

    # email: section, subject, marking after the email went out
    rows = push_all.orders_rows()
    text = "\n".join(push_all.orders_text(rows))
    html_ = push_all.orders_html(rows)
    ok("ORDERS: 5 new, 2 NEED YOU" in text and "!!" in text and "DOUBLE SALE" in html_ and "Libro A" in text,
       "email text and HTML: orders section, the ones that need you first")
    res = [{"name": "x", "label": "Kaufland", "status": "OK", "pushed": 0, "problems": [], "tail": [], "skipped": 0,
            "already": 0, "note": "", "log": None}]
    ok("2 order(s) need you" in push_all.subject_of(res, False) and "PROBLEMS" in push_all.subject_of(res, False),
       "subject warns when orders need you")
    ss.mark_reported([r["key"] for r in rows if r["new"]])
    sync()
    left = report()
    ok(set(left) == {"cdiscount:C9:L1", "cdon:D7"} and not any(r["new"] for r in left.values()),
       "after the email: only the orders that still need you stay in the report")

    lines[3]["cancelled"], lines[3]["needs_accept"] = True, False
    sync()
    ok("cdon:D7" not in report(), "a cancelled order no longer needs you")


if __name__ == "__main__":
    main()
