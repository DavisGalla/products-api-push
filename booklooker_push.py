#!/usr/bin/env python3
"""
Push books to booklooker.de (Germany) through the official booklooker REST
API 2.0 (https://www.booklooker.de/pages/rest_api.php, gateway
https://api.booklooker.de/2.0).

booklooker lists your own copy of each book (like AbeBooks): the script
writes all books into one tab-separated article file in the booklooker
format (the columns of booklooker's own upload template), uploads it with
file_import (mode "add/change"), waits until booklooker has processed it, and
reads the rejected lines (file_status showErrors). Uploading the same
order number again replaces that offer, so re-sending is safe. New books are
completed from the German VLB catalog by ISBN.

ONE-TIME SETUP (see docs/booklooker_push.md)
  booklooker.de -> Mein Konto -> API-Key: generate your personal API key.
  The account must be verified, with tax data, shipping costs for books and
  payment methods (check them with `check`). New books may only be offered
  by commercial sellers. Then (PowerShell), or as a Bitwarden secret:
      $env:BOOKLOOKER_API_KEY = "..."

USAGE
    pip install requests
    python booklooker_push.py check                 # API key, account ready to sell books?
    python booklooker_push.py push --dry-run        # build the article file, send nothing
    python booklooker_push.py push --limit 1        # one real offer
    python booklooker_push.py push                  # everything
    python booklooker_push.py push --resend --delete-sold-out   # sync prices, delete sold-out
    python booklooker_push.py status                # active / sold / deleted

NOTE: new books in Germany have a legally fixed price (Buchpreisbindung).
booklooker rejects new books whose price differs from the fixed price.
"""
import argparse
import io
import json
import os
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

# Source condition -> (booklooker "Zustand" 1 = like new ... 4 = heavily worn, "Neuware?").
# Conditions that aren't listed are skipped.
CONDITION_MAP = {
    "NewCondition": ("1", "ja"),
    "UsedCondition": ("2", "nein"),
    "RefurbishedCondition": ("2", "nein"),
    "DamagedCondition": ("4", "nein"),
}
LANGUAGE = "Spanisch"        # language of your books, as shown on booklooker ("Sprache")
CATEGORY_NO = ""             # optional booklooker category number ("Sparten-Nr."), see the template
PRICE_DECIMAL = ","          # decimal separator for prices in the file ("8,50"); "." if booklooker asks for it
DEFAULT_WEIGHT_G = 400       # parcel weight when the product has no "weight_kg"
# ======================================================================

API_URL = "https://api.booklooker.de/2.0"
STATE_FILE = Path("booklooker_posted.json")
PREVIEW_FILE = Path("booklooker_articles.txt")
MEDIA_BOOKS = 0
# Column order of booklooker's books template (booklooker_vorlage, sheet "Bücher"), formatID 1.
COLUMNS = ["Sparten-Nr.", "Autor", "Titel", "Verlag", "Auflage", "Jahr", "Ort", "Einband", "Zustand (1-4)",
           "Beschreibung", "Sprache", "ISBN", "Seiten", "Format", "Bestell-Nr", "Gewicht in g", "Ihr Preis in €",
           "unbenutzt", "unbenutzt", "Cover-URL", "Stichwort", "unbegrenzte Stückzahl?", "Neuware?",
           "Erstausgabe?", "Signiert?", "unbenutzt", "Persönliche Notiz"]
ERRORS = {"1": "duplicate order number", "2": "duplicate identical offers", "4": "new books in a private account",
          "5": "missing format", "6": "audiobook in a books file", "8": "unsupported article type",
          "10": "title is empty", "11": "price or currency missing", "12": "new-book price differs from the fixed price",
          "13": "more than 5000 VLB articles", "14": "e-books not allowed", "15": "no data for this ISBN",
          "16": "FSK 18", "17": "maximum number of articles exceeded", "18": "publication date in the future"}


class ApiError(Exception):
    def __init__(self, interface, code):
        super().__init__(f"{interface}: {code}")
        self.interface, self.code = interface, code


# --------------------------------------------------------------------------- API

class Booklooker:
    def __init__(self):
        self.key = os.environ.get("BOOKLOOKER_API_KEY")
        if not self.key:
            sys.exit("Missing environment variables: BOOKLOOKER_API_KEY")
        self.base = (os.environ.get("BOOKLOOKER_API_URL") or API_URL).rstrip("/")
        self.token, self.used = None, 0

    def raw(self, method, interface, params=None, files=None, retries=4):
        for attempt in range(retries):
            r = requests.request(method, f"{self.base}/{interface}", params=params, files=files, timeout=300)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            try:
                data = r.json()
            except ValueError:
                raise ApiError(interface, f"HTTP {r.status_code}: {r.text[:200]}")
            if data.get("status") == "OK":
                return data.get("returnValue")
            code = data.get("returnValue")
            if code == "QUOTA_EXCEEDED":
                print("   (booklooker: too many calls per minute, waiting 60s)")
                time.sleep(60)
                continue
            raise ApiError(interface, code)
        raise ApiError(interface, "gave up after retries")

    def current_token(self):
        if self.token and time.time() - self.used < 9 * 60:         # unused tokens expire after 10 minutes
            self.used = time.time()
            return self.token
        try:
            self.token = self.raw("POST", "authenticate", params={"apiKey": self.key})
        except ApiError as e:
            if e.code == "AUTHENTICATION_FAILED":
                sys.exit("booklooker rejected the API key (AUTHENTICATION_FAILED). Check BOOKLOOKER_API_KEY.")
            raise
        self.used = time.time()
        return self.token

    def call(self, method, interface, params=None, files=None):
        for again in (True, False):
            try:
                return self.raw(method, interface, dict(params or {}, token=self.current_token()), files)
            except ApiError as e:
                if again and e.code in ("TOKEN_EXPIRED", "TOKEN_UNKNOWN"):
                    self.token = None
                    continue
                raise

    def account(self):
        return self.call("GET", "account", {"mediaType": MEDIA_BOOKS})

    def upload(self, name, content):
        return self.call("POST", "file_import", {"fileType": "article", "dataType": 0, "mediaType": MEDIA_BOOKS,
                                                 "formatID": 1, "encoding": "UTF-8"},
                         files={"file": (name, content, "text/plain")})

    def file_status(self, name, errors=False):
        return self.call("GET", "file_status", {"filename": name, "showErrors": 1 if errors else 0})

    def article_status(self, order_no):
        return self.call("GET", "article_status", {"orderNo": order_no})

    def delete(self, order_no):
        return self.call("DELETE", "article", {"orderNo": order_no})


# ---------------------------------------------------------------------- helpers

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def to_isbn(code):
    """Normalise an ISBN-10/13 or EAN to a 13-digit ISBN/EAN, or return None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    if len(c) == 13 and c.isdigit():
        return c
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        core = "978" + c[:9]
        check = (10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10
        return core + str(check)
    return None


def money(price, factor=1.0):
    """price * factor rounded half-up to cents, as text with PRICE_DECIMAL (8.075 -> '8,08')."""
    v = (Decimal(str(price)) * Decimal(str(factor))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(v).replace(".", PRICE_DECIMAL)


def cell(value):
    """Tab-separated file: no tabs or line breaks inside a field."""
    return " ".join(str(value if value is not None else "").replace("\t", " ").split())


def normalize(raw):
    images = raw.get("images") or ([raw["image"]] if raw.get("image") else [])
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:50],
        "title": (raw.get("title") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "price": raw.get("price_eur", raw.get("price")),
        "isbn": to_isbn(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "author": raw.get("author") or "",
        "publisher": raw.get("publisher") or raw.get("brand") or "",
        "year": raw.get("year") or "",
        "binding": raw.get("binding") or "",
        "pages": raw.get("pages") or "",
        "weight_g": int(float(raw["weight_kg"]) * 1000) if raw.get("weight_kg") else DEFAULT_WEIGHT_G,
        "image": next((u for u in images if str(u).startswith("http")), ""),
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    if not p["title"]:
        probs.append("no title")
    if p["raw_code"] and not p["isbn"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in CONDITION_MAP:
        probs.append(f"no booklooker condition for '{p['condition']}' (set CONDITION_MAP)")
    if p["condition"] in CONDITION_MAP and CONDITION_MAP[p["condition"]][1] == "ja" and not p["isbn"]:
        probs.append("new books need an ISBN (booklooker completes them from the VLB catalog)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def row(p, a):
    zustand, neu = CONDITION_MAP[p["condition"]]
    values = {
        "Sparten-Nr.": CATEGORY_NO, "Autor": p["author"], "Titel": p["title"][:256], "Verlag": p["publisher"],
        "Jahr": p["year"], "Einband": p["binding"], "Zustand (1-4)": zustand, "Beschreibung": p["description"],
        "Sprache": LANGUAGE, "ISBN": p["isbn"] or "", "Seiten": p["pages"], "Bestell-Nr": p["sku"],
        "Gewicht in g": p["weight_g"], "Ihr Preis in €": money(p["price"], a.price_factor), "Cover-URL": p["image"],
        "unbegrenzte Stückzahl?": "nein", "Neuware?": neu, "Erstausgabe?": "nein", "Signiert?": "nein",
    }
    return "\t".join("" if col == "unbenutzt" else cell(values.get(col, "")) for col in COLUMNS)


def article_file(products, a):
    return ("\t".join(COLUMNS) + "\n" + "\n".join(row(p, a) for p in products) + "\n").encode("utf-8")


def parse_errors(text):
    """file_status showErrors=1: one line per rejected article: errorID, record, orderNo, title, details."""
    out = {}
    for line in str(text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].strip().isdigit():
            eid = parts[0].strip()
            details = parts[4].strip() if len(parts) > 4 else ""
            out[parts[2].strip()] = f"error {eid} ({ERRORS.get(eid, 'see booklooker')})" + (f": {details}" if details else "")
    return out


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    acc = api.account()
    print(f"API key accepted: booklooker account '{acc.get('username')}' (seller {acc.get('sellerId')})")
    if acc.get("readyToSell"):
        print("   ready to sell books.")
    else:
        print("   ! NOT ready to sell books: " + ", ".join(acc.get("missing") or ["unknown reason"]))


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()

    send, gone = [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and a.delete_sold_out and p["sku"] in state and state[p["sku"]].get("status") != "deleted":
            gone.append(p)
            continue
        probs = problems_of(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if p["sku"] in state:
            if not a.resend:
                print(f"[{i}] already pushed: {p['title']}")
                continue
            if money(p["price"], a.price_factor) == state[p["sku"]].get("price") and state[p["sku"]].get("status") != "deleted":
                print(f"   = {p['sku']}: unchanged")
                continue
        send.append(p)
    new_count = sum(1 for p in send if p["sku"] not in state)
    if a.limit:
        keep = [p for p in send if p["sku"] in state]
        keep += [p for p in send if p["sku"] not in state][: a.limit]
        send = keep
        new_count = min(new_count, a.limit)

    print(f"{new_count} new offer(s), {len(send) - new_count} update(s)" + (f", {len(gone)} sold-out to delete" if gone else "") + ".")
    if not (send or gone):
        return
    content = article_file(send, a)
    PREVIEW_FILE.write_bytes(content)
    if a.dry_run:
        print(f"Dry run: article file written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(send) + len(gone)} LIVE offer(s) on booklooker. Type 'yes': ").strip().lower() != "yes":
            return
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")

    if send:
        acc = api.account()
        if not acc.get("readyToSell"):
            sys.exit("booklooker account is not ready to sell books: " + ", ".join(acc.get("missing") or []) +
                     ". Fix it on booklooker.de (see `check`).")
        name = time.strftime("products_%Y%m%d_%H%M%S.txt")
        api.upload(name, content)
        print(f"Uploaded {name} ({len(send)} offer(s)), waiting for booklooker to process it...")
        deadline = time.time() + a.wait
        while True:                                 # always check at least once
            st = api.file_status(name)
            if st == "UPLOAD_DONE" or time.time() >= deadline:
                break
            time.sleep(a.poll)
        if st != "UPLOAD_DONE":
            print(f"{name} is still {st} after {a.wait}s. Nothing recorded; run push again later (re-uploading is safe).")
        else:
            errors = parse_errors(api.file_status(name, errors=True))
            ok = 0
            for p in send:
                if p["sku"] in errors:
                    print(f"   ! {p['sku']}: {errors[p['sku']]}")
                    continue
                state[p["sku"]] = {"isbn": p["isbn"], "price": money(p["price"], a.price_factor),
                                   "condition": p["condition"], "file": name, "status": "active", "pushed_at": now()}
                ok += 1
                print(f"   + {p['sku']}: {money(p['price'], a.price_factor)} EUR  ({p['condition']})")
            save_state(state)
            print(f"{name}: {ok} offer(s) accepted, {len(errors)} rejected.")

    for p in gone:
        try:
            api.delete(p["sku"])
        except ApiError as e:
            if e.code != "NOT_FOUND":
                print(f"   ! {p['sku']}: could not delete: {e.code}")
                continue
        state[p["sku"]].update(status="deleted", updated_at=now())
        save_state(state)
        print(f"   - {p['sku']}: sold out, deleted on booklooker (within 2 hours)")
    print("Done. Run `status` to see your offers.")


def cmd_status(api, a):
    state = load_state()
    if not state:
        print("No offers yet.")
        return
    for sku in sorted(state):
        try:
            st = api.article_status(sku)
        except ApiError as e:
            st = "not found on booklooker" if e.code == "NOT_FOUND" else e.code
        print(f"{sku}: {st}  price={state[sku].get('price')} EUR")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to booklooker.de (REST API 2.0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the API key and whether the account can sell books")
    p = sub.add_parser("push", help="upload offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--resend", action="store_true", help="re-upload already pushed offers whose price changed")
    p.add_argument("--delete-sold-out", action="store_true", help="delete pushed offers that are now sold out")
    p.add_argument("--dry-run", action="store_true", help="write booklooker_articles.txt, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--wait", type=int, default=900, help="max seconds to wait for booklooker to process the file")
    p.add_argument("--poll", type=int, default=15, help="seconds between file status checks")
    sub.add_parser("status", help="active / sold / deleted for every pushed offer")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run and not os.environ.get("BOOKLOOKER_API_KEY"):
        return cmd_push(None, a)
    api = Booklooker()
    try:
        {"check": cmd_check, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except ApiError as e:
        sys.exit(f"booklooker API error {e}")


if __name__ == "__main__":
    main()
