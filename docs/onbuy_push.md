# onbuy_push.py reference

This script pushes books to **OnBuy**, the UK marketplace, which now also
runs 20 European country sites. It uses the official **OnBuy / OnCommerce
API v2** (reference: [docs.api.onbuy.com](https://docs.api.onbuy.com)).
Everything below describes how the script behaves today.

Each book is matched to an **existing OnBuy product** by its ISBN, and your
listing is attached to it, like on Kaufland and eMAG. Books OnBuy doesn't know
are skipped.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Login.** The consumer key and secret key are exchanged for an access
   token (`POST /v2/auth/request-token`). The token lasts **15 minutes** and
   only works from the IP address that requested it. The script renews it
   before it runs out, and once more if OnBuy answers `401`.
2. **Catalog match.** New books are looked up by barcode:
   `GET /v2/products` with `filter[field]=product_code`, 50 ISBNs per call.
   This gives each book's **OPC** (OnBuy product code).
3. **Listing.** Matched books are sent with `POST /v2/listings` in batches.
   Each listing has:
   - OPC, SKU, condition `new`
   - price, stock, handling time
   - optionally, a delivery template, free returns and the GPSR responsible
     person

   OnBuy answers per row: created, or failed with a message such as
   "This product is restricted in your account".
4. **Updates.** OnBuy requires price and stock changes to go through
   `PUT /v2/listings/by-sku`, in batches of up to 1000:
   - **`--resend`** sends only what changed.
   - **`--zero-sold-out`** sets stock 0.
   - **`--delete-sold-out`** deletes the listing (`DELETE /v2/listings/by-sku`).

   Errors come back per SKU, for example "SKU does not exist".
5. **Several sites.** `--sites gb,es,de` (or `eur` / `all`) lists on each
   country site separately, each in its own currency. OnBuy counts API limits
   per site.
6. **Status.** `status` reads your listings (`GET /v2/listings`) and whether
   you have the buy box, i.e. the lowest price (`GET /v2/listings/check-winning`).
7. **Rate limits:** per site, GET 600 an hour, POST/PUT/DELETE 240 an hour.
   On `429` and `5xx` the script waits and retries.

## Setup

1. **Become an OnBuy seller** ([onbuy.com](https://www.onbuy.com), "Sell on
   OnBuy"). Enable the country sites you want in the Seller Control Panel.
2. **Get the API keys:** Seller Control Panel → Integrations → OnBuy API:
   the **consumer key** and **secret key**. OnBuy also gives test keys; with
   them, only test products can be found.
3. **Store them in Bitwarden** (project `product-pushers`), or for a quick
   local test:
   ```powershell
   $env:ONBUY_CONSUMER_KEY = "..."
   $env:ONBUY_SECRET_KEY = "..."
   ```
4. **Check the settings** at the top of `onbuy_push.py`:

   | Setting | Default | Meaning |
   |---|---|---|
   | `SITES` | `gb` | Default country sites. Codes: `gb fr de cz nl at be dk es fi gr hu ie it no pl pt ro se ch sk`. |
   | `EUR_RATE` | GBP 0.85, SEK 11.0, … | EUR → site currency. **Update regularly.** |
   | `HANDLING_TIME_DAYS` | 3 | Days to dispatch |
   | `DELIVERY_TEMPLATE_ID` | `None` | One of your delivery templates (shown by `check`); `None` = account default |
   | `FREE_RETURNS` | `None` | `True` / `False`, or `None` for the account default |
   | `RESPONSIBLE_PERSON` | (empty) | GPSR responsible person for EU sites: name, postal address, country code, phone, e-mail or website. Sent when the name is filled in. |

5. **Run `check`**, then `lookup <isbn>`, `push --dry-run` and
   `push --limit 1`.

## Commands

All commands take `--sites gb,es,…` (or `eur` / `all`).

| Command | What it does |
|---|---|
| `check` | Login, delivery templates per site, GPSR warning |
| `lookup ISBN` | Whether OnBuy knows the book on each site, with its OPC |
| `push [json_file]` | Matches new books, creates listings, sends updates |
| `status` | Your listings per site: price, stock, buy box, product URL |

### `push`

| Option | Meaning |
|---|---|
| `--dry-run` | Write `onbuy_listings.json` only. No credentials needed (OPC is a placeholder). |
| `--limit N` | At most N new books per site |
| `--resend` | Update prices and stock that changed |
| `--zero-sold-out` / `--delete-sold-out` | What to do with pushed books that are now sold out |
| `--quantity N` | Stock when the book has no `stock`/`quantity` field (default 1) |
| `--price-factor X` | Multiply source prices |
| `--yes` | No confirmation (used by `push_all`) |

## Input JSON format

| Field | Use |
|---|---|
| `id` or `sku` | Your SKU |
| `isbn` or `ean` | Barcode for the catalog match. ISBN-10 is converted, and the check digit is verified. |
| `price_eur` or `price` | Price in EUR, converted per site (GBP, SEK, PLN, …) |
| `stock` or `quantity` | Stock (default `--quantity`) |
| `condition` | Only `NewCondition` (listed as `new`) |
| `sold_out` | New books are skipped. For pushed ones, see the sold-out options. |

Titles, descriptions and images come from the OnBuy product. A listing only
adds your offer.

## Files the script creates

| File | Content |
|---|---|
| `onbuy_listings.json` | The last new listings built (also on `--dry-run`) |
| `onbuy_posted.json` | Per site and SKU (`gb:142534`): OPC, ISBN, listing ID, price, currency, stock, product URL, times |

Both are in `.gitignore`.

## Errors and troubleshooting

| Message | Cause | What to do |
|---|---|---|
| `OnBuy refused the login` | Wrong consumer or secret key | Check the Bitwarden secrets |
| `OnBuy refused the token (401)` | The token is tied to the IP that requested it | Run from one machine or IP address |
| `ISBN … is not in the OnBuy catalog` | OnBuy has no product with this barcode on that site | Can't be listed by this script (new products aren't built) |
| `! SKU: listing rejected: …` | OnBuy refused the listing (restricted, bad price, …) | Fix it; the book is tried again on the next run |
| `! SKU: update rejected: SKU does not exist` | The listing was removed at OnBuy | Delete its entry in `onbuy_posted.json` to list it again |
| `gave up after 4 tries (HTTP 429)` | Hourly API limit for that site reached | Wait an hour; send fewer, bigger batches |

## Limitations

- **Existing products only.** Creating new OnBuy products needs a category,
  brand, images and OnBuy's product queue. That isn't built.
- **New books only.** Book categories on OnBuy take condition `new`.
- **Exchange rates** are fixed in `EUR_RATE`, not fetched live.
- **The same copy on several sites:** each site gets its own listing. With one
  copy, it can sell twice until stock is synced across marketplaces.
- **Orders** (dispatch, tracking, cancel, refund) have their own OnBuy API
  resources. They aren't built yet; see [capabilities.md](capabilities.md).
- **Not tested against the real OnBuy API**, because there is no seller
  account yet. Only the local mock below has been used.

## Testing

```powershell
python tests\test_onbuy.py
```

The test starts a mock of the OnBuy API v2 on a free local port. It covers:

- the token request, the 15-minute token sent raw in `Authorization`, and
  automatic renewal
- barcode search, at most 50 per call, per site
- batch listings with per-row errors
- update and delete by SKU, with OnBuy's per-row errors
- several sites at once, paginated listings and check-winning
- delivery templates and `429` retries

There are 21 checks. Nothing is sent to OnBuy.
