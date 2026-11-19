"""Exchange an eBay OAuth authorization code for a refresh token.

Run it with the keyset in the environment (for example through `bws run`):

    bws run --project-id <id> -- python tools/ebay_get_refresh_token.py --sandbox

It asks you to paste the redirect URL (the address bar after you click Agree on
eBay's consent page). The paste is hidden, so the code never shows up on screen
or in a chat. The refresh token is NOT printed: it is copied to the Windows
clipboard so you can paste it straight into Bitwarden as EBAY_REFRESH_TOKEN.

Needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (asked for, hidden, if missing) and
the RuName (--runame or EBAY_RUNAME).
"""
import argparse
import base64
import getpass
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import requests


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sandbox", action="store_true", help="use api.sandbox.ebay.com")
    ap.add_argument("--runame", default=os.environ.get("EBAY_RUNAME"),
                    help="the RuName used in the authorize link (or EBAY_RUNAME)")
    args = ap.parse_args()

    client_id = (os.environ.get("EBAY_CLIENT_ID") or getpass.getpass("EBAY_CLIENT_ID (hidden): ")).strip()
    secret = (os.environ.get("EBAY_CLIENT_SECRET") or getpass.getpass("EBAY_CLIENT_SECRET (hidden): ")).strip()
    # Lengths only, so a wrong paste is visible without showing the values.
    print(f"client id: {len(client_id)} chars, starts with SBX marker: {'-SBX-' in client_id}; "
          f"secret: {len(secret)} chars, starts with SBX-: {secret.startswith('SBX-')}")
    runame = args.runame or input("RuName: ").strip()

    pasted = getpass.getpass("Paste the redirect URL (hidden), then Enter: ").strip()
    query = parse_qs(urlparse(pasted).query)
    code = (query.get("code") or [pasted])[0]  # accept a bare code too

    host = "api.sandbox.ebay.com" if args.sandbox else "api.ebay.com"
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    r = requests.post(
        f"https://{host}/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": runame},
        timeout=30,
    )
    if r.status_code != 200:
        # error body has no secrets, only the reason (invalid_grant, ...)
        hint = ("The App ID / Cert ID pair was rejected: re-copy both from the "
                "sandbox column of Application Keys." if "invalid_client" in r.text else
                "The code is single-use and lasts about 5 minutes: "
                "open the authorize link again and retry.")
        sys.exit(f"eBay returned {r.status_code}: {r.text[:300]}\n{hint}")

    data = r.json()
    token = data.get("refresh_token")
    if not token:
        sys.exit("eBay answered without a refresh_token.")

    subprocess.run(["clip"], input=token.encode(), check=True)
    days = int(data.get("refresh_token_expires_in", 0)) // 86400
    print(f"Refresh token copied to the clipboard (valid ~{days} days). "
          "Paste it into Bitwarden as EBAY_REFRESH_TOKEN, then clear the clipboard.")


if __name__ == "__main__":
    main()
