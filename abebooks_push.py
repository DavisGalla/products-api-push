#!/usr/bin/env python3
"""
Push books to AbeBooks / IberLibro through the official AbeBooks Inventory
Update API (XML over TLS, https://inventoryupdate.abebooks.com:10027).

IberLibro.com is AbeBooks' Spanish site: books in your AbeBooks inventory are
listed there (and on the other AbeBooks sites) automatically.

Unlike Amazon/Fnac, AbeBooks listings describe YOUR copy of a book (title,
publisher, ISBN, condition, price, description), so books don't have to be in
a catalog first.

ONE-TIME SETUP (see docs/abebooks_push.md)
  1. An AbeBooks / IberLibro seller account.
  2. Your API key: seller account -> "Members Menu" / "Mi cuenta de vendedor"
     -> API key (request it from AbeBooks seller support if it isn't shown).
  3. Set environment variables (PowerShell):
        $env:ABEBOOKS_USERNAME = "your seller username / client PIN"
        $env:ABEBOOKS_API_KEY  = "your API key"

USAGE
    python abebooks_push.py push --dry-run          # write the XML, send nothing
    python abebooks_push.py push --limit 1          # one real listing
    python abebooks_push.py push                    # everything
    python abebooks_push.py push --resend           # re-send all (price/stock sync)
    python abebooks_push.py push --delete-sold-out  # also remove books now sold out at source
    python abebooks_push.py delete 142534 142550    # remove specific listings

AbeBooks has NO test environment: every non-dry-run request changes your real
inventory. New books in Spain have a legally fixed price (max 5% discount).
"""
import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

CURRENCY = "EUR"            # must match your AbeBooks/IberLibro account currency

# Source condition -> AbeBooks bookCondition keyword.
BOOK_CONDITIONS = {
    "NewCondition": "New",
    "UsedCondition": "Very Good",
    "RefurbishedCondition": "Good",
    "DamagedCondition": "Poor",
}
BOOKSELLER_CATALOGUE = ""   # optional: your own catalogue name for these books
# ======================================================================

API_URL = "https://inventoryupdate.abebooks.com:10027"
STATE_FILE = Path("abebooks_posted.json")
XML_FILE = Path("abebooks_request.xml")
BATCH_SIZE = 100            # API maximum per request
OK = "600"
RETRYABLE = {"100", "103", "105", "106", "111"}
CODE_HINTS = {
    "601": "Book id is not valid (vendorBookID must be 1-15 characters)",
    "602": "Book list too large",
    "603": "Illegal book transaction",
    "604": "Bad price",
    "606": "Bad book data (author, title or publisher required)",
    "104": "Invalid XML",
    "108": "Unauthorized action",
    "109": "Unknown action",
    "110": "User is invalid - wrong username or API key",
}


# --------------------------------------------------------------------------- API

class AbeBooks:
    def __init__(self):
        user, key = os.environ.get("ABEBOOKS_USERNAME"), os.environ.get("ABEBOOKS_API_KEY")
        missing = [k for k, v in (("ABEBOOKS_USERNAME", user), ("ABEBOOKS_API_KEY", key)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.user, self.key = user, key

    def request(self, books, retries=4):
        """Send up to 100 <Abebook> elements; return {vendorBookID: (code, message)}."""
        body = build_request(self.user, self.key, books)
        for attempt in range(retries):
            try:
                r = requests.post(API_URL, data=body, timeout=120,
                                  headers={"Content-Type": "text/xml; charset=ISO-8859-1"})
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"cannot reach {API_URL} ({e})")
                time.sleep(5 * 2 ** attempt)
                continue
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                raise RuntimeError(f"HTTP {r.status_code}, not XML: {r.text[:300]}")
            if root.tag == "requestError" or root.findtext("code") != OK and root.find("AbebookList") is None:
                code, msg = root.findtext("code"), (root.findtext("message") or "").strip()
                if code in RETRYABLE and attempt < retries - 1:
                    print(f"   (AbeBooks error {code} {msg}, retrying)")
                    time.sleep(5 * 2 ** attempt)
                    continue
                if code == "110":
                    sys.exit(f"AbeBooks login failed (110): {msg or CODE_HINTS['110']}\n"
                             "Check ABEBOOKS_USERNAME and ABEBOOKS_API_KEY.")
                raise RuntimeError(f"AbeBooks error {code}: {msg or CODE_HINTS.get(code, '')}")
            return {(b.findtext("vendorBookID") or "").strip(): ((b.findtext("code") or "").strip(),
                                                                 (b.findtext("message") or "").strip())
                    for b in root.iter("Abebook")}
        raise RuntimeError(f"gave up after {retries} retries")


def build_request(user, key, books):
    root = ET.Element("inventoryUpdateRequest", {"version": "1.0"})
    action = ET.SubElement(root, "action", {"name": "bookupdate"})
    ET.SubElement(action, "username").text = user
    ET.SubElement(action, "password").text = key
    lst = ET.SubElement(root, "AbebookList")
    lst.extend(books)
    return ET.tostring(root, encoding="ISO-8859-1")  # adds the ISO-8859-1 XML declaration


# ---------------------------------------------------------------------- helpers

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(raw):
    images = raw.get("images") or raw.get("image_urls") or ([raw["image"]] if raw.get("image") else [])
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": (raw.get("title") or "").strip(),
        "author": (raw.get("author") or "").strip(),
        "publisher": (raw.get("publisher") or raw.get("brand") or "").strip(),
        "price": raw.get("price_eur", raw.get("price")),
        "isbn": str(raw.get("isbn") or "").replace("-", "").replace(" ", ""),
        "description": (raw.get("description") or "").strip(),
        "condition": raw.get("condition") or "NewCondition",
        "binding": (raw.get("binding") or "").lower(),
        "year": str(raw.get("publish_year") or raw.get("year") or ""),
        "quantity": raw.get("quantity"),
        "images": [u for u in images if str(u).startswith("http")],
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    elif len(p["sku"]) > 15:
        probs.append(f"id '{p['sku']}' longer than 15 characters (AbeBooks vendorBookID limit)")
    if not p["price"]:
        probs.append("no price")
    if not (p["title"] or p["author"] or p["publisher"]):
        probs.append("needs a title, author or publisher")
    if p["condition"] not in BOOK_CONDITIONS:
        probs.append(f"no AbeBooks condition for '{p['condition']}' (see BOOK_CONDITIONS)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def book_xml(p, a, transaction):
    b = ET.Element("Abebook")

    def add(tag, text, limit=None, **attrs):
        if text not in (None, ""):
            ET.SubElement(b, tag, attrs).text = str(text)[:limit] if limit else str(text)

    add("transactionType", transaction)
    add("vendorBookID", p["sku"])
    add("author", p["author"], 750)
    add("title", p["title"], 750)
    add("publisher", p["publisher"], 750)
    add("price", f"{float(p['price']) * a.price_factor:.2f}", currency=CURRENCY)
    if p["binding"] in ("hard", "soft"):
        add("binding", "Hardcover" if p["binding"] == "hard" else "Softcover", type=p["binding"])
    add("booksellerCatalogue", BOOKSELLER_CATALOGUE, 750)
    add("description", p["description"], 4000)
    add("bookCondition", BOOK_CONDITIONS[p["condition"]])
    add("isbn", p["isbn"], 15)
    add("publishYear", p["year"] if p["year"].isdigit() and len(p["year"]) == 4 else "")
    ET.SubElement(b, "quantity", {"amount": str(min(int(p["quantity"] or a.quantity), 999))})
    if a.pictures and p["images"]:
        pl = ET.SubElement(b, "pictureList")
        for url in p["images"][:5]:
            ET.SubElement(pl, "pictureURL").text = url[:2000]
    return b


def delete_xml(sku):
    b = ET.Element("Abebook")
    ET.SubElement(b, "transactionType").text = "delete"
    ET.SubElement(b, "vendorBookID").text = sku
    return b


def send(api, items, state, label):
    """items: list of (sku, element, record_or_None). record None = delete."""
    ok = failed = 0
    for start in range(0, len(items), BATCH_SIZE):
        chunk = items[start:start + BATCH_SIZE]
        results = api.request([el for _, el, _ in chunk])
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for sku, _, record in chunk:
            code, msg = results.get(sku, ("?", "no result returned for this book"))
            if code == OK:
                ok += 1
                if record is None:
                    state.pop(sku, None)
                    print(f"   - {sku}: deleted")
                else:
                    state[sku] = dict(record, pushed_at=now)
                    print(f"   + {sku}: {msg or 'added or updated'}")
            else:
                failed += 1
                print(f"   ! {sku}: {code} {msg or CODE_HINTS.get(code, '')}")
        save_state(state)
        if start + BATCH_SIZE < len(items):
            time.sleep(1)
    print(f"Done: {ok} {label}, {failed} failed.")


# --------------------------------------------------------------------- commands

def cmd_push(a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()

    items = []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and a.delete_sold_out and p["sku"] in state:
            print(f"[{i}] DELETE '{p['title']}': sold out at source")
            items.append((p["sku"], delete_xml(p["sku"]), None))
            continue
        probs = problems_of(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if p["sku"] in state and not a.resend:
            print(f"[{i}] already pushed: {p['title']}")
            continue
        transaction = "update" if p["sku"] in state else "add"
        items.append((p["sku"], book_xml(p, a, transaction), {"isbn": p["isbn"], "title": p["title"][:80]}))
    if a.limit:
        items = items[: a.limit]
    print(f"{len(items)} book(s) to send.")
    if not items:
        return

    XML_FILE.write_bytes(build_request(os.environ.get("ABEBOOKS_USERNAME", "YOUR_USERNAME"), "API_KEY_HIDDEN",
                                       [el for _, el, _ in items]))
    if a.dry_run:
        print(f"Dry run: request written to {XML_FILE.resolve()} (API key hidden) - nothing sent.")
        return
    api = AbeBooks()
    if not a.yes:
        if input(f"This will change {len(items)} LIVE listing(s) on AbeBooks/IberLibro. Type 'yes': "
                 ).strip().lower() != "yes":
            return
    send(api, items, state, "sent")


def cmd_delete(a):
    api = AbeBooks()
    state = load_state()
    if not a.yes:
        if input(f"This will DELETE {len(a.skus)} listing(s) on AbeBooks/IberLibro. Type 'yes': ").strip().lower() != "yes":
            return
    send(api, [(s, delete_xml(s), None) for s in a.skus], state, "deleted")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to AbeBooks / IberLibro (Inventory Update API)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push", help="add/update listings from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="copies per listing, max 999 (per-item 'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--pictures", action="store_true",
                   help="send image URLs (only if you hold the rights - AbeBooks blocks others)")
    p.add_argument("--resend", action="store_true", help="also re-send already pushed books (price/stock sync)")
    p.add_argument("--delete-sold-out", action="store_true", help="delete pushed books now sold out at source")
    p.add_argument("--dry-run", action="store_true", help="write abebooks_request.xml, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max books this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    d = sub.add_parser("delete", help="delete listings by SKU (vendorBookID)")
    d.add_argument("skus", nargs="+")
    d.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    a = ap.parse_args()
    {"push": cmd_push, "delete": cmd_delete}[a.cmd](a)


if __name__ == "__main__":
    main()
