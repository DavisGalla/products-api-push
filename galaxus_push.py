#!/usr/bin/env python3
"""
Push books to Galaxus (Digitec Galaxus marketplace, Switzerland / Germany)
the official way: Galaxus has no listing REST API. Partners deliver a data
feed file that Galaxus imports every 30 minutes (technical integration docs:
https://confdg.atlassian.net/wiki/spaces/PHCM, "Technische Integration").

This script implements stage 1 ("Etappe 1: Angebotszuweisung"): offers are
attached to products that already exist in the Galaxus catalog, matched by
GTIN (ISBN-13 for books). It writes one StockPricingData CSV file (UTF-8,
";" separated, strings in quotes) with ProviderKey, MainGtin,
ManufacturerKey, price excl. VAT, VAT rate and stock, and uploads it to the
Galaxus SFTP server (ftp.digitecgalaxus.ch:22). The file is always the full
assortment: books that are no longer in it go offline at Galaxus.

The upload is safe for Galaxus' importer: the file is written as
tmp_<name> first, its size is checked, then it is renamed to the configured
name (Galaxus may otherwise download a half-written, empty file).

ONE-TIME SETUP (see docs/galaxus_push.md)
  Galaxus Partner Portal -> marketplace onboarding. Galaxus sends the SFTP
  user name and password and configures the feed file name. Then
  (PowerShell), or as Bitwarden secrets:
      $env:GALAXUS_SFTP_USER = "..."
      $env:GALAXUS_SFTP_PASSWORD = "..."
      $env:GALAXUS_PROVIDER = "YourShopName"      # the [ProviderName] in the file name

USAGE
    pip install paramiko
    python galaxus_push.py check                  # SFTP login works? feed folder visible?
    python galaxus_push.py push --dry-run         # write the CSV locally, upload nothing
    python galaxus_push.py push                   # upload the feed (only if something changed)
    python galaxus_push.py push --drop-sold-out   # remove sold-out books from the assortment
    python galaxus_push.py status                 # what the last feed contained, file on the server

NOTE: Galaxus only reports import problems in the Partner Portal
(Katalog -> Feeds), not over SFTP. Check it after the first upload.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

# Which Galaxus shop: "CH" (galaxus.ch, prices in CHF only) or "DE" (galaxus.de, EUR).
MARKET = "CH"
EUR_TO_CHF = 0.94            # source prices are EUR; CH needs CHF (change, or pass --chf-rate)
VAT_RATE = {"CH": "2.6", "DE": "7"}   # reduced VAT rate for books, in percent
# Galaxus attaches offers to NEW products in its catalog. Used goods need a separate
# contract (annex "Gebrauchtwaren"), so only these source conditions are offered.
ALLOWED_CONDITIONS = {"NewCondition"}
DEFAULT_STOCK = 1            # stock when the product has no "stock"/"quantity" field
# Feed file name as configured by Galaxus during onboarding. {provider} = GALAXUS_PROVIDER.
FEED_FILE_NAME = "StockPricingData_{provider}.csv"
REUPLOAD_AFTER_HOURS = 20    # upload again even if unchanged (Galaxus wants >= 1 update a day)
# ======================================================================

SFTP_HOST = "ftp.digitecgalaxus.ch"
SFTP_PORT = 22
# Public host keys of ftp.digitecgalaxus.ch, from the Galaxus docs ("3 Datenübermittlung").
# The connection is refused if the server shows another key (override: GALAXUS_SFTP_HOSTKEY).
HOST_KEYS = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHgyJzvzzPJ1wJBuuUIYTCL+SJkViVFqw12JhV1fd/QO",
    "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBN+YVR7x8JFkUipf7sgh6iynBEqzIanc2iRAAMWefcOWKFiHW4P5pgjfAD/gb72po27L+OuqVs0UOI3PMztFObA=",
]
STATE_FILE = Path("galaxus_posted.json")      # one entry per offer in the last uploaded feed
UPLOAD_FILE = Path("galaxus_upload.json")     # hash and time of the last upload
PREVIEW_FILE = Path("galaxus_feed.csv")
CURRENCY = {"CH": "CHF", "DE": "EUR"}


class TransferError(Exception):
    pass


# ------------------------------------------------------------------- SFTP

class Sftp:
    """Galaxus SFTP server (password login, pinned host key, tmp_ upload + rename)."""

    def __init__(self):
        missing = [n for n in ("GALAXUS_SFTP_USER", "GALAXUS_SFTP_PASSWORD") if not os.environ.get(n)]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        try:
            import paramiko
        except ImportError:
            sys.exit("paramiko is needed for SFTP: pip install paramiko")
        self.paramiko = paramiko
        self.host = os.environ.get("GALAXUS_SFTP_HOST") or SFTP_HOST
        self.port = int(os.environ.get("GALAXUS_SFTP_PORT") or SFTP_PORT)
        self.folder = (os.environ.get("GALAXUS_SFTP_DIR") or ".").rstrip("/") or "/"
        self.client = self.sftp = None

    def host_keys(self):
        lines = [os.environ["GALAXUS_SFTP_HOSTKEY"]] if os.environ.get("GALAXUS_SFTP_HOSTKEY") else HOST_KEYS
        keys = []
        for line in lines:
            kind, b64 = line.split()[:2]
            keys.append(self.paramiko.PKey.from_type_string(kind, __import__("base64").b64decode(b64)))
        return keys

    def connect(self):
        if self.sftp:
            return self.sftp
        pk = self.paramiko
        try:
            transport = pk.Transport((self.host, self.port))
            transport.connect()                     # handshake only: check the host key before the password
            server_key = transport.get_remote_server_key()
            if not any(server_key.get_name() == k.get_name() and server_key.asbytes() == k.asbytes()
                       for k in self.host_keys()):
                transport.close()
                raise TransferError(f"{self.host} showed an unknown host key ({server_key.get_name()} "
                                    f"{server_key.get_base64()[:24]}...). Not sending the password. If Galaxus "
                                    f"renewed its key, check docs and set GALAXUS_SFTP_HOSTKEY.")
            transport.auth_password(os.environ["GALAXUS_SFTP_USER"], os.environ["GALAXUS_SFTP_PASSWORD"])
        except pk.AuthenticationException:
            raise TransferError("Galaxus SFTP rejected the user name/password. Careful: after several wrong "
                                "logins Galaxus blocks your IP for 3 hours.")
        except (OSError, pk.SSHException) as e:
            raise TransferError(f"cannot connect to {self.host}:{self.port}: {e}")
        self.client = transport
        self.sftp = pk.SFTPClient.from_transport(transport)
        return self.sftp

    def path(self, name):
        return name if self.folder == "." else f"{self.folder}/{name}"

    def listdir(self):
        return {a.filename: a for a in self.connect().listdir_attr(self.folder)}

    def upload(self, name, content, retries=3):
        """Write tmp_<name>, check its size, rename it over <name>, check again. Retries on errors."""
        s = self.connect()
        tmp, final = self.path("tmp_" + name), self.path(name)
        for attempt in range(1, retries + 1):
            try:
                with s.open(tmp, "wb") as f:
                    f.write(content)
                if s.stat(tmp).st_size != len(content):
                    raise TransferError("size check of the temporary file failed")
                try:
                    s.posix_rename(tmp, final)          # atomic replace (SFTPGo supports it)
                except IOError:
                    try:
                        s.remove(final)
                    except IOError:
                        pass
                    s.rename(tmp, final)
                if s.stat(final).st_size != len(content):
                    raise TransferError("size check of the uploaded file failed")
                return
            except (IOError, TransferError) as e:
                if attempt == retries:
                    raise TransferError(f"upload of {name} failed after {retries} tries: {e}")
                print(f"   (upload try {attempt} failed: {e}; retrying)")
                time.sleep(2 * attempt)

    def close(self):
        if self.client:
            self.client.close()


# ---------------------------------------------------------------- helpers

def load_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def to_gtin(code):
    """Normalise an ISBN-10/13 or EAN to a valid 13-digit GTIN, or return None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    digit = lambda core: str((10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10)
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        return "978" + c[:9] + digit("978" + c[:9])
    if len(c) == 13 and c.isdigit() and digit(c[:12]) == c[12]:
        return c
    return None


def price_excl_vat(price, a):
    """Source price (EUR, incl. VAT) -> Galaxus PriceExclVat in the market currency, half-up to cents."""
    v = Decimal(str(price)) * Decimal(str(a.price_factor))
    if MARKET == "CH":
        v *= Decimal(str(a.chf_rate))
    v /= 1 + Decimal(VAT_RATE[MARKET]) / 100
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def provider_name():
    name = os.environ.get("GALAXUS_PROVIDER") or ""
    return name if re.fullmatch(r"[A-Za-z0-9_-]{1,60}", name) else ""


def feed_name():
    return os.environ.get("GALAXUS_FEED_NAME") or FEED_FILE_NAME.format(provider=provider_name() or "YourShop")


def normalize(raw):
    stock = raw.get("stock", raw.get("quantity"))
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": (raw.get("title") or "").strip(),
        "price": raw.get("price_eur", raw.get("price")),
        "gtin": to_gtin(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "stock": int(stock) if stock not in (None, "") else DEFAULT_STOCK,
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    elif len(p["sku"]) > 50 or not all(32 <= ord(ch) <= 126 for ch in p["sku"]):
        probs.append("id/sku must be at most 50 plain ASCII characters (Galaxus ProviderKey)")
    if not p["price"]:
        probs.append("no price")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (Galaxus matches offers to its catalog by GTIN)")
    elif not p["gtin"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in ALLOWED_CONDITIONS:
        probs.append(f"condition '{p['condition']}' is not offered on Galaxus (new goods only, see ALLOWED_CONDITIONS)")
    return probs


def q(text):
    """CSV string field: in quotes, inner quotes doubled, no line breaks."""
    return '"' + " ".join(str(text).split()).replace('"', '""') + '"'


COLUMNS = ["ProviderKey", "MainGtin", "ManufacturerKey", "PriceExclVat_{cur}", "VatRatePercentage", "QuantityOnStock"]


def feed(rows):
    cur = CURRENCY[MARKET]
    lines = [";".join(c.format(cur=cur) for c in COLUMNS)]
    for r in rows:
        lines.append(";".join([q(r["sku"]), r["gtin"], q(r["gtin"]), r["price"], VAT_RATE[MARKET], str(r["stock"])]))
    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------- commands

def cmd_check(api, a):
    if not provider_name():
        print("! GALAXUS_PROVIDER is not set (or has spaces/special characters). It is part of the feed file name.")
    print(f"Market {MARKET} ({CURRENCY[MARKET]}), feed file {feed_name()}")
    files = api.listdir()
    print(f"SFTP login OK: {api.host}, folder '{api.folder}', {len(files)} file(s)")
    if feed_name() in files:
        f = files[feed_name()]
        print(f"   {feed_name()}: {f.st_size} bytes, last changed {time.strftime('%Y-%m-%d %H:%M', time.localtime(f.st_mtime))}")
    else:
        print(f"   {feed_name()} is not there yet (it will be created by the first push).")


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    offers, upload = load_json(STATE_FILE), load_json(UPLOAD_FILE)

    rows, seen_gtin, seen_sku, new = [], set(), set(), 0
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        probs = problems_of(p)
        if p["gtin"] in seen_gtin:
            probs.append(f"GTIN {p['gtin']} is already used by another book in this feed")
        if p["sku"] in seen_sku:
            probs.append(f"id/sku {p['sku']} appears twice")
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if p["sold_out"]:
            if a.drop_sold_out:
                print(f"[{i}] sold out, left out of the assortment: {p['title']}")
                continue
            p["stock"] = 0
        if p["sku"] not in offers:
            if a.limit and new >= a.limit:
                continue
            new += 1
        seen_gtin.add(p["gtin"])
        seen_sku.add(p["sku"])
        rows.append({"sku": p["sku"], "gtin": p["gtin"], "price": price_excl_vat(p["price"], a), "stock": p["stock"]})

    keys = {r["sku"] for r in rows}
    removed = sorted(k for k in offers if k not in keys)
    changed = sum(1 for r in rows if r["sku"] in offers and
                  (offers[r["sku"]]["price"], offers[r["sku"]]["stock"]) != (r["price"], r["stock"]))
    cur = CURRENCY[MARKET]
    print(f"Feed: {len(rows)} offer(s): {new} new, {changed} changed price/stock, "
          f"{len(removed)} removed from the assortment.")
    content = feed(rows)
    PREVIEW_FILE.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    age_h = (time.time() - upload.get("uploaded_ts", 0)) / 3600
    if a.dry_run:
        print(f"Dry run: feed written to {PREVIEW_FILE.resolve()} - nothing uploaded.")
        return
    if digest == upload.get("sha256") and age_h < REUPLOAD_AFTER_HOURS and not a.force:
        print(f"Feed unchanged since the last upload {age_h:.1f}h ago - nothing uploaded (--force to upload anyway).")
        return
    if not rows and offers and not a.force:
        sys.exit("The new feed is EMPTY: this would take every offer offline at Galaxus. "
                 "Not uploading (pass --force if you really want that).")
    if not a.yes:
        if input(f"This replaces the LIVE Galaxus feed {feed_name()} ({len(rows)} offer(s)). Type 'yes': ").strip().lower() != "yes":
            return
    name = feed_name()
    api.upload(name, content)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        if r["sku"] not in offers:
            print(f"   + {r['sku']}: {r['gtin']}  {r['price']} {cur} excl. VAT, stock {r['stock']}")
        old = offers.get(r["sku"], {})
        if (old.get("price"), old.get("stock")) != (r["price"], r["stock"]):
            offers[r["sku"]] = {"gtin": r["gtin"], "price": r["price"], "stock": r["stock"],
                                "since": old.get("since", now), "updated_at": now}
    for k in removed:
        print(f"   - {k}: no longer in the feed (goes offline at Galaxus)")
        del offers[k]
    save_json(STATE_FILE, offers)
    save_json(UPLOAD_FILE, {"file": name, "sha256": digest, "bytes": len(content), "offers": len(rows),
                            "uploaded_at": now, "uploaded_ts": time.time()})
    print(f"Uploaded {name} ({len(content)} bytes). Galaxus imports it within ~30 minutes; "
          f"check Partner Portal -> Katalog -> Feeds for rejected lines.")


def cmd_status(api, a):
    offers, upload = load_json(STATE_FILE), load_json(UPLOAD_FILE)
    if not offers:
        print("No feed uploaded yet.")
    else:
        sold = sum(1 for o in offers.values() if o["stock"] == 0)
        print(f"Last upload {upload.get('uploaded_at')}: {upload.get('file')} with {len(offers)} offer(s) "
              f"({sold} with stock 0).")
        for sku in sorted(offers):
            o = offers[sku]
            print(f"   {sku}: GTIN {o['gtin']}  {o['price']} {CURRENCY[MARKET]} excl. VAT  stock {o['stock']}")
    files = api.listdir()
    f = files.get(feed_name())
    if f:
        print(f"On the SFTP server: {feed_name()} {f.st_size} bytes, "
              f"changed {time.strftime('%Y-%m-%d %H:%M', time.localtime(f.st_mtime))}")
    else:
        print(f"On the SFTP server: {feed_name()} is missing!")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to Galaxus (StockPricingData feed over SFTP)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the SFTP login and show the feed file on the server")
    p = sub.add_parser("push", help="build the feed from a JSON file and upload it")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--chf-rate", type=float, default=EUR_TO_CHF, help=f"EUR -> CHF rate (default {EUR_TO_CHF})")
    p.add_argument("--drop-sold-out", action="store_true",
                   help="leave sold-out books out of the feed (default: keep them with stock 0)")
    p.add_argument("--dry-run", action="store_true", help="write galaxus_feed.csv, upload nothing")
    p.add_argument("--force", action="store_true", help="upload even if the feed did not change")
    p.add_argument("--limit", type=int, default=0, help="max new offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="what the last feed contained, and the file on the server")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run:
        return cmd_push(None, a)
    api = Sftp()
    try:
        {"check": cmd_check, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except TransferError as e:
        sys.exit(f"Galaxus SFTP error: {e}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
