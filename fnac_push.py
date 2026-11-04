#!/usr/bin/env python3
"""
Push products to the Fnac marketplace (Fnac.es, Fnac.com, Fnac.pt...) through
the Fnac Marketplace seller API v2.6 (XML web services: auth, offers_update,
batch_status, offers_query).

Each product becomes an offer (price, stock, condition) on the EXISTING Fnac
catalog product with the same EAN/ISBN. Fnac rejects offers for products it
doesn't have; the script shows those errors and doesn't record them.

ONE-TIME SETUP (see docs/fnac_push.md)
  Fnac gives approved sellers three credentials - partner_id, shop_id and key -
  and the API URL for your country (ask Fnac Marketplace seller support, or
  look in your seller/Catalog Portal account). Then (PowerShell):
      $env:FNAC_API_URL    = "https://vendeur.fnac.com/api.php"   # the URL Fnac gives you
      $env:FNAC_PARTNER_ID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      $env:FNAC_SHOP_ID    = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      $env:FNAC_KEY        = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

USAGE
    pip install requests
    python fnac_push.py check                 # test the credentials
    python fnac_push.py push --dry-run        # write the XML, send nothing
    python fnac_push.py push --limit 1        # one real offer
    python fnac_push.py push                  # everything
    python fnac_push.py push --resend         # re-send all (price/stock sync)
    python fnac_push.py status                # what's live

NOTE: new books in Spain/France have a legally fixed price (max 5% discount).
Don't use --price-factor to sell above the cover price.
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

# Source condition -> Fnac product_state code (Fnac API 2.6):
#  11 new | 1 used, like new | 2 used, very good | 3 used, good | 4 used, acceptable
#  5-8 = the same for "uncommon"/collectible items
PRODUCT_STATES = {
    "NewCondition": 11,
    "UsedCondition": 2,
    "RefurbishedCondition": 1,
    "DamagedCondition": 4,
}
DEFAULT_COMMENT = ""        # offer comment buyers see (max 255 chars), e.g. "Envío en 24h"
BATCH_SIZE = 500            # offers per offers_update call
# ======================================================================

NS = "http://www.fnac.com/schemas/mp-dialog.xsd"
SANDBOX_URL = "https://marketplace.ws.fd-recette.net/api.php"
STATE_FILE = Path("fnac_posted.json")
XML_FILE = Path("fnac_offers.xml")
MIN_PRICE, MAX_PRICE = 0.90, 20000  # Fnac price_090_20000
DONE = ("OK", "WARNING", "ERROR", "FATAL")


def q(tag):
    return f"{{{NS}}}{tag}"


# --------------------------------------------------------------------------- API

class Fnac:
    def __init__(self, sandbox=False):
        names = ("FNAC_PARTNER_ID", "FNAC_SHOP_ID", "FNAC_KEY") + (() if sandbox else ("FNAC_API_URL",))
        missing = [k for k in names if not os.environ.get(k)]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.url = (SANDBOX_URL if sandbox else os.environ["FNAC_API_URL"]).rstrip("/")
        self.partner_id = os.environ["FNAC_PARTNER_ID"]
        self.shop_id = os.environ["FNAC_SHOP_ID"]
        self.key = os.environ["FNAC_KEY"]
        self.token, self.version = None, None

    def _post(self, service, root, retries=4):
        body = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")
        for attempt in range(retries):
            try:
                r = requests.post(f"{self.url}/{service}", data=body, timeout=120,
                                  headers={"Content-Type": "text/xml; charset=utf-8"})
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"{service}: cannot reach {self.url} ({e})")
                time.sleep(5 * 2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = 5 * 2 ** attempt
                print(f"   (Fnac returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                return ET.fromstring(r.content)
            except ET.ParseError:
                raise RuntimeError(f"{service}: HTTP {r.status_code}, not XML: {r.text[:300]}")
        raise RuntimeError(f"{service}: gave up after {retries} retries")

    @staticmethod
    def errors(el):
        return [f"{e.get('code') or ''} {(e.text or '').strip()}".strip() for e in el.findall(q("error"))]

    def auth(self):
        root = ET.Element(q("auth"))
        for tag, val in (("partner_id", self.partner_id), ("shop_id", self.shop_id), ("key", self.key)):
            ET.SubElement(root, q(tag)).text = val
        resp = self._post("auth", root)
        token = resp.findtext(q("token"))
        if resp.get("status") != "OK" or not token:
            sys.exit(f"Fnac login failed ({resp.get('status')}): {'; '.join(self.errors(resp)) or 'no token'}\n"
                     "Check FNAC_PARTNER_ID / FNAC_SHOP_ID / FNAC_KEY and FNAC_API_URL.")
        self.token, self.version = token, resp.findtext(q("version"))
        return resp

    def _request(self, tag, **attrs):
        if not self.token:
            self.auth()
        return ET.Element(q(tag), {"partner_id": self.partner_id, "shop_id": self.shop_id,
                                   "token": self.token, **{k: str(v) for k, v in attrs.items()}})

    def offers_update(self, offers_xml):
        root = self._request("offers_update")
        root.extend(offers_xml)
        resp = self._post("offers_update", root)
        batch_id = resp.findtext(q("batch_id"))
        if resp.get("status") not in ("OK", "RUNNING", "ACTIVE") or not batch_id:
            raise RuntimeError(f"offers_update refused ({resp.get('status')}): {'; '.join(self.errors(resp))}")
        return batch_id

    def batch_status(self, batch_id):
        root = self._request("batch_status")
        ET.SubElement(root, q("batch_id")).text = batch_id
        return self._post("batch_status", root)

    def offers(self):
        out, page = [], 1
        while True:
            root = self._request("offers_query", results_count=100)
            ET.SubElement(root, q("paging")).text = str(page)
            resp = self._post("offers_query", root)
            if resp.get("status") not in ("OK", "WARNING"):
                raise RuntimeError(f"offers_query failed ({resp.get('status')}): {'; '.join(self.errors(resp))}")
            for o in resp.findall(q("offer")):
                out.append({c.tag.split("}")[1]: (c.text or "").strip() for c in o})
            if page >= int(resp.findtext(q("total_paging")) or 1):
                return out
            page += 1


# ---------------------------------------------------------------------- helpers

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def to_ean(code):
    """Normalise an ISBN-10/13 or EAN to a 13-digit EAN, or return None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    if len(c) == 13 and c.isdigit():
        return c
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        core = "978" + c[:9]
        check = (10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10
        return core + str(check)
    return None


def normalize(raw):
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": raw.get("title") or "",
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "quantity": raw.get("quantity"),
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p, a):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    elif not MIN_PRICE <= round(float(p["price"]) * a.price_factor, 2) <= MAX_PRICE:
        probs.append(f"price outside Fnac's {MIN_PRICE}-{MAX_PRICE} range")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (needed to match the Fnac catalog)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in PRODUCT_STATES:
        probs.append(f"no Fnac state for condition '{p['condition']}' (see PRODUCT_STATES)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def offer_xml(p, a):
    o = ET.Element(q("offer"))
    ET.SubElement(o, q("product_reference"), {"type": "Ean"}).text = p["ean"]
    ET.SubElement(o, q("offer_reference"), {"type": "SellerSku"}).text = p["sku"]
    ET.SubElement(o, q("price")).text = f"{float(p['price']) * a.price_factor:.2f}"
    ET.SubElement(o, q("product_state")).text = str(PRODUCT_STATES[p["condition"]])
    ET.SubElement(o, q("quantity")).text = str(min(int(p["quantity"] or a.quantity), 9999))
    if a.comment:
        ET.SubElement(o, q("description")).text = a.comment[:255]
    ET.SubElement(o, q("internal_comment")).text = p["title"][:255]
    if a.time_to_ship:
        ET.SubElement(o, q("time_to_ship")).text = str(a.time_to_ship)
    return o


# --------------------------------------------------------------------- commands

def cmd_check(api, a):
    api.auth()
    print(f"Connected to Fnac Marketplace API {api.version or ''} at {api.url} (shop {api.shop_id})")


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

    todo = []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        probs = problems_of(p, a)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if p["sku"] in state and not a.resend:
            print(f"[{i}] already pushed: {p['title']}")
            continue
        todo.append(p)
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(todo)} offer(s) to push.")
    if not todo:
        return

    preview = ET.Element(q("offers_update"))
    preview.extend(offer_xml(p, a) for p in todo)
    XML_FILE.write_bytes(b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(preview, encoding="utf-8"))
    if a.dry_run:
        print(f"Dry run: offers written to {XML_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will send {len(todo)} LIVE offer(s) to Fnac. Type 'yes': ").strip().lower() != "yes":
            return

    ok = failed = 0
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start:start + BATCH_SIZE]
        batch_id = api.offers_update([offer_xml(p, a) for p in chunk])
        print(f"Sent batch {batch_id} ({len(chunk)} offers), waiting for Fnac to process it...")
        deadline = time.time() + a.wait
        while True:
            resp = api.batch_status(batch_id)
            status = resp.get("status")
            if status in DONE:
                break
            if time.time() > deadline:
                print(f"   still {status} after {a.wait}s - nothing recorded for this batch; run `push` again later.")
                break
            time.sleep(a.poll)
        if status not in DONE:
            continue
        results = {o.findtext(q("offer_seller_id")): o for o in resp.findall(q("offer"))}
        print(f"   batch {status}" + (f": {'; '.join(api.errors(resp))}" if api.errors(resp) else ""))
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for p in chunk:
            o = results.get(p["sku"])
            errs = api.errors(o) if o is not None else []
            st = o.get("status") if o is not None else status
            if st in ("OK", "WARNING") and status != "FATAL":
                state[p["sku"]] = {"ean": p["ean"], "batch_id": batch_id, "pushed_at": now,
                                   "offer_fnac_id": o.findtext(q("offer_fnac_id")) if o is not None else None}
                ok += 1
                if errs:
                    print(f"   ~ {p['sku']}: {'; '.join(errs)}")
            else:
                failed += 1
                print(f"   ! {p['sku']}: {'; '.join(errs) or st}")
        save_state(state)
    print(f"Done: {ok} offer(s) accepted, {failed} rejected. Run `status` to see them live.")


def cmd_status(api, a):
    state = load_state()
    offers = {o.get("offer_seller_id"): o for o in api.offers()}
    if not state and not offers:
        print("No offers yet.")
        return
    for sku in sorted(set(state) | set(offers)):
        o = offers.get(sku)
        if not o:
            print(f"{sku}: not found on Fnac (yet)")
            continue
        live = "LIVE" if int(o.get("quantity") or 0) > 0 else "out of stock"
        print(f"{sku}: {live}  price={o.get('price')}  qty={o.get('quantity')}  state={o.get('product_state')}  "
              f"{o.get('product_name', '')[:50]}" + ("" if sku in state else "  (not pushed by this script)"))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--sandbox", action="store_true", help="use Fnac's test platform (needs test credentials)")
    ap = argparse.ArgumentParser(description="Push offers to the Fnac marketplace (API v2.6)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", parents=[common], help="test the credentials")
    p = sub.add_parser("push", parents=[common], help="send offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'quantity' wins)")
    p.add_argument("--comment", default=DEFAULT_COMMENT, help="offer comment buyers see (max 255 chars)")
    p.add_argument("--time-to-ship", type=int, help="days until you ship")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--resend", action="store_true", help="also re-send already pushed offers (price/stock sync)")
    p.add_argument("--dry-run", action="store_true", help="write fnac_offers.xml, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--wait", type=int, default=900, help="max seconds to wait for each batch")
    p.add_argument("--poll", type=int, default=15, help="seconds between batch status checks")
    sub.add_parser("status", parents=[common], help="list your offers on Fnac")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run:  # no credentials needed to build the file
        api = None
        if all(os.environ.get(k) for k in ("FNAC_PARTNER_ID", "FNAC_SHOP_ID", "FNAC_KEY")):
            api = Fnac(a.sandbox)
        cmd_push(api, a)
        return
    api = Fnac(a.sandbox)
    {"check": cmd_check, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)


if __name__ == "__main__":
    main()
