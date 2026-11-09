#!/usr/bin/env python3
"""
Push books to Fruugo (global marketplace, ~40 countries) the official way:
Fruugo has no listing API. Retailers publish a full product feed (CSV or XML)
at a static http(s) URL, and Fruugo downloads it every time a new version is
published (at least daily). Fruugo's Order API only handles orders.
Spec: Fruugo Retailer Support wiki, https://fruugo.atlassian.net/wiki/spaces/RR
("Fruugo Feed", "Field Specification", "CSV", "Lead Time", "Country
Restrictions", "Media Product Attributes").

What this script does:
  1. Builds the Fruugo CSV feed from books.json (comma separated, UTF-8,
     header row with Fruugo's field names, text fields in double quotes).
     Books use the ISBN field (Fruugo: "mandatory for all books") instead of
     EAN, and the media attributes (Attribute2 language, Attribute3 publisher).
  2. Copies the images next to the feed: Fruugo allows at most 150 characters
     per image URL and needs at least 400x400 px. The scraped image URLs are
     longer, so each image is downloaded once, its size is checked, and it is
     published as img/<hash>.<ext> beside the feed.
  3. Publishes images first, then the feed, to the web location Fruugo reads:
     a local folder your web server serves (FRUUGO_FEED_DIR), or your own web
     host over SFTP (FRUUGO_SFTP_*; the host key must be pinned).
  4. Downloads the feed from the public URL and checks it is the new file.

The feed is always the full assortment. Books that disappear from the source
are kept for NOTAVAILABLE_KEEP_DAYS with StockStatus NOTAVAILABLE ("permanently
out of stock and needs to be removed"), then left out.

ONE-TIME SETUP (see docs/fruugo_push.md)
  Fruugo retailer account -> integration team. Tell them the feed URL.
      $env:FRUUGO_FEED_URL = "https://feeds.example.com/<random>/fruugo.csv"
      $env:FRUUGO_FEED_DIR = "D:\\www\\feeds\\<random>"        # the folder served at that URL
  or, to upload to a web host:
      $env:FRUUGO_SFTP_HOST / FRUUGO_SFTP_USER / FRUUGO_SFTP_PASSWORD (or FRUUGO_SFTP_KEY_FILE)
      $env:FRUUGO_SFTP_DIR      # folder on the host that is served at the feed URL
      $env:FRUUGO_SFTP_HOSTKEY  # "ssh-ed25519 AAAA..." (ssh-keyscan, verified with your host)

USAGE
    python fruugo_push.py check                   # settings, target writable, feed URL reachable
    python fruugo_push.py push --dry-run          # write fruugo_feed.csv locally, publish nothing
    python fruugo_push.py push                    # publish images + feed (only if something changed)
    python fruugo_push.py push --drop-sold-out    # sold-out books -> NOTAVAILABLE instead of OUTOFSTOCK
    python fruugo_push.py status                  # what the last feed contained; is it online?

NOTE: Fruugo reports feed and product errors in its Retailer Portal (and by
e-mail from the integration team), not through an API. Check them after the
first publish.
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

# Full Fruugo category path, in English, exactly as in Fruugo's category tree
# (Fruugo: products without an accurate category path don't go live).
CATEGORY = ""
CATEGORY_MAP = {}             # optional: source "category" -> Fruugo path, e.g. {"Libros infantiles": "..."}
CURRENCY = "EUR"              # prices in books.json are EUR
VAT_RATE = "4"                # VAT rate in % of your registered country (Spain: 4 % for books). Verify!
LANGUAGE = "es"               # language of titles/descriptions (ISO 639-1, lower case)
# Media attributes for books (Fruugo "Media Product Attributes"): Attribute1 Format,
# Attribute2 Language, Attribute3 Publisher, Attribute4 Length, Attribute5 Author,
# Attribute6 Release Date. Tell Fruugo's integration team which ones you use.
BOOK_ATTRIBUTES = {"Attribute2": "Spanish"}
PUBLISHER_ATTRIBUTE = "Attribute3"   # filled with the book's brand (publisher); "" = don't send
LEAD_TIME_DAYS = None         # days from order to dispatch; None = Fruugo's 24 h default. Minimum 2.
PACKAGE_WEIGHT_G = None       # grams, whole number; mandatory if your shipping is priced by weight
COUNTRIES = ""                # "" = your account's countries; or an include list like "ES PT FR"
ALLOWED_CONDITIONS = {"NewCondition"}   # the feed has no condition field: new books only
DEFAULT_STOCK = 1             # stock when the product has no "stock"/"quantity" field
MIRROR_IMAGES = "auto"        # "auto": copy images whose URL is too long; "always"; "never"
IMAGE_FORMATS = {"jpg", "png", "gif"}
MIN_IMAGE_PX = 400            # Fruugo: at least 400 x 400 px
REPUBLISH_AFTER_HOURS = 20    # publish again even if unchanged (Fruugo wants a fresh feed daily)
NOTAVAILABLE_KEEP_DAYS = 30   # keep removed books as NOTAVAILABLE this long, then drop them
MAX_REMOVED_SHARE = 0.5       # refuse to retire more than this share of published books at once
# ======================================================================

STATE_FILE = Path("fruugo_posted.json")     # one entry per SKU in the last published feed
PUBLISH_FILE = Path("fruugo_publish.json")  # hash/time of the last publish, published images
PREVIEW_FILE = Path("fruugo_feed.csv")
IMAGE_CACHE = Path("fruugo_images")
IMAGE_DIR = "img"
USER_AGENT = "product-pushers-fruugo/1.0"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
PLACEHOLDER_URL = "https://feeds.example.com/fruugo/fruugo.csv"

COLUMNS = (["ProductId", "SkuId", "ISBN", "EAN", "Brand", "Manufacturer", "Category"]
           + [f"Imageurl{i}" for i in range(1, 6)]
           + ["StockStatus", "StockQuantity", "LeadTime", "PackageWeight", "Title", "Description",
              "Language", "Currency", "NormalPriceWithVAT", "VATRate", "Country"]
           + [f"Attribute{i}" for i in range(1, 11)])
NUMERIC = {"StockQuantity", "LeadTime", "PackageWeight", "NormalPriceWithVAT", "VATRate"}


class PublishError(Exception):
    pass


# ----------------------------------------------------------------- helpers

def load_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def to_gtin(code):
    """Normalise an ISBN-10/13 or EAN to a valid 13-digit GTIN, or return None."""
    c = str(code or "").replace("-", "").replace(" ", "").upper()
    digit = lambda core: str((10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10)
    if len(c) == 10 and c[:9].isdigit() and (c[9].isdigit() or c[9] == "X"):
        return "978" + c[:9] + digit("978" + c[:9])
    if len(c) == 13 and c.isdigit() and digit(c[:12]) == c[12]:
        return c
    return None


def feed_url():
    return os.environ.get("FRUUGO_FEED_URL") or ""


def feed_name(url=None):
    name = posixpath.basename(urllib.parse.urlparse(url or feed_url() or PLACEHOLDER_URL).path)
    return name or "fruugo.csv"


def image_base(url=None):
    u = urllib.parse.urlparse(url or feed_url() or PLACEHOLDER_URL)
    return urllib.parse.urlunparse((u.scheme, u.netloc, posixpath.dirname(u.path).rstrip("/") + f"/{IMAGE_DIR}/", "", "", ""))


def http_get(url, limit=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        data = r.read(limit + 1 if limit else -1)
        if limit and len(data) > limit:
            raise PublishError(f"{url}: larger than {limit} bytes")
        return r.status, dict(r.headers), data


def image_info(b):
    """(extension, width, height) for JPEG/PNG/GIF/WebP bytes, or None."""
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        w, h = struct.unpack(">II", b[16:24])
        return "png", w, h
    if b[:6] in (b"GIF87a", b"GIF89a") and len(b) >= 10:
        w, h = struct.unpack("<HH", b[6:10])
        return "gif", w, h
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP" and len(b) >= 30:
        kind = b[12:16]
        if kind == b"VP8X":
            return "webp", 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", b[26:30])
            return "webp", w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            bits = int.from_bytes(b[21:25], "little")
            return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return None
    if b[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                i += 1
                continue
            marker = b[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return "jpg", w, h
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return None


def clean_text(text, limit):
    """Plain text for Fruugo: no URLs or e-mail addresses, no control characters, cut at a word."""
    t = str(text or "")
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "", t)
    t = "".join(ch for ch in t if ch in "\n" or ord(ch) >= 32)
    lines = [" ".join(line.split()) for line in t.replace("\r", "\n").split("\n")]
    t = "\n".join(line for line in lines if line).strip()
    if len(t) > limit:
        t = t[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:\n") + "…"
    return t


def price_with_vat(price, factor):
    try:
        v = Decimal(str(price)) * Decimal(str(factor))
    except InvalidOperation:
        return None
    v = v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(v) if Decimal("0") < v < Decimal("1000000") else None


def q(text):
    """CSV text field (Fruugo CSV page): always in double quotes, inner quotes doubled."""
    return '"' + str(text).replace('"', '""') + '"'


def feed_csv(rows):
    lines = [",".join(COLUMNS)]
    for r in rows:
        cells = []
        for c in COLUMNS:
            v = r.get(c, "")
            cells.append(str(v) if (c in NUMERIC or v == "") else q(v))
        lines.append(",".join(cells))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# ---------------------------------------------------------------- targets

class LocalTarget:
    """A folder your web server serves at the feed URL's directory."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.desc = f"local folder {self.folder}"

    def check(self):
        if not self.folder.is_dir():
            raise PublishError(f"FRUUGO_FEED_DIR {self.folder} does not exist")
        probe = self.folder / ".fruugo_write_test"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as e:
            raise PublishError(f"cannot write to {self.folder}: {e}")
        return sorted(p.name for p in self.folder.iterdir())

    def write(self, relpath, content):
        final = self.folder / relpath
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name("tmp_" + final.name)
        tmp.write_bytes(content)
        if tmp.stat().st_size != len(content):
            raise PublishError(f"size check of {tmp} failed")
        os.replace(tmp, final)              # atomic: Fruugo never sees a half-written file

    def close(self):
        pass


class SftpTarget:
    """Your own web host over SFTP. The host key must be pinned (FRUUGO_SFTP_HOSTKEY)."""

    def __init__(self):
        need = ["FRUUGO_SFTP_HOST", "FRUUGO_SFTP_USER", "FRUUGO_SFTP_HOSTKEY"]
        missing = [n for n in need if not os.environ.get(n)]
        if not (os.environ.get("FRUUGO_SFTP_PASSWORD") or os.environ.get("FRUUGO_SFTP_KEY_FILE")):
            missing.append("FRUUGO_SFTP_PASSWORD or FRUUGO_SFTP_KEY_FILE")
        if missing:
            raise PublishError("missing " + ", ".join(missing))
        try:
            import paramiko
        except ImportError:
            raise PublishError("paramiko is needed for SFTP: pip install paramiko")
        self.pk = paramiko
        self.host = os.environ["FRUUGO_SFTP_HOST"]
        self.port = int(os.environ.get("FRUUGO_SFTP_PORT") or 22)
        self.folder = (os.environ.get("FRUUGO_SFTP_DIR") or ".").rstrip("/") or "/"
        self.desc = f"SFTP {self.host}:{self.port} folder '{self.folder}'"
        self.transport = self.sftp = None

    def connect(self):
        if self.sftp:
            return self.sftp
        pk = self.pk
        import base64
        pinned = []
        for line in os.environ["FRUUGO_SFTP_HOSTKEY"].split(";"):
            parts = line.split()
            if len(parts) >= 2:
                pinned.append(pk.PKey.from_type_string(parts[0], base64.b64decode(parts[1])))
        if not pinned:
            raise PublishError("FRUUGO_SFTP_HOSTKEY must look like 'ssh-ed25519 AAAA...'")
        try:
            t = pk.Transport((self.host, self.port))
            t.connect()                          # handshake only: check the host key before logging in
            key = t.get_remote_server_key()
            if not any(key.get_name() == k.get_name() and key.asbytes() == k.asbytes() for k in pinned):
                t.close()
                raise PublishError(f"{self.host} showed an unknown host key ({key.get_name()} "
                                   f"{key.get_base64()[:24]}...). Not logging in.")
            user = os.environ["FRUUGO_SFTP_USER"]
            if os.environ.get("FRUUGO_SFTP_KEY_FILE"):
                t.auth_publickey(user, pk.PKey.from_path(os.environ["FRUUGO_SFTP_KEY_FILE"]))
            else:
                t.auth_password(user, os.environ["FRUUGO_SFTP_PASSWORD"])
        except pk.AuthenticationException:
            raise PublishError(f"{self.host} rejected the SFTP login")
        except (OSError, pk.SSHException) as e:
            raise PublishError(f"cannot connect to {self.host}:{self.port}: {e}")
        self.transport = t
        self.sftp = pk.SFTPClient.from_transport(t)
        return self.sftp

    def path(self, rel):
        return rel if self.folder == "." else f"{self.folder}/{rel}"

    def check(self):
        return sorted(self.connect().listdir(self.folder))

    def write(self, relpath, content, retries=3):
        s = self.connect()
        final = self.path(relpath)
        d, name = posixpath.split(final)
        if d:
            try:
                s.stat(d)
            except IOError:
                s.mkdir(d)
        tmp = posixpath.join(d, "tmp_" + name) if d else "tmp_" + name
        for attempt in range(1, retries + 1):
            try:
                with s.open(tmp, "wb") as f:
                    f.write(content)
                if s.stat(tmp).st_size != len(content):
                    raise PublishError("size check of the temporary file failed")
                try:
                    s.posix_rename(tmp, final)
                except IOError:
                    try:
                        s.remove(final)
                    except IOError:
                        pass
                    s.rename(tmp, final)
                if s.stat(final).st_size != len(content):
                    raise PublishError("size check of the uploaded file failed")
                return
            except (IOError, PublishError) as e:
                if attempt == retries:
                    raise PublishError(f"upload of {relpath} failed after {retries} tries: {e}")
                print(f"   (upload try {attempt} of {relpath} failed: {e}; retrying)")
                time.sleep(2 * attempt)

    def close(self):
        if self.transport:
            self.transport.close()


def make_target():
    if os.environ.get("FRUUGO_FEED_DIR"):
        return LocalTarget(os.environ["FRUUGO_FEED_DIR"])
    if os.environ.get("FRUUGO_SFTP_HOST"):
        return SftpTarget()
    raise PublishError("set FRUUGO_FEED_DIR (local web folder) or FRUUGO_SFTP_HOST (+ user, key, host key)")


# ------------------------------------------------------------------ images

def mirror_name(src):
    """Short, stable file name for a source image. The extension is fixed after download."""
    ext = posixpath.splitext(urllib.parse.urlparse(src).path)[1].lower().lstrip(".")
    ext = {"jpeg": "jpg"}.get(ext, ext)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:20], ext if ext in IMAGE_FORMATS else "jpg"


def cached_image(stem):
    for ext in IMAGE_FORMATS:
        p = IMAGE_CACHE / f"{stem}.{ext}"
        if p.exists():
            return p
    return None


def resolve_images(urls, base, download):
    """Fruugo image URLs for one book: [(url, cache file or None)], and notes about dropped images."""
    out, notes = [], []
    for src in urls:
        if len(out) == 5:
            break
        if MIRROR_IMAGES == "never" or (MIRROR_IMAGES == "auto" and len(src) <= 150):
            if len(src) > 150:
                notes.append(f"image URL longer than 150 characters: {src[:60]}...")
            else:
                out.append((src, None))
            continue
        stem, ext = mirror_name(src)
        cached = cached_image(stem)
        rejected = IMAGE_CACHE / f"{stem}.rejected"
        if not cached and rejected.exists():          # checked before: don't download it again
            notes.append(rejected.read_text(encoding="utf-8"))
            continue
        if not cached and download:
            try:
                _, _, data = http_get(src, limit=MAX_IMAGE_BYTES)
            except (urllib.error.URLError, OSError, PublishError) as e:
                notes.append(f"image download failed ({e}): {src[:60]}...")
                continue
            info = image_info(data)
            kind, w, h = info or ("?", 0, 0)
            reason = ("not a JPEG/PNG/GIF/WebP image" if not info
                      else f"image format {kind} not in IMAGE_FORMATS" if kind not in IMAGE_FORMATS
                      else f"image is {w}x{h} px, Fruugo needs at least {MIN_IMAGE_PX}x{MIN_IMAGE_PX}"
                      if w < MIN_IMAGE_PX or h < MIN_IMAGE_PX else "")
            IMAGE_CACHE.mkdir(exist_ok=True)
            if reason:
                reason += f": {src[:60]}..."
                rejected.write_text(reason, encoding="utf-8")
                notes.append(reason)
                continue
            cached = IMAGE_CACHE / f"{stem}.{kind}"
            cached.write_bytes(data)
        name = cached.name if cached else f"{stem}.{ext}"
        url = base + name
        if len(url) > 150:
            notes.append(f"mirrored image URL is {len(url)} characters (max 150): use a shorter FRUUGO_FEED_URL")
            continue
        out.append((url, cached))
    return out, notes


# ----------------------------------------------------------------- products

def normalize(raw):
    stock = raw.get("stock", raw.get("quantity"))
    images = raw.get("images") or ([raw["image"]] if raw.get("image") else [])
    return {
        "sku": str(raw.get("sku") or raw.get("id") or ""),
        "title": raw.get("title") or "",
        "description": raw.get("description") or "",
        "brand": (raw.get("brand") or raw.get("publisher") or "").strip(),
        "category": raw.get("category") or "",
        "price": raw.get("price_eur", raw.get("price")),
        "gtin": to_gtin(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "stock": stock,
        "sold_out": bool(raw.get("sold_out", False)),
        "images": [u for u in images if isinstance(u, str) and u.startswith(("http://", "https://"))],
    }


def build_row(p, a, base, download):
    """Fruugo CSV row for a product, or (None, problems)."""
    probs = []
    sku = p["sku"]
    if not sku:
        probs.append("no id/sku")
    elif len(sku) > 50 or not re.fullmatch(r"[\x21-\x7e]+", sku):
        probs.append("id/sku must be at most 50 printable ASCII characters without spaces")
    if not p["raw_code"]:
        probs.append("no ISBN (Fruugo: mandatory for books)")
    elif not p["gtin"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if not p["brand"]:
        probs.append("no brand/publisher (Fruugo Brand is mandatory)")
    title = clean_text(p["title"], 150).replace("\n", " ")
    if not title:
        probs.append("no title")
    desc = clean_text(p["description"], 5000) or title
    price = price_with_vat(p["price"], a.price_factor)
    if price is None:
        probs.append(f"no valid price ({p['price']!r})")
    if p["condition"] not in ALLOWED_CONDITIONS:
        probs.append(f"condition '{p['condition']}' not offered (Fruugo feed has no condition field)")
    category = CATEGORY_MAP.get(p["category"]) or CATEGORY or ("<CATEGORY not set>" if a.dry_run else "")
    if not category:
        probs.append("no Fruugo category (set CATEGORY or CATEGORY_MAP)")
    try:
        stock = int(p["stock"]) if p["stock"] not in (None, "") else a.quantity
    except (TypeError, ValueError):
        stock = -1
    if not 0 <= stock <= 99999:
        probs.append(f"stock must be a whole number 0..99999 ({p['stock']!r})")
    images, notes = resolve_images(p["images"], base, download) if not probs else ([], [])
    if not probs and not images:
        probs.append("no usable image (" + ("; ".join(notes) or "none in the source") + ")")
    if probs:
        return None, probs, []
    isbn = p["gtin"] if p["gtin"].startswith(("978", "979")) else ""
    row = {
        "ProductId": sku, "SkuId": sku, "ISBN": isbn, "EAN": "" if isbn else p["gtin"],
        "Brand": p["brand"][:50], "Manufacturer": p["brand"][:300], "Category": category,
        "StockStatus": "INSTOCK" if stock > 0 else "OUTOFSTOCK", "StockQuantity": stock,
        "LeadTime": LEAD_TIME_DAYS if (LEAD_TIME_DAYS and stock > 0) else "",
        "PackageWeight": PACKAGE_WEIGHT_G or "",
        "Title": title, "Description": desc, "Language": LANGUAGE, "Currency": CURRENCY,
        "NormalPriceWithVAT": price, "VATRate": VAT_RATE, "Country": COUNTRIES,
    }
    for i, (url, _) in enumerate(images, 1):
        row[f"Imageurl{i}"] = url
    for k, v in BOOK_ATTRIBUTES.items():
        row[k] = str(v)[:2000]
    if PUBLISHER_ATTRIBUTE:
        row[PUBLISHER_ATTRIBUTE] = p["brand"][:2000]
    return row, notes, [c for _, c in images if c]


def retire(row):
    """Same product, StockStatus NOTAVAILABLE (Fruugo: permanently out of stock, to be removed)."""
    r = dict(row)
    r.update(StockStatus="NOTAVAILABLE", StockQuantity=0, LeadTime="")
    return r


# --------------------------------------------------------------- commands

def settings_problems():
    probs = []
    if not CATEGORY and not CATEGORY_MAP:
        probs.append("CATEGORY is not set (full Fruugo category path in English, from Fruugo's category tree)")
    if LEAD_TIME_DAYS is not None and not (isinstance(LEAD_TIME_DAYS, int) and 2 <= LEAD_TIME_DAYS <= 99):
        probs.append("LEAD_TIME_DAYS must be None or a whole number 2..99 (Fruugo: minimum 2)")
    if PACKAGE_WEIGHT_G is not None and not (isinstance(PACKAGE_WEIGHT_G, int) and 0 < PACKAGE_WEIGHT_G <= 99999):
        probs.append("PACKAGE_WEIGHT_G must be None or whole grams 1..99999")
    if COUNTRIES and not re.fullmatch(r"[A-Z]{2}( [A-Z]{2})*", COUNTRIES):
        probs.append("COUNTRIES must be upper-case ISO codes separated by spaces, e.g. 'ES PT FR'")
    if not re.fullmatch(r"[a-z]{2}", LANGUAGE):
        probs.append("LANGUAGE must be a two-letter lower-case code, e.g. 'es'")
    if not re.fullmatch(r"[A-Z]{3}", CURRENCY):
        probs.append("CURRENCY must be a three-letter upper-case code, e.g. 'EUR'")
    bad = [k for k in list(BOOK_ATTRIBUTES) + ([PUBLISHER_ATTRIBUTE] if PUBLISHER_ATTRIBUTE else [])
           if not re.fullmatch(r"Attribute([1-9]|10)", k)]
    if bad:
        probs.append("attribute names must be Attribute1..Attribute10: " + ", ".join(bad))
    url = feed_url()
    if url:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc or not feed_name(url).lower().endswith(".csv"):
            probs.append("FRUUGO_FEED_URL must be an http(s) URL ending in a .csv file name")
        elif len(image_base(url)) + 25 > 150:
            probs.append(f"FRUUGO_FEED_URL is too long: image URLs next to it would exceed 150 characters")
    return probs


def fetch_feed(url):
    """(status, sha256, rows, headers) of the feed at the public URL."""
    status, headers, data = http_get(url)
    rows = max(0, data.count(b"\n") - 1)
    return status, hashlib.sha256(data).hexdigest(), rows, headers


def cmd_check(a):
    url = feed_url()
    print(f"Feed URL: {url or '(FRUUGO_FEED_URL not set)'}")
    print(f"Images:   {image_base() if url else '(follows the feed URL)'}<hash>.<ext>  (mirroring: {MIRROR_IMAGES})")
    print(f"Category {CATEGORY or '(not set)'}; {CURRENCY}, VAT {VAT_RATE} %, language {LANGUAGE}, "
          f"lead time {LEAD_TIME_DAYS or 'Fruugo default (24 h)'}, countries {COUNTRIES or 'account default'}")
    ok = True
    for p in settings_problems():
        print("! " + p)
        ok = False
    try:
        target = make_target()
        files = target.check()
        print(f"Target OK: {target.desc}, {len(files)} file(s)" + (f", {feed_name()} present" if feed_name() in files else ""))
        target.close()
    except PublishError as e:
        print(f"! Target: {e}")
        ok = False
    if url:
        try:
            status, digest, rows, headers = fetch_feed(url)
            pub = load_json(PUBLISH_FILE)
            same = " (matches the last publish)" if digest == pub.get("sha256") else ""
            print(f"Feed URL answers {status}: {rows} product row(s), last modified "
                  f"{headers.get('Last-Modified', '?')}{same}")
        except (urllib.error.HTTPError) as e:
            print(f"   Feed URL answers {e.code} (normal before the first push)")
        except (urllib.error.URLError, OSError) as e:
            print(f"! Feed URL not reachable: {e}")
            ok = False
    else:
        ok = False
    print("Ready to push." if ok else "Fix the points marked '!' before a real push.")


def cmd_push(a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    probs = settings_problems()
    if not a.dry_run:
        if not feed_url():
            probs.append("FRUUGO_FEED_URL is not set")
        if probs:
            sys.exit("Settings problems:\n  - " + "\n  - ".join(probs))
    else:
        for p in probs + ([] if feed_url() else [f"FRUUGO_FEED_URL not set; using {PLACEHOLDER_URL} for image URLs"]):
            print(f"(dry run: {p}; a real push needs it)")
    print(f"Reading {path.resolve()}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    posted, pub = load_json(STATE_FILE), load_json(PUBLISH_FILE)
    base = image_base()
    now, now_ts = now_iso(), time.time()

    rows, entries, images, seen_sku, seen_code, new = [], {}, {}, set(), set(), 0
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sku"] in seen_sku:
            print(f"[{i}] SKIP '{p['title']}': id/sku {p['sku']} appears twice")
            continue
        if p["gtin"] and p["gtin"] in seen_code:
            print(f"[{i}] SKIP '{p['title']}': ISBN {p['gtin']} is already used by another book")
            continue
        known = p["sku"] in posted
        if not known and p["sold_out"]:
            continue                                   # never list a book that is already sold out
        if not known and a.limit and new >= a.limit:
            continue
        row, notes, files = build_row(p, a, base, download=not a.dry_run)
        if row is None:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(notes))
            continue
        for n in notes:
            print(f"[{i}] note '{p['title']}': {n}")
        if p["sold_out"]:
            row = retire(row) if a.drop_sold_out else dict(row, StockStatus="OUTOFSTOCK", StockQuantity=0, LeadTime="")
        seen_sku.add(p["sku"])
        seen_code.add(p["gtin"])
        new += not known
        rows.append(row)
        for f in files:
            images[f.name] = f
        old = posted.get(p["sku"], {})
        entries[p["sku"]] = {"isbn": row["ISBN"] or row["EAN"], "status": row["StockStatus"],
                             "stock": row["StockQuantity"], "price": row["NormalPriceWithVAT"],
                             "images": [row[f"Imageurl{n}"] for n in range(1, 6) if row.get(f"Imageurl{n}")],
                             "row": row, "first_published": old.get("first_published", now), "updated_at": now}

    gone, dropped = [], []
    for sku, old in posted.items():
        if sku in entries:
            continue
        since = old.get("gone_since") or now
        if now_ts - time.mktime(time.strptime(since, "%Y-%m-%dT%H:%M:%S")) > NOTAVAILABLE_KEEP_DAYS * 86400:
            dropped.append(sku)
            continue
        row = retire(old["row"])
        rows.append(row)
        gone.append(sku)
        entries[sku] = dict(old, status="NOTAVAILABLE", stock=0, row=row, gone_since=since,
                            updated_at=old.get("updated_at") if old.get("gone_since") else now)

    active = sum(1 for e in entries.values() if e["status"] != "NOTAVAILABLE")
    newly_gone = [s for s in gone if not posted[s].get("gone_since")]
    changed = sum(1 for s, e in entries.items() if s in posted and e["row"] != posted[s].get("row"))
    print(f"Feed: {len(rows)} product(s): {new} new, {changed} changed, {len(newly_gone)} newly NOTAVAILABLE, "
          f"{len(gone) - len(newly_gone)} still NOTAVAILABLE, {len(dropped)} dropped after "
          f"{NOTAVAILABLE_KEEP_DAYS} days.")
    for s in newly_gone:
        print(f"   - {s}: no longer in the source -> NOTAVAILABLE")
    if not active:
        sys.exit("Refusing to publish a feed without any available product (check the JSON file).")
    published_active = sum(1 for e in posted.values() if e.get("status") != "NOTAVAILABLE")
    if (published_active and len(newly_gone) > MAX_REMOVED_SHARE * published_active
            and not a.allow_mass_removal):
        sys.exit(f"Refusing: {len(newly_gone)} of {published_active} published books would become NOTAVAILABLE. "
                 f"If that is right, run again with --allow-mass-removal.")

    content = feed_csv(rows)
    PREVIEW_FILE.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    if a.dry_run:
        print(f"Dry run: wrote {PREVIEW_FILE} ({len(content)} bytes, {len(rows)} rows). Nothing published.")
        return
    age_h = (now_ts - pub.get("published_ts", 0)) / 3600
    if digest == pub.get("sha256") and age_h < REPUBLISH_AFTER_HOURS and not a.force:
        print(f"Feed unchanged since {pub.get('published_at')}; not publishing (use --force).")
        return
    if not a.yes and input(f"Publish {feed_name()} with {len(rows)} product(s)? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return

    try:
        target = make_target()
    except PublishError as e:
        sys.exit(f"Fruugo publish error: {e}")
    sent = set() if a.force else set(pub.get("images") or [])
    if pub.get("target") != target.desc:
        sent = set()
    try:
        todo = [n for n in sorted(images) if n not in sent]
        for n in todo:
            target.write(f"{IMAGE_DIR}/{n}", images[n].read_bytes())
            sent.add(n)
        if todo:
            print(f"Published {len(todo)} image(s) to {target.desc}/{IMAGE_DIR}")
        target.write(feed_name(), content)
    except PublishError as e:
        save_json(PUBLISH_FILE, dict(pub, images=sorted(sent), target=target.desc))
        sys.exit(f"Fruugo publish error: {e}")
    finally:
        target.close()

    for s in dropped:
        print(f"   - {s}: dropped from the feed ({NOTAVAILABLE_KEEP_DAYS} days NOTAVAILABLE)")
    save_json(STATE_FILE, entries)
    save_json(PUBLISH_FILE, {"file": feed_name(), "url": feed_url(), "target": target.desc, "sha256": digest,
                             "bytes": len(content), "products": len(rows), "available": active,
                             "images": sorted(sent), "published_at": now, "published_ts": now_ts})
    print(f"Published {feed_name()} ({len(content)} bytes) to {target.desc}.")
    if a.no_verify:
        return
    try:
        status, got, _, _ = fetch_feed(feed_url())
        if got == digest:
            print(f"Verified: {feed_url()} serves the new feed. Fruugo picks it up on its next download.")
        else:
            print(f"! {feed_url()} answered {status} but with a different file (cache? wrong folder?). "
                  f"Check that FRUUGO_FEED_DIR / FRUUGO_SFTP_DIR is the folder served at that URL.")
    except (urllib.error.URLError, OSError) as e:
        print(f"! Could not download {feed_url()} to verify it: {e}")


def cmd_status(a):
    posted, pub = load_json(STATE_FILE), load_json(PUBLISH_FILE)
    if not posted:
        print("No feed published yet.")
        return
    by = {}
    for e in posted.values():
        by[e["status"]] = by.get(e["status"], 0) + 1
    print(f"Last publish {pub.get('published_at')}: {pub.get('file')} with {len(posted)} product(s) "
          f"(" + ", ".join(f"{n} {s}" for s, n in sorted(by.items())) + ")")
    for sku in sorted(posted):
        e = posted[sku]
        extra = f"  (since {e['gone_since']})" if e.get("gone_since") else ""
        print(f"   {sku}: ISBN {e['isbn']}  {e['price']} {CURRENCY}  stock {e['stock']}  {e['status']}{extra}")
    url = pub.get("url") or feed_url()
    if not url:
        return
    try:
        status, digest, rows, headers = fetch_feed(url)
        state = "the last published feed" if digest == pub.get("sha256") else "a DIFFERENT file than the last publish"
        print(f"Online: {url} answers {status} with {state} ({rows} rows, Last-Modified "
              f"{headers.get('Last-Modified', '?')}).")
    except urllib.error.HTTPError as e:
        print(f"! Online: {url} answers {e.code}. Fruugo cannot download the feed!")
    except (urllib.error.URLError, OSError) as e:
        print(f"! Online: {url} not reachable: {e}")
    print("Product errors and live status: Fruugo Retailer Portal.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push books to Fruugo (CSV product feed at a static URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="settings, publish target and feed URL")
    p = sub.add_parser("push", help="build the feed from a JSON file and publish it")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--quantity", type=int, default=DEFAULT_STOCK, help="stock when the book has none")
    p.add_argument("--drop-sold-out", action="store_true",
                   help="sold-out books become NOTAVAILABLE (default: OUTOFSTOCK, stock 0)")
    p.add_argument("--dry-run", action="store_true", help="write fruugo_feed.csv only, publish nothing")
    p.add_argument("--force", action="store_true", help="publish (and re-upload images) even if unchanged")
    p.add_argument("--limit", type=int, default=0, help="max new books this run (0 = all)")
    p.add_argument("--allow-mass-removal", action="store_true",
                   help=f"allow retiring more than {int(MAX_REMOVED_SHARE * 100)} %% of published books")
    p.add_argument("--no-verify", action="store_true", help="don't download the feed URL after publishing")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="what the last feed contained, and whether the URL serves it")
    a = ap.parse_args()
    {"check": cmd_check, "push": cmd_push, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
