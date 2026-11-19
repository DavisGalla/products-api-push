"""Create the seller prerequisites on an eBay SANDBOX test user through the API.

The sandbox Seller Hub pages are often broken, so this does what they would:
opt in to Business Policies, create one shipping, payment and return policy per
site, and create one inventory location. It skips whatever already exists, so
running it twice is safe.

    bws run --project-id <sandbox project id> -- python tools/ebay_sandbox_setup.py
    bws run --project-id <sandbox project id> -- python tools/ebay_sandbox_setup.py --markets all
    bws run --project-id <sandbox project id> -- python tools/ebay_sandbox_setup.py --markets EBAY_DE,EBAY_FR --dry-run

Sandbox only: the script has no production mode on purpose. The shipping policy
is "DHL parcel" on ebay.de and local pickup on the other sites: it is only there
so books can be published for testing, it is not a real shipping setup.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ebay_push  # noqa: E402

CATEGORY = [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}]
LOCATION_KEY = "sandbox-warehouse"
# A German address is enough for the sandbox; change it for other sites.
ADDRESS = {"addressLine1": "Unter den Linden 1", "city": "Berlin",
           "postalCode": "10117", "country": "DE"}


def existing(eb, kind, key):
    return (eb.call("GET", f"/sell/account/v1/{kind}",
                    params={"marketplace_id": eb.market}).get(key) or [])


def wanted_policies(eb):
    if eb.market == "EBAY_DE":
        ship = {"handlingTime": {"unit": "DAY", "value": 1},
                "shippingOptions": [{
                    "optionType": "DOMESTIC", "costType": "FLAT_RATE",
                    "shippingServices": [{
                        "sortOrder": 1, "shippingCarrierCode": "DHL",
                        "shippingServiceCode": "DE_DHLPaket",
                        "shippingCost": {"value": "3.99", "currency": eb.currency},
                        "freeShipping": False}]}]}
    else:
        ship = {"handlingTime": {"unit": "DAY", "value": 1}, "localPickup": True}
    return [
        ("fulfillment_policy", "fulfillmentPolicies", "sandbox shipping", ship),
        ("payment_policy", "paymentPolicies", "sandbox payment", {}),
        ("return_policy", "returnPolicies", "sandbox returns", {
            "returnsAccepted": True, "returnPeriod": {"value": 30, "unit": "DAY"},
            "returnShippingCostPayer": "BUYER", "refundMethod": "MONEY_BACK"}),
    ]


def setup_market(market, dry):
    eb = ebay_push.Ebay(True, market)  # True = sandbox
    eb.login()
    print(f"--- {market} ---")
    for kind, key, name, extra in wanted_policies(eb):
        try:
            have = existing(eb, kind, key)
        except RuntimeError:
            if not dry:
                raise
            have = []
        if have:
            print(f"{kind}: exists ({have[0].get('name')})")
            continue
        print(f"{kind}: missing")
        if dry:
            continue
        body = {"name": name, "marketplaceId": eb.market, "categoryTypes": CATEGORY, **extra}
        try:
            eb.call("POST", f"/sell/account/v1/{kind}", body)
            print("  -> created")
        except RuntimeError as e:
            print(f"  ! {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--markets", default="EBAY_DE",
                    help="comma separated sites, or 'all' (default EBAY_DE)")
    ap.add_argument("--dry-run", action="store_true", help="only report what is missing")
    a = ap.parse_args()
    markets = list(ebay_push.MARKETS) if a.markets == "all" else [m.strip() for m in a.markets.split(",")]
    for m in markets:
        if m not in ebay_push.MARKETS:
            sys.exit(f"Unknown site {m}. Known: {', '.join(ebay_push.MARKETS)}")

    eb = ebay_push.Ebay(True, markets[0])
    eb.login()

    progs = eb.call("GET", "/sell/account/v1/program/get_opted_in_programs").get("programs") or []
    opted = any(p.get("programType") == "SELLING_POLICY_MANAGEMENT" for p in progs)
    print(f"Business Policies opted in: {opted}")
    if not opted and not a.dry_run:
        eb.call("POST", "/sell/account/v1/program/opt_in", {"programType": "SELLING_POLICY_MANAGEMENT"})
        print("  -> opted in")

    locs = eb.call("GET", "/sell/inventory/v1/location").get("locations") or []
    if locs:
        print(f"inventory location: exists ({locs[0]['merchantLocationKey']})")
    else:
        print("inventory location: missing")
        if not a.dry_run:
            eb.call("POST", f"/sell/inventory/v1/location/{LOCATION_KEY}", {
                "location": {"address": ADDRESS}, "name": "Sandbox warehouse",
                "merchantLocationStatus": "ENABLED", "locationTypes": ["WAREHOUSE"]})
            print("  -> created")

    for m in markets:
        setup_market(m, a.dry_run)
    print("Done." if not a.dry_run else "Dry run: nothing was changed.")


if __name__ == "__main__":
    main()
