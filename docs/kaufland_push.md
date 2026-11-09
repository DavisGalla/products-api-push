# kaufland_push.py reference

This script pushes books to **Kaufland.es** (Kaufland Global Marketplace)
through the official **Kaufland Seller API v2**
(<https://sellerapi.kaufland.com>). Everything below describes how the script
behaves today.

Kaufland opened its Spanish storefront to sellers in June 2026. One Kaufland
seller account covers every storefront: es, de, at, cz, sk, pl, fr, it and nl.
The script defaults to Spain (`es`). `--storefront` selects another one, and
`--storefronts` serves several in one run: a list (`es,de,fr`), `eur` (the 7
euro storefronts) or `all` (all 9). Each storefront gets its own offers. Prices
are converted from EUR to CZK for kaufland.cz and to PLN for kaufland.pl.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to Kaufland](#what-is-sent-to-kaufland)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

Kaufland sells existing catalog products. As a seller, you add a **unit** (an
offer with price, stock, condition and handling time) to a product, which is
identified by its **EAN**. An ISBN-13 is an EAN, and ISBN-10s are converted
automatically.

1. **Catalog check:** for each book, the script calls
   `GET /products/ean/{ean}?storefront=es`. Books that aren't in Kaufland's
   catalog are skipped.
2. **Create:** the script sends one `POST /units/` per book. Your product ID
   is sent as `id_offer`, and Kaufland treats it as your unique offer ID.
   Sending the same `id_offer` again **updates** that unit instead of creating
   a duplicate, which is how `--resend` and `--zero-sold-out` work.
3. **Record:** successful units are saved in `kaufland_posted.json` as
   `ES:<id>`, so another storefront (e.g. `DE:<id>`) is tracked separately.
4. **Live check:** `status` lists your units and uses `is_live`, which Kaufland
   calls "the single reliable signal" that an offer is live. For units that
   aren't live, it asks Kaufland why (`POST /units/status`).

Every request is signed as Kaufland requires:
- the `Shop-Client-Key`, `Shop-Timestamp` and `Shop-Signature` headers
- an HMAC-SHA256 over `METHOD\nURL\nBODY\nTIMESTAMP` with your Secret Key

The implementation is checked against the test vector in Kaufland's
documentation. Kaufland shows its own title, cover and description, so your
scraped text and photos aren't sent.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Seller account.** Register at the Kaufland Global Marketplace seller
portal, and activate the Spanish storefront (and every other storefront
you want to sell on, each with its shipping group and warehouse).

**2. Shipping and warehouse.** In the Seller Portal, set up a **shipping group**
and a **default warehouse** for Spain. Without a default warehouse, Kaufland
rejects every unit.

**3. API keys.** Seller Portal → **Settings → API**
(<https://sellerportal.kaufland.de/settings/api>). Generate a **Client Key**
(32 characters) and a **Secret Key** (64 characters). The Secret Key is shown
only once, and can't be recovered later.

**4. Credentials.** Add them to Bitwarden as `KAUFLAND_CLIENT_KEY` and
`KAUFLAND_SECRET_KEY` (see [push_all.md](push_all.md#where-credentials-are-stored)),
or for a quick local test:

```powershell
$env:KAUFLAND_CLIENT_KEY = "..."
$env:KAUFLAND_SECRET_KEY = "..."
```

Your PC's clock must be correct to within 5 minutes, because Kaufland rejects
signatures with an older or newer timestamp.

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `STOREFRONT` | `es` (kaufland.es). `--storefront` / `--storefronts` override it. |
| `EUR_RATE` | CZK 25.0, PLN 4.25: EUR → storefront currency for kaufland.cz and kaufland.pl. **Update regularly.** |
| `CONDITION_MAP` | Your condition mapped to the Kaufland condition (see below). |
| `DEFAULT_HANDLING_DAYS` | Working days until you ship (`3`). |
| `SHIPPING_GROUP_ID`, `WAREHOUSE_ID` | Optional. Leave them empty to use your defaults from the Seller Portal. |

| Source condition | Kaufland `condition` |
|---|---|
| `NewCondition` (or missing) | `NEW` |
| `UsedCondition` | `USED___VERY_GOOD` |
| `RefurbishedCondition` | `USED___GOOD` |
| `DamagedCondition` | `USED___ACCEPTABLE` |

Kaufland also accepts `USED___AS_NEW` and the `REFURBISHED___…` values. Its
condition names use three underscores.

## Commands

| Command | What it does |
|---|---|
| `check` | Tests the keys (a signed request), and lists the storefronts. It warns if `es` isn't among them. |
| `push` | Creates or updates units (see below). |
| `status` | Lists your units on the storefront: LIVE or not, price, stock, Kaufland status, and the reasons a unit isn't live. |

### `push`

```powershell
python kaufland_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--storefront` | `es` | Kaufland country storefront. |
| `--storefronts` | – | Several storefronts in one run: `es,de,fr`, or `eur` / `all`. Works for `check`, `push` and `status`. The dry-run preview `kaufland_units.json` then holds the offers of all of them. |
| `--quantity` | `1` | Stock per offer (at most 99999). A product's own `quantity` takes precedence. |
| `--handling-days` | `3` | Working days until you ship. |
| `--price-factor` | `1.0` | Multiplies every source price. See the [fixed book price](#limitations). |
| `--note` | empty | Note shown to buyers, at most 250 characters. |
| `--shipping-group`, `--warehouse` | settings | Send a specific `id_shipping_group` / `id_warehouse`. |
| `--resend` | off | Also re-sends books that were already pushed, which updates their price and stock. |
| `--zero-sold-out` | off | Sets stock to 0 for pushed books that are now `sold_out` in your JSON. |
| `--no-match-check` | off | Skips the catalog lookup. Unknown books are then rejected by Kaufland instead. |
| `--dry-run` | off | Writes `kaufland_units.json` and sends nothing. Without credentials, the catalog check is skipped too. |
| `--limit N` | `0` (all) | Sends at most N new offers. |
| `--yes` | off | Skips the confirmation prompt. |

Use this order for your first run:

```powershell
python kaufland_push.py check                  # keys work, es listed?
python kaufland_push.py push --dry-run         # look at kaufland_units.json
python kaufland_push.py push --limit 1         # one real offer
python kaufland_push.py status                 # LIVE? if not, the reasons are shown
python kaufland_push.py push                   # the rest
python kaufland_push.py push --resend --zero-sold-out   # later: sync price/stock
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Used for |
|---|---|
| `id` or `sku` | `id_offer`, your unique offer ID. **Required.** |
| `isbn` or `ean` | `ean`. **Required.** ISBN-10/13 or EAN; hyphens are removed. |
| `price_eur` or `price` | `listing_price`, in **cents** (8.50 → `850`), rounded half-up after `--price-factor`. |
| `condition` | Looked up in `CONDITION_MAP`. |
| `quantity` | Optional per-product stock. |
| `sold_out` | `true` means the product is skipped, or set to stock 0 with `--zero-sold-out`. |
| `title` | Log messages only. |

## What is sent to Kaufland

```json
{
  "ean": "9788409591923",
  "id_offer": "142534",
  "condition": "NEW",
  "listing_price": 850,
  "amount": 1,
  "handling_time": 3,
  "storefront": "es",
  "note": ""
}
```

The script sends this to `POST https://sellerapi.kaufland.com/v2/units/`. The
last batch it built is saved in `kaufland_units.json`.

## Files the script creates

| File | Contents |
|---|---|
| `kaufland_posted.json` | `STOREFRONT:id` mapped to `ean`, `id_unit`, `listing_price`, `amount` and `pushed_at`. Delete an entry to push that book again. |
| `kaufland_units.json` | The last set of units that was built or sent. |

## Errors and troubleshooting

Kaufland's errors have a `type`, a `message`, and sometimes field-level
`errors`. The script prints all three, for example
`! 142534: 400 Validation Failed (listing_price: must be ...)`.

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | Keys not set | Set `KAUFLAND_CLIENT_KEY` and `KAUFLAND_SECRET_KEY`. |
| `401 Unauthorized: Signature mismatch` / `invalid-credentials` | Wrong key, or the secret was copied incompletely | Generate new keys in the Seller Portal. |
| `401 … Timestamp` | PC clock is off by more than 5 minutes | Sync the Windows clock (Settings → Time → Sync now). |
| `403 Forbidden` | Seller account inactive | Check the account in the Seller Portal. |
| `storefront-not-configured` | Spain isn't activated for your account | Activate the storefront in the Seller Portal. |
| `default-warehouse-not-found` | No default warehouse | Create one in the Seller Portal, or pass `--warehouse`. |
| `EAN … is not in the Kaufland catalog` | Kaufland doesn't list that book | You can't add an offer for it (see Limitations). |
| `status` shows `not live` with `stock_update_needed`, `product_data_incomplete` and similar | Kaufland's reason the unit isn't shown | Follow the reason. Stock 0 is expected for sold-out books. |
| `(API returned 429, retrying …)` | Rate limit (111 requests per second) | Retried automatically. |

## Limitations

- **The same copy on several storefronts:** each storefront gets its own
  offer. With one copy, it can sell twice until stock is synced.
- **Exchange rates** for CZK and PLN are fixed in `EUR_RATE`, not fetched live.

- **Offers only.** The script can't create new products in Kaufland's catalog,
  which would need the product data import.
- **Spanish fixed book price.** New books have a price fixed by law
  (Ley 10/2007, at most 5% discount). Don't use `--price-factor` to go above
  the cover price.
- **One request per book.** That's fine for hundreds of books. For very large
  catalogs, Kaufland's inventory CSV import would be faster.
- **Not verified:**
  - Whether the query string is part of the signed URL. The script signs the
    full URL, query included, and `check` will show it on the first real run.
  - The exact success response of `POST /units/`. If it doesn't contain
    `id_unit`, `status` still finds the unit by `id_offer`.
- **The script hasn't been run against the real Kaufland API yet.** Start with
  `check`, `--dry-run` and `--limit 1`. Kaufland also offers a test
  environment, the Seller API Playground
  (<https://sellerapi-playground.kaufland.com>).

## Testing

```powershell
python tests\test_kaufland.py
```

[tests/test_kaufland.py](../tests/test_kaufland.py) checks the signature
against the official test vector from Kaufland's documentation. It then starts
a fake Kaufland API on `127.0.0.1:8765`. The fake recomputes the HMAC
signature of every request, and rejects:
- missing headers, a wrong key or signature, or an old timestamp
- invalid conditions, prices that aren't in cents, amounts over 99999, and
  notes over 250 characters
- EANs that aren't in its catalog

It returns a 429 once, to test the retry. The test checks the following:
- credentials errors are reported
- `check` works, and an unknown storefront is flagged
- the dry run works with and without credentials
- skips and the catalog check are correct
- the request body: price in cents, condition, ISBN cleanup
- units are created and recorded per storefront
- nothing is sent twice on a re-run
- `--resend` updates units without duplicates, with half-up rounding
- `--zero-sold-out` works
- `status` shows reasons for units that aren't live
- server and validation errors are shown, and rejected units aren't recorded
- a second storefront is tracked separately
- `--storefronts`: several storefronts in one run, CZK / PLN conversion,
  nothing sent twice, unknown storefronts refused, `eur` in a dry run

Your real `kaufland_posted.json` is never touched, because the test runs in a
temporary folder.
