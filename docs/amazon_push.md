# amazon_push.py reference

This script pushes products to the **European Amazon marketplaces** through the
official Selling Partner API (SP-API). Everything below describes how it
behaves today.

| Code | Store | Currency | Marketplace ID |
|---|---|---|---|
| `ES` | amazon.es | EUR | `A1RKKUPIHCS9HS` |
| `DE` | amazon.de | EUR | `A1PA6795UKMFR9` |
| `FR` | amazon.fr | EUR | `A13V1IB3VIYZZH` |
| `IT` | amazon.it | EUR | `APJ6JRA9NG5V4` |
| `NL` | amazon.nl | EUR | `A1805IZSGTT6HS` |
| `BE` | amazon.com.be | EUR | `AMEN7PMS2XZ99` |
| `PL` | amazon.pl | PLN | `A1C3SOZRARQ6R3` |
| `SE` | amazon.se | SEK | `A2NODRKZP88ZB9` |
| `IE` | amazon.ie | EUR | `A28R8C7NBKEWEA` |
| `UK` | amazon.co.uk | GBP | `A1F83G8C2ARO7P` (not EU, so only used when you name it) |

`--markets all` means the nine EU stores, which excludes UK.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [Prices and currencies](#prices-and-currencies)
- [What is sent to Amazon](#what-is-sent-to-amazon)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

Amazon has a single product page per book (one ASIN), and many sellers add
their own offer to it. For each product, and for **each selected marketplace**,
the script does the following:

1. It looks the product's **ISBN/EAN** up in that marketplace's catalog with
   the Catalog Items API, `searchCatalogItems`. A book can exist on amazon.es
   but not on amazon.se, so each marketplace is checked separately.
2. If the marketplace has the book, the script creates an **offer-only
   listing** on that ASIN with the Listings Items API, `putListingsItem`,
   using `requirements: LISTING_OFFER_ONLY`. The offer carries your price,
   converted to the local currency, plus the condition and stock.
3. If the marketplace doesn't have the book, it's **skipped there** and the
   other marketplaces still get their offer. Creating new catalog pages needs
   much more data (author, binding, publisher data, compliant images) than
   the scraped feed has.

Amazon uses the catalog page's own title, images and description. Your
scraped text and photos aren't sent.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**Amazon account.** You only need to do this once.

1. You need a **Professional** seller account on **Seller Central Europe**.
   Enable each marketplace you want to sell in under **Settings → Account Info
   → Marketplaces**. If a marketplace isn't enabled, Amazon rejects offers
   for it.
2. Go to **Seller Central → Apps and Services → Develop Apps** and register as
   a developer. Then create a **private SP-API app** with the **Product
   Listing** role.
3. On the app, choose **Authorize** to generate a **refresh token**
   (`Atzr|...`). One token covers every European marketplace of your account.
   The app page also shows the **LWA client ID** and **client secret**.
4. Find your **Seller ID** (Merchant Token) under **Settings → Account Info →
   Your Merchant Token**. It's the same for every EU marketplace.

**Environment variables:**

```powershell
$env:AMAZON_LWA_CLIENT_ID     = "amzn1.application-oa2-client...."
$env:AMAZON_LWA_CLIENT_SECRET = "..."
$env:AMAZON_REFRESH_TOKEN     = "Atzr|..."
$env:AMAZON_SELLER_ID         = "A1B2C3D4E5F6G7"
```

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `DEFAULT_MARKETS` | Marketplaces used when you don't pass `--markets` (default `["ES"]`). |
| `EUR_RATES` | Exchange rates for non-euro marketplaces. **Update these before you push to SE, PL or UK.** |
| `CONDITION_MAP` | Maps your source condition to Amazon's `condition_type`. |

## Commands

### `push`

```powershell
python amazon_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--markets` | `DEFAULT_MARKETS` | Comma-separated codes such as `ES,DE,FR`, or `all` for the nine EU stores. |
| `--quantity` | `1` | Stock per offer. A product's own `quantity` takes precedence. |
| `--handling-days` | account default | Maximum days until you ship (`lead_time_to_ship_max_days`). |
| `--price-factor` | `1.0` | Multiplies every source price before currency conversion. See the [fixed book price laws](#limitations). |
| `--validate-only` | off | Amazon validates each offer (`mode=VALIDATION_PREVIEW`) and nothing is listed. |
| `--limit N` | `0` (all) | Processes at most N products. Each product can create one offer per marketplace. |
| `--yes` | off | Skips the confirmation prompt before live offers are created. |
| `--sandbox` | off | Uses the SP-API sandbox. It only returns canned responses, so it can't test your real data. |

Use this order for your first run:

```powershell
python amazon_push.py push --validate-only                # Amazon.es check, nothing is listed
python amazon_push.py push --limit 1                      # one real offer on Amazon.es
python amazon_push.py status                              # after a few minutes: BUYABLE?
python amazon_push.py push --markets all --validate-only  # check the other stores
python amazon_push.py push --markets all                  # everything, everywhere
```

At the end, the script prints a summary such as `Done: 9 submitted, 16 not in
catalog, 0 rejected/failed.`

### `status`

For every offer in `amazon_posted.json`, this command calls `getListingsItem`
in that offer's marketplace. It prints `LIVE` when the listing status includes
`BUYABLE`, along with any issues Amazon reports. Use `--markets DE` (for
example) to check only some stores.

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Purpose |
|---|---|
| `id` or `sku` | Your SKU on Amazon (at most 40 characters). The same SKU is used in every marketplace. |
| `isbn` or `ean` | **Required.** ISBN-10, ISBN-13 or EAN. Hyphens and spaces are removed. |
| `price_eur` or `price` | Price **in EUR, including VAT**. It's converted for SEK, PLN and GBP stores. |
| `condition` | See [Condition](#condition). Defaults to new. |
| `quantity` | Optional per-product stock. |
| `sold_out` | `true` means the product is skipped. |
| `title` | Only used in log messages. |

A product is skipped everywhere if it has no SKU, no price, no ISBN/EAN or an
invalid one, or is sold out. It's skipped in a single marketplace if that
marketplace's catalog doesn't have it.

### Condition

| Source value | Amazon `condition_type` |
|---|---|
| `NewCondition` (or missing) | `new_new` |
| `UsedCondition` | `used_very_good` |
| `RefurbishedCondition` | `used_good` (books have no refurbished condition) |
| `DamagedCondition` | `used_acceptable` |
| an Amazon value, for example `used_like_new` | used as-is |

## Prices and currencies

- **Euro stores** (ES, DE, FR, IT, NL, BE, IE) get the source price multiplied
  by `--price-factor`.
- **SE, PL and UK** get the source price multiplied by `--price-factor`, then
  converted at the `EUR_RATES` rate and rounded to 2 decimals. The run prints
  the rate it used.
- The rates don't update themselves. Set them to today's rates before you push,
  and consider rounding prices yourself for those stores.

## What is sent to Amazon

This is one request per product per marketplace. For example, for amazon.de:

```
PUT /listings/2021-08-01/items/{sellerId}/{sku}?marketplaceIds=A1PA6795UKMFR9
```

```json
{
  "productType": "ABIS_BOOK",
  "requirements": "LISTING_OFFER_ONLY",
  "attributes": {
    "merchant_suggested_asin": [{"value": "B0...", "marketplace_id": "A1PA6795UKMFR9"}],
    "condition_type":          [{"value": "new_new", "marketplace_id": "A1PA6795UKMFR9"}],
    "purchasable_offer": [{"marketplace_id": "A1PA6795UKMFR9", "currency": "EUR",
                            "our_price": [{"schedule": [{"value_with_tax": 8.5}]}]}],
    "fulfillment_availability": [{"fulfillment_channel_code": "DEFAULT", "quantity": 1}]
  }
}
```

`productType` is taken from the catalog item, and is usually `ABIS_BOOK`.
Orders are fulfilled by you (`DEFAULT`), not FBA.

All marketplaces use the EU endpoint, `sellingpartnerapi-eu.amazon.com`.
Authentication works through a Login with Amazon access token, sent in the
`x-amz-access-token` header. The script gets the access token from your
refresh token and renews it automatically. If Amazon throttles a request
(`429`) or has a server error (`5xx`), the script retries it with backoff. It
also waits 0.6 seconds after each catalog search.

## Files the script creates

| File | Contents |
|---|---|
| `amazon_posted.json` | Keyed by `MARKET:SKU`, for example `DE:142534`. Each entry holds `asin`, `status`, `submissionId` and `pushed_at`, plus `listing_status` and `issues` after `status`. Delete an entry to push that offer again. |

Only offers that Amazon `ACCEPTED` are recorded. Products that were skipped or
rejected in a marketplace are tried again there on the next run, which also
picks up books that Amazon has since added.

## Errors and troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | Credentials aren't set | Set all four `AMAZON_*` variables. |
| `Login failed (400) invalid_grant` | The refresh token is wrong or revoked | Re-authorize the app in Seller Central. |
| `Unknown market(s) XX` | Typo in `--markets` | Use the codes in the table at the top. |
| `403 Unauthorized` | The app lacks the Product Listing role, the Seller ID is wrong, or that marketplace isn't enabled | Check the app's roles, `AMAZON_SELLER_ID` and your enabled marketplaces. |
| `XX: SKIP - … not in the amazon.xx catalog` | That store has no page for the book | You can't list it there with this script. |
| `INVALID` with issues | Amazon rejected the offer | Read the issue message. Common causes are a marketplace that isn't enabled, a restricted category, or a price outside the allowed range. |
| `status` shows `not buyable yet` | Amazon is still processing the offer | Wait a few minutes and run `status` again. |

## Limitations

- **Offer-only listings.** The script only lists books a marketplace already has.
- **Fixed book prices.** New books have legally fixed prices in several
  countries, including Spain (Ley 10/2007, at most 5% discount), France (Loi
  Lang, at most 5%), Germany (Buchpreisbindung, no discount), Italy and the
  Netherlands. Check the rules before selling new books abroad, and don't use
  `--price-factor` to go above the cover price.
- **Shared stock.** The quantity belongs to your SKU, not to a single
  marketplace, so one copy listed in nine stores is still one copy. Check this
  in Seller Central after your first multi-store push.
- **Exchange rates are static.** Update `EUR_RATES` yourself.
- **No stock sync or deletion.** Manage those in Seller Central.
- **Category approval.** Some categories need Amazon's approval in a given
  marketplace. If so, Amazon reports it as an issue.
- **The script hasn't been run against the real SP-API yet.** Start with
  `--validate-only` and `--limit 1`.

## Testing

```powershell
python tests\test_amazon.py
```

[tests/test_amazon.py](../tests/test_amazon.py) starts a fake Amazon server on
`127.0.0.1:8765`. It mocks the Login with Amazon token endpoint, catalog search
and listings for all ten marketplaces, each with its own catalog. The fake
server returns a `429` once to test the retry, and it validates every offer
body, including the marketplace ID and currency. The test checks the following:

- missing or bad credentials are reported
- unknown market codes are rejected
- validate-only lists nothing
- ES is the default marketplace
- `--markets all` means the nine EU stores and excludes UK
- a book is skipped only in the stores that don't have it
- SEK, PLN and GBP prices are converted
- each offer carries its own marketplace ID
- state is recorded per marketplace
- nothing is pushed twice on a re-run
- `status` checks every offer, and its `--markets` filter works

Your real `amazon_posted.json` is never touched, because the test runs in a
temporary folder.
