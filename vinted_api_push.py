#!/usr/bin/env python3
"""
Push products to Vinted through the official Vinted Pro Integrations API
(https://pro-docs.svc.vinted.com). No browser needed - just your API token.

Token: copy it from your Vinted Pro integrations settings. It looks like
"ACCESS_KEY,SIGNING_KEY". Provide it via env var or --token:
    PowerShell:  $env:VINTED_PRO_TOKEN = "access,signing"

Usage:
    # 1) Look up the ids you need (catalog, package size, condition options)
    python vinted_api_push.py ontologies                 # prints catalog tree + ids
    python vinted_api_push.py ontologies --find libros   # filter catalogs by name

    # 2) Validate (no side effects), then push
    python vinted_api_push.py push products.json --catalog-id 2320 --validate-only
    python vinted_api_push.py push products.json --catalog-id 2320
    python vinted_api_push.py push products.json --catalog-id 2320 --draft --limit 5

    # 3) Check processing status of what you pushed (creation is asynchronous)
    python vinted_api_push.py status

Add --sandbox to any command to use the dev-mode sandbox (needs a sandbox token).
Photos must be PUBLIC URLs (image_urls / images in your JSON); local files
cannot be sent through the API.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ============================== SETTINGS ==============================
# Path to your products JSON file. Used when you don't pass one on the
# command line. Use r"..." so Windows backslashes work, e.g.
#   JSON_FILE = r"C:\programming\product pushers\products.json"
JSON_FILE = r"C:\programming\scraper\books.json"
# ======================================================================

PROD_URL = "https://pro.svc.vinted.com"
SANDBOX_URL = "https://pro-public-sandbox.svc.vinted.com"
STATE_FILE = Path("posted_api.json")
ONTOLOGY_CACHE = Path(".vinted_ontologies.json")
BATCH = 100  # API max items per request

# Schema.org condition (scraped JSON) -> candidate option titles of the
# "condition" item attribute (titles depend on your account's country).
CONDITION_CANDIDATES = {
    "NewCondition": ["Nuevo sin etiquetas", "New without tags", "Neuf sans étiquette", "Neu ohne Etikett"],
    "UsedCondition": ["Muy bueno", "Very good", "Très bon état", "Sehr gut"],
    "RefurbishedCondition": ["Bueno", "Good", "Bon état", "Gut"],
}


# --------------------------------------------------------------------------- API

class VintedPro:
    def __init__(self, token, sandbox=False):
        if not token or "," not in token:
            sys.exit("Token missing or malformed. Expected 'ACCESS_KEY,SIGNING_KEY' "
                     "(set VINTED_PRO_TOKEN or pass --token).")
        self.access_key, self.signing_key = (s.strip() for s in token.split(",", 1))
        self.base = SANDBOX_URL if sandbox else PROD_URL

    def _sign(self, method, path, body: bytes):
        ts = str(int(time.time()))
        msg = f"{ts}.{method}.{path}.{self.access_key}.".encode() + body
        sig = hmac.new(self.signing_key.encode(), msg, hashlib.sha256).hexdigest()
        return f"t={ts},v1={sig}"

    def request(self, method, path, payload=None):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "X-Vpi-Access-Key": self.access_key,
            "X-Vpi-Hmac-Sha256": self._sign(method, path, body),
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body or None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw.decode("utf-8", "replace")
            return e.code, data

    def ontologies(self, refresh=False):
        if ONTOLOGY_CACHE.exists() and not refresh:
            return json.loads(ONTOLOGY_CACHE.read_text(encoding="utf-8"))
        code, data = self.request("GET", "/api/v1/ontologies")
        if code != 200:
            sys.exit(f"Failed to fetch ontologies ({code}): {data}")
        ONTOLOGY_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data


# ---------------------------------------------------------------------- helpers

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def find_condition_attr(onto):
    """Return (attribute_id, {option_title_lower: option_id}) for the condition attribute."""
    for a in onto.get("enumerations", {}).get("item_attributes", []):
        if "condition" in str(a.get("id", "")).lower() or "condition" in str(a.get("title", "")).lower():
            return a["id"], {o["title"].lower(): o["id"] for o in a.get("options", [])}
    return None, {}


def normalize(raw):
    """Map scraper JSON (Correos Market style) or the simple format to API fields."""
    if "price_eur" not in raw and "price" in raw:  # simple format
        p = dict(raw)
        p.setdefault("image_urls", p.get("photo_urls", []))
        return p
    desc = (raw.get("description") or "").strip()
    return {
        "sku": raw.get("id"),
        "title": (raw.get("title") or "")[:100],
        "description": desc,
        "price": raw.get("price_eur"),
        "brand": raw.get("brand"),
        "isbn": raw.get("isbn"),
        "condition": raw.get("condition"),
        "image_urls": raw.get("images") or ([raw["image"]] if raw.get("image") else []),
        "sold_out": raw.get("sold_out", False),
    }


def build_item(p, args, onto, cond_attr):
    """Return (item_dict, problems)."""
    problems = []
    title = (p.get("title") or "").strip()[:100]
    desc = (p.get("description") or "").strip()
    if p.get("isbn") and "ISBN" not in desc:
        desc += f"\n\nISBN: {p['isbn']}"
    desc = desc[:2000]
    photos = [u for u in p.get("image_urls", []) if str(u).startswith("http")]
    max_photos = onto.get("validation_rules", {}).get("max_photos") or 20
    catalog_id = p.get("catalog_id") or args.catalog_id
    package_size_id = p.get("package_size_id") or args.package_size_id

    if len(title) < 5:
        problems.append("title shorter than 5 chars")
    if len(desc) < 5:
        problems.append("description shorter than 5 chars")
    if not p.get("price"):
        problems.append("missing price")
    if not photos:
        problems.append("no public photo URLs")
    if not catalog_id:
        problems.append("no catalog_id (use --catalog-id)")
    if not package_size_id:
        problems.append("no package_size_id (use --package-size-id)")
    if p.get("sold_out"):
        problems.append("sold out at source")

    item = {
        "item_reference": str(p.get("sku") or title)[:255],
        "title": title,
        "description": desc,
        "price": round(float(p.get("price") or 0) * args.price_factor, 2),
        "currency": args.currency or onto.get("version", {}).get("currency") or "EUR",
        "brand": p.get("brand") or args.default_brand,
        "catalog_id": int(catalog_id) if catalog_id else None,
        "package_size_id": int(package_size_id) if package_size_id else None,
        "photo_urls": photos[:max_photos],
        "is_draft": bool(args.draft),
    }
    for k in ("size_id", "color_ids", "isbn", "manufacturer", "is_unisex"):
        if p.get(k):
            item[k] = p[k]
    attrs = list(p.get("item_attributes") or [])

    # condition -> item attribute
    cond_id, cond_opts = cond_attr
    cond = p.get("condition") or args.default_condition
    if cond and cond_id and not any(a.get("item_attribute_id") == cond_id for a in attrs):
        opt = None
        for cand in CONDITION_CANDIDATES.get(cond, [cond]):
            opt = cond_opts.get(str(cand).lower())
            if opt is not None:
                break
        if opt is not None:
            attrs.append({"item_attribute_id": cond_id, "item_attribute_option_ids": [opt]})
        else:
            print(f"   ! condition '{cond}' not matched to an option of '{cond_id}' "
                  f"(options: {', '.join(cond_opts)})")
    if attrs:
        item["item_attributes"] = attrs
    return item, problems


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------- commands

def cmd_ontologies(api, args):
    onto = api.ontologies(refresh=True)
    en = onto.get("enumerations", {})
    print(f"Saved full ontologies to {ONTOLOGY_CACHE}  (version: {onto.get('version')})\n")

    def walk(cats, path):
        for c in cats:
            p = path + [c["title"]]
            leaf = not c.get("catalogs")
            line = " > ".join(p)
            if leaf and (not args.find or args.find.lower() in line.lower()):
                extra = f"  sizes:{c['size_group_ids']}" if c.get("size_group_ids") else ""
                print(f"  catalog_id={c['id']:<6} {line}{extra}")
            walk(c.get("catalogs") or [], p)

    print("Leaf catalogs:")
    walk(en.get("catalogs", []), [])
    print("\nPackage sizes:")
    for ps in en.get("package_sizes", []):
        print(f"  package_size_id={ps['id']:<4} {ps['title']}  ({ps.get('weight_description', '')})")
    cid, opts = find_condition_attr(onto)
    print(f"\nCondition attribute: {cid}")
    for t, i in opts.items():
        print(f"  option_id={i:<4} {t}")
    print(f"\nValidation rules: {onto.get('validation_rules')}")


def cmd_push(api, args):
    json_path = Path(args.json_file)
    if not json_path.exists():
        sys.exit(f"JSON file not found: {json_path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {json_path.resolve()}")
    products = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(products, dict):
        products = products.get("products", [])
    products = [normalize(p) for p in products]
    onto = api.ontologies()
    cond_attr = find_condition_attr(onto)
    state = load_state()

    todo = []
    for i, p in enumerate(products, 1):
        item, problems = build_item(p, args, onto, cond_attr)
        ref = item["item_reference"]
        if ref in state and not args.validate_only:
            print(f"[{i}] already pushed: {item['title']}")
            continue
        if problems:
            print(f"[{i}] SKIP '{item['title'] or '?'}': " + "; ".join(problems))
            continue
        todo.append(item)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} item(s) to {'validate' if args.validate_only else 'push'}.")
    if not todo:
        return

    for batch in chunks(todo, BATCH):
        code, data = api.request("POST", "/api/v1/items/validate", {"items": batch})
        if code != 200:
            print(f"Validate request failed ({code}): {data}")
            return
        invalid = {r.get("item_reference"): r.get("errors") for r in (data or {}).get("items", [])
                   if not r.get("valid")}
        for ref, errs in invalid.items():
            print(f"   ! invalid '{ref}': {errs}")
        if args.validate_only:
            print(f"   {len(batch) - len(invalid)}/{len(batch)} valid")
            continue
        batch = [it for it in batch if it["item_reference"] not in invalid]
        if not batch:
            continue

        code, data = api.request("POST", "/api/v1/items", {"items": batch})
        if code == 409:
            print(f"Slot limit exceeded - nothing in this batch was created: {data}")
            return
        if code != 202:
            print(f"Create request failed ({code}): {data}")
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in data.get("items", []):
            state[r["item_reference"]] = {"item_id": r["item_id"], "pushed_at": now, "status": "SUBMITTED"}
            print(f"   + {r['item_reference']} -> {r['item_id']}")
        save_state(state)
    if not args.validate_only:
        print("Submitted. Creation is asynchronous - run 'status' in a minute to check results.")


def cmd_status(api, args):
    state = load_state()
    if not state:
        print(f"Nothing in {STATE_FILE}.")
        return
    for ref, info in state.items():
        code, data = api.request("GET", f"/api/v1/items/{info['item_id']}/status")
        if code != 200:
            print(f"{ref}: error {code} {data}")
            continue
        info["status"] = data.get("status")
        info["marketplace_item_id"] = data.get("marketplace_item_id")
        errs = f"  errors={data['errors']}" if data.get("errors") else ""
        print(f"{ref}: {data.get('status')}  vinted_id={data.get('marketplace_item_id')}{errs}")
    save_state(state)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token", default=os.environ.get("VINTED_PRO_TOKEN"),
                        help="'ACCESS_KEY,SIGNING_KEY' (default: $VINTED_PRO_TOKEN)")
    common.add_argument("--sandbox", action="store_true", help="use the dev-mode sandbox")
    ap = argparse.ArgumentParser(description="Push products via the Vinted Pro API")
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("ontologies", parents=[common], help="fetch and list catalog/package/condition ids")
    o.add_argument("--find", help="only show catalogs whose path contains this text")

    p = sub.add_parser("push", parents=[common], help="create items from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE,
                   help=f"products JSON (default: JSON_FILE setting = {JSON_FILE})")
    p.add_argument("--catalog-id", type=int, help="leaf catalog id for all items (per-item 'catalog_id' wins)")
    p.add_argument("--package-size-id", type=int, help="package size id (per-item 'package_size_id' wins)")
    p.add_argument("--currency", choices=["EUR", "GBP"], help="default: from ontologies")
    p.add_argument("--default-brand", default="Otros", help="brand when the product has none")
    p.add_argument("--default-condition", default="NewCondition")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices, e.g. 1.2")
    p.add_argument("--draft", action="store_true", help="create as drafts instead of live listings")
    p.add_argument("--validate-only", action="store_true", help="only call the validate endpoint")
    p.add_argument("--limit", type=int, default=0, help="max items this run (0 = all)")

    sub.add_parser("status", parents=[common], help="refresh status of pushed items")

    args = ap.parse_args()
    api = VintedPro(args.token, args.sandbox)
    {"ontologies": cmd_ontologies, "push": cmd_push, "status": cmd_status}[args.cmd](api, args)


if __name__ == "__main__":
    main()
