#!/usr/bin/env python3
"""
Push products to Allegro (allegro.pl, Poland) through the official Allegro
REST API (https://developer.allegro.pl, gateway https://api.allegro.pl).

Each book is listed as an offer on Allegro's product catalog: the product is
referenced by its ISBN/EAN (productSet[].product = {id: EAN, idType: GTIN}),
so Allegro supplies title, images and parameters. Books not in Allegro's
catalog are skipped. Your offer adds price (PLN), stock, condition, delivery
(your shipping rates), after-sales conditions (return policy, implied
warranty) and the GPSR data (responsible producer, safety information).
EUR prices are converted with EUR_TO_PLN (or --eur-rate).

ONE-TIME SETUP (see docs/allegro_push.md)
  1. apps.developer.allegro.pl: register an app of type "device" (no browser
     needed). You get a Client ID and Client Secret. (Two-step login must be
     enabled on the Allegro account.)
  2. Allegro account (business): a shipping rates set, a return policy, an
     implied warranty, and a responsible producer (GPSR).
  3. Authorize the app for your account once:
        python allegro_push.py auth
     (open the printed link, confirm the code, the script waits). Tokens are
     saved to allegro_token.json (not in git) and refreshed automatically.
  Credentials (PowerShell or Bitwarden):
      $env:ALLEGRO_CLIENT_ID = "..."
      $env:ALLEGRO_CLIENT_SECRET = "..."

USAGE
    pip install requests
    python allegro_push.py auth                     # one-time authorization (device code)
    python allegro_push.py check                    # account, shipping rates, policies, GPSR
    python allegro_push.py lookup 9788409591923     # is this ISBN in Allegro's catalog?
    python allegro_push.py push --dry-run           # build the offers, send nothing
    python allegro_push.py push --limit 1           # one real offer
    python allegro_push.py push                     # everything
    python allegro_push.py push --resend --end-sold-out   # sync price/stock, end sold-out offers
    python allegro_push.py status                   # active / ended, price, stock

NOTE: prices on Allegro are in PLN and include Polish VAT (books: 5%).
Poland has no fixed book price law; check EUR_TO_PLN regularly.
"""
import argparse
import base64
import json
import os
import sys
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

# ============================== SETTINGS ==============================
# Products JSON used when you don't pass one on the command line.
JSON_FILE = r"C:\programming\scraper\books.json"

EUR_TO_PLN = 4.25           # exchange rate for your EUR prices (check it regularly; --eur-rate overrides)
CURRENCY = "PLN"
HANDLING_TIME = "P2D"       # time to ship: PT24H, P2D, P3D, P4D, P5D, P7D, P10D, ...

# Source condition -> value of Allegro's "Stan" (condition) offer parameter (id 11323).
# Check the values for your category with `lookup <isbn>`. Not listed here = skipped.
CONDITION_PARAMETER_ID = "11323"
CONDITION_VALUES = {
    "NewCondition": "11323_1",       # Nowy
    "UsedCondition": "11323_2",      # Używany
}

# "" = the first one in your account (see `check`).
SHIPPING_RATES_ID = ""
RETURN_POLICY_ID = ""
IMPLIED_WARRANTY_ID = ""
RESPONSIBLE_PRODUCER_ID = ""        # GPSR responsible producer (e.g. the publisher), required since 13 Dec 2024
# GPSR safety information sent with every book (plain text, Polish).
SAFETY_TEXT = "Książka. Produkt nie wymaga szczególnych ostrzeżeń dotyczących bezpieczeństwa."
INVOICE = "VAT"                     # VAT, VAT_MARGIN, WITHOUT_VAT or NO_INVOICE
# ======================================================================

MEDIA = "application/vnd.allegro.public.v1+json"
STATE_FILE = Path("allegro_posted.json")
PREVIEW_FILE = Path("allegro_offers.json")
TOKEN_FILE = Path("allegro_token.json")


def hosts():
    sandbox = os.environ.get("ALLEGRO_SANDBOX") in ("1", "true", "yes")
    domain = "allegro.pl.allegrosandbox.pl" if sandbox else "allegro.pl"
    api = os.environ.get("ALLEGRO_API_URL") or f"https://api.{domain}"
    auth = os.environ.get("ALLEGRO_AUTH_URL") or f"https://{domain}"
    return api.rstrip("/"), auth.rstrip("/")


class ApiError(Exception):
    def __init__(self, status, payload):
        self.status, self.payload = status, payload
        super().__init__(f"{status} {error_text(payload)}")


def error_text(p):
    if not isinstance(p, dict):
        return str(p)[:300]
    errs = p.get("errors") or []
    if errs:
        return "; ".join(f"{e.get('userMessage') or e.get('message') or e.get('code')}"
                         + (f" ({e['path']})" if e.get("path") else "") for e in errs)
    return p.get("error_description") or p.get("error") or p.get("message") or json.dumps(p)[:300]


# --------------------------------------------------------------------------- API

class Allegro:
    def __init__(self):
        self.client = os.environ.get("ALLEGRO_CLIENT_ID")
        self.secret = os.environ.get("ALLEGRO_CLIENT_SECRET")
        missing = [k for k, v in (("ALLEGRO_CLIENT_ID", self.client), ("ALLEGRO_CLIENT_SECRET", self.secret)) if not v]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.api, self.auth = hosts()
        self.basic = "Basic " + base64.b64encode(f"{self.client}:{self.secret}".encode()).decode()
        self.tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8")) if TOKEN_FILE.exists() else {}

    # ---- tokens
    def oauth(self, path, **form):
        r = requests.post(f"{self.auth}{path}", data=form, timeout=60,
                          headers={"Authorization": self.basic, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:200]}
        return r.status_code, data

    def save_token(self, data):
        self.tok = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                    "expires_at": int(time.time()) + int(data.get("expires_in", 43199)),
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        TOKEN_FILE.write_text(json.dumps(self.tok, indent=2), encoding="utf-8")
        return self.tok

    def device_auth(self, wait=600):
        code, data = self.oauth(f"/auth/oauth/device?client_id={self.client}", client_id=self.client)
        if code != 200 or "device_code" not in data:
            sys.exit(f"Could not start the authorization ({code}): {error_text(data)}")
        uc = data["user_code"]
        print("Open this link and confirm the code to authorize the app for your Allegro account:\n")
        print(f"    {data.get('verification_uri_complete') or data.get('verification_uri')}")
        print(f"    code: {' '.join(uc[i:i + 3] for i in range(0, len(uc), 3))}\n")
        print("Waiting for your confirmation...")
        interval = int(data.get("interval", 5))
        deadline = time.time() + min(wait, int(data.get("expires_in", 3600)))
        while time.time() < deadline:
            time.sleep(interval)
            code, tok = self.oauth("/auth/oauth/token", grant_type="urn:ietf:params:oauth:grant-type:device_code",
                                   device_code=data["device_code"])
            if code == 200 and tok.get("access_token"):
                return self.save_token(tok)
            err = tok.get("error")
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                sys.exit(f"Authorization failed: {error_text(tok)}")
        sys.exit("Authorization timed out - run `auth` again.")

    def refresh(self):
        rt = self.tok.get("refresh_token") or os.environ.get("ALLEGRO_REFRESH_TOKEN")
        if not rt:
            sys.exit("No Allegro authorization yet. Run `python allegro_push.py auth` first "
                     "(or set ALLEGRO_REFRESH_TOKEN).")
        code, data = self.oauth("/auth/oauth/token", grant_type="refresh_token", refresh_token=rt)
        if code != 200 or not data.get("access_token"):
            sys.exit(f"Could not refresh the Allegro token ({error_text(data)}). Authorize again: "
                     "`python allegro_push.py auth` (the authorization lasts at most 3 months).")
        self.save_token(data)           # the refresh token is single-use: always keep the new one

    def access_token(self):
        if not self.tok.get("access_token") or self.tok.get("expires_at", 0) - time.time() < 10 * 60:
            self.refresh()
        return self.tok["access_token"]

    # ---- business calls
    def call(self, method, path, params=None, body=None, retries=5, _again=True):
        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {self.access_token()}", "Accept": MEDIA, "Accept-Language": "en-US"}
            if body is not None:
                headers["Content-Type"] = MEDIA
            url = path if path.startswith("http") else f"{self.api}{path}"
            r = requests.request(method, url, params=params, headers=headers, timeout=60,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
            if r.status_code == 401 and _again:
                self.tok["expires_at"] = 0
                self.refresh()
                return self.call(method, path, params, body, retries, _again=False)
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After") or 2 ** attempt)
                print(f"   (API returned {r.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            try:
                data = r.json() if r.content else {}
            except ValueError:
                data = {"message": r.text[:300]}
            if r.status_code == 403:
                sys.exit(f"403 Forbidden: {error_text(data)} (does the app have the sale/offers scopes?)")
            return r, data
        return r, {"message": "gave up after retries"}

    def get(self, path, params=None):
        r, data = self.call("GET", path, params=params)
        if r.status_code != 200:
            raise ApiError(r.status_code, data)
        return data

    def me(self):
        return self.get("/me")

    def first_id(self, path, key):
        items = self.get(path).get(key) or []
        return (items[0].get("id"), items[0].get("name")) if items else (None, None)

    def product(self, ean):
        data = self.get("/sale/products", {"phrase": ean, "mode": "GTIN"})
        return (data.get("products") or [None])[0]

    def create(self, body):
        r, data = self.call("POST", "/sale/product-offers", body=body)
        if r.status_code == 202 and r.headers.get("Location"):
            return self.wait_operation(r.headers["Location"], data)
        if r.status_code not in (200, 201):
            raise ApiError(r.status_code, data)
        return data

    def wait_operation(self, location, data, timeout=120):
        offer_id = ((data or {}).get("id") or ((data or {}).get("offer") or {}).get("id"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, op = self.call("GET", location)
            if r.status_code in (200, 303) or (op.get("operation") or {}).get("status") == "COMPLETED":
                offer_id = offer_id or (op.get("offer") or {}).get("id")
                return self.get(f"/sale/product-offers/{offer_id}")
            if r.status_code not in (202,):
                raise ApiError(r.status_code, op)
            offer_id = offer_id or (op.get("offer") or {}).get("id")
            time.sleep(2)
        raise ApiError("timeout", {"message": f"offer {offer_id} still processing - check `status` later"})

    def patch(self, offer_id, body):
        r, data = self.call("PATCH", f"/sale/product-offers/{offer_id}", body=body)
        if r.status_code == 202 and r.headers.get("Location"):
            return self.wait_operation(r.headers["Location"], {"id": offer_id})
        if r.status_code not in (200, 201):
            raise ApiError(r.status_code, data)
        return data

    def publication(self, offer_ids, action):
        command = str(uuid.uuid4())
        r, data = self.call("PUT", f"/sale/offer-publication-commands/{command}", body={
            "offerCriteria": [{"offers": [{"id": i} for i in offer_ids], "type": "CONTAINS_OFFERS"}],
            "publication": {"action": action}})
        if r.status_code not in (200, 201):
            raise ApiError(r.status_code, data)
        for _ in range(30):
            summary = self.get(f"/sale/offer-publication-commands/{command}")
            tc = summary.get("taskCount") or {}
            if tc.get("total") is not None and tc.get("success", 0) + tc.get("failed", 0) >= tc["total"]:
                return tc
            time.sleep(1)
        return summary.get("taskCount") or {}

    def offers_by_external(self, skus):
        out = []
        for i in range(0, len(skus), 50):
            out += self.get("/sale/offers", [("external.id", s) for s in skus[i:i + 50]] + [("limit", 1000)]).get("offers") or []
        return out


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


def money(price, factor, rate):
    """price * factor * rate as a 2-decimal string, rounded half-up (36.125 -> '36.13')."""
    v = Decimal(str(price)) * Decimal(str(factor)) * Decimal(str(rate))
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize(raw):
    return {
        "sku": str(raw.get("sku") or raw.get("id") or "")[:100],
        "title": raw.get("title") or "",
        "price": raw.get("price_eur", raw.get("price")),
        "ean": to_ean(raw.get("isbn") or raw.get("ean")),
        "raw_code": raw.get("isbn") or raw.get("ean"),
        "condition": raw.get("condition") or "NewCondition",
        "quantity": raw.get("quantity"),
        "sold_out": raw.get("sold_out", False),
    }


def problems_of(p):
    probs = []
    if not p["sku"]:
        probs.append("no id/sku")
    if not p["price"]:
        probs.append("no price")
    if not p["raw_code"]:
        probs.append("no ISBN/EAN (needed to match the Allegro catalog)")
    elif not p["ean"]:
        probs.append(f"invalid ISBN/EAN '{p['raw_code']}'")
    if p["condition"] not in CONDITION_VALUES:
        probs.append(f"no Allegro condition for '{p['condition']}' (set CONDITION_VALUES)")
    if p["sold_out"]:
        probs.append("sold out at source")
    return probs


def offer_body(p, a, ctx):
    product = {"product": {"id": p["ean"], "idType": "GTIN"},
               "safetyInformation": {"type": "TEXT", "description": SAFETY_TEXT}}
    if ctx.get("producer"):
        product["responsibleProducer"] = {"type": "ID", "id": ctx["producer"]}
    body = {
        "productSet": [product],
        "parameters": [{"id": CONDITION_PARAMETER_ID, "valuesIds": [CONDITION_VALUES[p["condition"]]]}],
        "sellingMode": {"format": "BUY_NOW", "price": {"amount": money(p["price"], a.price_factor, a.eur_rate),
                                                       "currency": CURRENCY}},
        "stock": {"available": max(1, int(p["quantity"] or a.quantity)), "unit": "UNIT"},
        "delivery": {"handlingTime": a.handling_time},
        "payments": {"invoice": INVOICE},
        "external": {"id": p["sku"]},
        "publication": {"status": "ACTIVE"},
    }
    if ctx.get("shipping"):
        body["delivery"]["shippingRates"] = {"id": ctx["shipping"]}
    after = {k: {"id": v} for k, v in (("returnPolicy", ctx.get("returns")), ("impliedWarranty", ctx.get("warranty"))) if v}
    if after:
        body["afterSalesServices"] = after
    return body


def account_context(api, quiet=False):
    ctx = {"shipping": SHIPPING_RATES_ID or None, "returns": RETURN_POLICY_ID or None,
           "warranty": IMPLIED_WARRANTY_ID or None, "producer": RESPONSIBLE_PRODUCER_ID or None}
    if api is None:
        return ctx
    lookups = [("shipping", "/sale/shipping-rates", "shippingRates", "shipping rates set"),
               ("returns", "/after-sales-service-conditions/return-policies", "returnPolicies", "return policy"),
               ("warranty", "/after-sales-service-conditions/implied-warranties", "impliedWarranties", "implied warranty"),
               ("producer", "/sale/responsible-producers", "responsibleProducers", "responsible producer (GPSR)")]
    missing = []
    for key, path, field, label in lookups:
        if not ctx[key]:
            ctx[key], name = api.first_id(path, field)
            if not ctx[key]:
                missing.append(label)
            elif not quiet:
                print(f"   using {label}: {name} ({ctx[key]})")
    ctx["missing"] = missing
    return ctx


# --------------------------------------------------------------------- commands

def cmd_auth(api, a):
    tok = api.device_auth()
    exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(tok["expires_at"]))
    print(f"Authorized. Tokens saved to {TOKEN_FILE.resolve()} (access token valid until {exp}, refreshed automatically;")
    print("   the authorization itself lasts at most 3 months without use). On a server: store refresh_token from")
    print("   that file in Bitwarden as ALLEGRO_REFRESH_TOKEN (it changes on every refresh, so keep the file too).")


def cmd_check(api, a):
    me = api.me()
    print(f"Connected as {me.get('login')} (id {me.get('id')}"
          + (f", company {me['company'].get('name')}" if me.get("company") else "")
          + f", marketplace {(me.get('baseMarketplace') or {}).get('id')})")
    ctx = account_context(api)
    for m in ctx["missing"]:
        print(f"   ! no {m} in your account - create one on Allegro (Sales Center) before pushing")


def cmd_lookup(api, a):
    ean = to_ean(a.isbn)
    if not ean:
        sys.exit(f"Not a valid ISBN/EAN: {a.isbn}")
    prod = api.product(ean)
    if not prod:
        print(f"{ean}: NOT in the Allegro catalog")
        return
    print(f"{ean}: {prod.get('name')}  (product {prod.get('id')}, category {(prod.get('category') or {}).get('id')})")


def cmd_push(api, a):
    path = Path(a.json_file)
    if not path.exists():
        sys.exit(f"JSON file not found: {path.resolve()}\n"
                 f"Set JSON_FILE at the top of {Path(__file__).name} or pass a path to 'push'.")
    print(f"Reading {path.resolve()}  (prices converted at 1 EUR = {a.eur_rate} PLN)")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    state = load_state()

    new, resend, end = [], [], []
    for i, r in enumerate(raw, 1):
        p = normalize(r)
        if p["sold_out"] and a.end_sold_out and p["sku"] in state and state[p["sku"]].get("status") != "ENDED":
            end.append(p)
            continue
        probs = problems_of(p)
        if probs:
            print(f"[{i}] SKIP '{p['title']}': " + "; ".join(probs))
            continue
        if p["sku"] in state:
            if a.resend:
                resend.append(p)
            else:
                print(f"[{i}] already pushed: {p['title']}")
            continue
        new.append(p)
    if a.limit:
        new = new[: a.limit]

    ctx = account_context(api) if new else {}
    if api and new:
        if ctx["missing"]:
            sys.exit("Missing in your Allegro account: " + ", ".join(ctx["missing"]) + ". Create them first (see `check`).")
        if not a.no_match_check:
            print(f"Checking {len(new)} ISBN(s) against the Allegro catalog...")
            keep = []
            for p in new:
                if api.product(p["ean"]):
                    keep.append(p)
                else:
                    print(f"   SKIP '{p['title']}': EAN {p['ean']} is not in the Allegro catalog")
            new = keep
    elif new:
        print("(dry run without credentials: catalog check and account settings skipped)")

    print(f"{len(new)} new offer(s)" + (f", {len(resend)} to update" if resend else "")
          + (f", {len(end)} sold-out to end" if end else "") + ".")
    if not (new or resend or end):
        return
    PREVIEW_FILE.write_text(json.dumps([offer_body(p, a, ctx) for p in new], ensure_ascii=False, indent=2), encoding="utf-8")
    if a.dry_run:
        print(f"Dry run: offers written to {PREVIEW_FILE.resolve()} - nothing sent.")
        return
    if not a.yes:
        if input(f"This will change {len(new) + len(resend) + len(end)} LIVE offer(s) on Allegro. Type 'yes': ").strip().lower() != "yes":
            return
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")

    created = 0
    for p in new:
        body = offer_body(p, a, ctx)
        try:
            offer = api.create(body)
        except ApiError as e:
            print(f"   ! {p['sku']}: {e}")
            continue
        errs = (offer.get("validation") or {}).get("errors") or []
        status = (offer.get("publication") or {}).get("status")
        state[p["sku"]] = {"offer_id": offer.get("id"), "ean": p["ean"], "price": body["sellingMode"]["price"]["amount"],
                           "stock": body["stock"]["available"], "status": status, "pushed_at": now()}
        save_state(state)
        created += 1
        print(f"   + {p['sku']}: offer {offer.get('id')}  {body['sellingMode']['price']['amount']} PLN x{body['stock']['available']}"
              f"  [{status}]" + (f"  validation: {error_text({'errors': errs})}" if errs else ""))

    back = []
    for p in resend:
        info = state[p["sku"]]
        price = money(p["price"], a.price_factor, a.eur_rate)
        stock = max(1, int(p["quantity"] or a.quantity))
        patch = {}
        if price != info.get("price"):
            patch["sellingMode"] = {"price": {"amount": price, "currency": CURRENCY}}
        if stock != info.get("stock"):
            patch["stock"] = {"available": stock, "unit": "UNIT"}
        if patch:
            try:
                api.patch(info["offer_id"], patch)
            except ApiError as e:
                print(f"   ! {p['sku']}: update failed: {e}")
                continue
            info.update(price=price, stock=stock, updated_at=now())
            print(f"   ~ {p['sku']}: {price} PLN x{stock}")
        if info.get("status") == "ENDED":
            back.append(p["sku"])
        save_state(state)
    if back:
        tc = api.publication([state[s]["offer_id"] for s in back], "ACTIVATE")
        print(f"   ^ reactivated {tc.get('success', 0)} offer(s)" + (f", {tc.get('failed')} failed" if tc.get("failed") else ""))
        if tc.get("success"):
            for s in back:
                state[s]["status"] = "ACTIVE"
            save_state(state)

    if end:
        tc = api.publication([state[p["sku"]]["offer_id"] for p in end], "END")
        print(f"   - ended {tc.get('success', 0)} sold-out offer(s)" + (f", {tc.get('failed')} failed" if tc.get("failed") else ""))
        if tc.get("success"):
            for p in end:
                state[p["sku"]].update(status="ENDED", updated_at=now())
            save_state(state)
    print(f"Done: {created} created. Run `status` to see whether they are active.")


def cmd_status(api, a):
    state = load_state()
    if not state:
        print("No offers yet.")
        return
    found = {(o.get("external") or {}).get("id"): o for o in api.offers_by_external(sorted(state))}
    for sku in sorted(state):
        o = found.get(sku)
        if not o:
            print(f"{sku}: offer {state[sku].get('offer_id')} not found on Allegro")
            continue
        pub = (o.get("publication") or {}).get("status")
        price = ((o.get("sellingMode") or {}).get("price") or {}).get("amount")
        stock = o.get("stock") or {}
        print(f"{sku}: {pub}  price={price} PLN  stock={stock.get('available')}  sold(30d)={stock.get('sold', 0)}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Push offers to Allegro (REST API)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="authorize the app for your Allegro account (device code)")
    sub.add_parser("check", help="account, shipping rates, after-sales conditions, GPSR producer")
    s = sub.add_parser("lookup", help="check whether Allegro's catalog knows an ISBN/EAN")
    s.add_argument("isbn")
    p = sub.add_parser("push", help="create offers from a JSON file")
    p.add_argument("json_file", nargs="?", default=JSON_FILE, help=f"default: {JSON_FILE}")
    p.add_argument("--quantity", type=int, default=1, help="stock per offer (per-item 'quantity' wins)")
    p.add_argument("--price-factor", type=float, default=1.0, help="multiply source prices")
    p.add_argument("--eur-rate", type=float, default=EUR_TO_PLN, help=f"EUR -> PLN rate (default {EUR_TO_PLN})")
    p.add_argument("--handling-time", default=HANDLING_TIME, help=f"time to ship, ISO 8601 (default {HANDLING_TIME})")
    p.add_argument("--resend", action="store_true", help="update price/stock of already pushed offers")
    p.add_argument("--end-sold-out", action="store_true", help="end pushed offers that are now sold out")
    p.add_argument("--no-match-check", action="store_true", help="skip the catalog lookup")
    p.add_argument("--dry-run", action="store_true", help="write allegro_offers.json, send nothing")
    p.add_argument("--limit", type=int, default=0, help="max new offers this run (0 = all)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("status", help="list your offers: active / ended, price, stock")
    a = ap.parse_args()
    if a.cmd == "push" and a.dry_run:
        creds = all(os.environ.get(k) for k in ("ALLEGRO_CLIENT_ID", "ALLEGRO_CLIENT_SECRET"))
        if not (creds and (TOKEN_FILE.exists() or os.environ.get("ALLEGRO_REFRESH_TOKEN"))):
            return cmd_push(None, a)             # no credentials needed to build the preview
    api = Allegro()
    try:
        {"auth": cmd_auth, "check": cmd_check, "lookup": cmd_lookup, "push": cmd_push, "status": cmd_status}[a.cmd](api, a)
    except ApiError as e:
        sys.exit(f"Allegro API error {e}")


if __name__ == "__main__":
    main()
