# bol_push.py reference

This script pushes books to **bol.com**, the largest online marketplace in the
Netherlands and Belgium. It uses the official **bol.com Retailer API**, with
the new **Offers API v11**. Everything below describes how the script behaves
today.

bol.com retires the old v10 offer calls on **1 February 2027**, so this
script uses v11 from the start.

bol.com works like Kaufland and the Mirakl marketplaces: you add an **offer**
(price, stock, condition, delivery) to a product that already exists in
bol's catalog, identified by its **EAN**. bol started as a bookshop, so its
book catalog is large. **New and used books** can both be sold.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to bol.com](#what-is-sent-to-bolcom)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Login:** the script exchanges your Client ID and Client Secret for an
   access token at `login.bol.com` (client credentials). The token lasts
   about 5 minutes and is reused for the whole run. A fresh one is fetched
   only when it expires, because bol blocks IPs that request a token for
   every call.
2. **Catalog check:** for each book, the script calls
   `GET /retailer/products/{ean}/product-ids`. Books bol doesn't know are
   skipped. An ISBN-13 is an EAN, and ISBN-10s are converted automatically.
3. **Create:** the script sends one `POST /retailer/offers` (v11) per book. It
   sends your product ID as `reference`, which bol also shows on orders. The
   offer is for sale in the countries in `COUNTRIES` (NL and BE). The answer
   comes straight back with the new `offerId`.
   - **Already on bol:** if an offer for the same EAN and condition already
     exists (`409`) with your reference, the script links it instead of
     failing.
4. **Record:** successful offers are saved in `bol_posted.json`.
5. **Sync:**
   - `--resend` updates price and stock with one `PATCH`, only when they changed.
   - `--zero-sold-out` sets stock to 0 for books that are now sold out.
6. **Status:** `status` reads each offer and shows whether it's for sale in
   NL and BE. For offers that aren't, it shows bol's reasons, for example
   "Out of stock" or "Offline due to incomplete economic operator".

**GPSR:** EU product safety rules require an *economic operator* on each
offer. The script uses the first economic operator in your account, unless
you set `ECONOMIC_OPERATOR_ID`. Without one, bol takes offers offline.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Seller account.** You need a bol.com seller account (partnerplatform.bol.com).
Selling as a seller based outside the Netherlands and Belgium must be allowed
for your account.

**2. API credentials.** In the Seller Dashboard, go to **Instellingen →
Diensten → API Instellingen**:
1. Fill in the **technical contact**. Without it, bol doesn't allow API access.
2. Under **API Credentials for the Retailer API**, create credentials. You get
   a **Client ID** and a **Client Secret**.

**3. Seller Dashboard.**
- Set up your **delivery promise** (*levertijd*). The script uses it by default.
- Add an **economic operator** (GPSR) if you don't have one. `check` lists them.

**4. Credentials.** Add them to Bitwarden as `BOL_CLIENT_ID` and
`BOL_CLIENT_SECRET` (see [push_all.md](push_all.md#where-credentials-are-stored)),
or for a quick local test:

```powershell
$env:BOL_CLIENT_ID = "..."
$env:BOL_CLIENT_SECRET = "..."
```

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `COUNTRIES` | Where the offers are for sale: `["NL", "BE"]`. `--countries` overrides it. |
| `CONDITION_MAP` | Your condition mapped to bol's condition (see below). |
| `DELIVERY_SCHEDULE`, `DELIVERY_PROFILE_ID` | `MY_DELIVERY_PROMISE` uses the delivery promise from your Seller Dashboard. `--delivery-days` states it per offer instead. |
| `ECONOMIC_OPERATOR_ID` | GPSR economic operator. Empty means the first one in your account. |

| Source condition | bol `condition` |
|---|---|
| `NewCondition` (or missing) | `{"category": "NEW"}` |
| `UsedCondition` | `{"category": "SECONDHAND", "state": "GOOD"}` |
| `RefurbishedCondition` | `{"category": "SECONDHAND", "state": "AS_NEW"}` |
| `DamagedCondition` | `{"category": "SECONDHAND", "state": "MODERATE"}` |

bol's used states are `AS_NEW`, `GOOD`, `REASONABLE` and `MODERATE`.

## Commands

| Command | What it does |
|---|---|
| `check` | Tests the credentials (gets a token) and lists your economic operators. |
| `lookup ISBN` | Shows whether bol's catalog knows a book, with its bol product ID. |
| `push` | Creates or updates offers (see below). |
| `status` | Shows each offer: for sale in which countries, price, stock (and available stock after open orders), and bol's reasons when it isn't for sale. |

### `push`

```powershell
python bol_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--countries` | `NL,BE` | `NL`, `BE` or `NL,BE`. |
| `--quantity` | `1` | Stock per offer. A product's own `quantity` takes precedence. |
| `--price-factor` | `1.0` | Multiplies every source price, rounded half-up to cents. bol accepts 1 to 9999 EUR. |
| `--delivery-days MIN-MAX` | Seller Dashboard promise | States the delivery time per offer (e.g. `2-4`), as `BOL_DELIVERY_PROMISE`. |
| `--resend` | off | Updates price and stock of books that were already pushed, when they changed. |
| `--zero-sold-out` | off | Sets stock to 0 for pushed books that are now `sold_out`. |
| `--no-match-check` | off | Skips the catalog lookup. Unknown books are then rejected by bol instead. |
| `--dry-run` | off | Writes `bol_offers.json` and sends nothing. Without credentials, the lookups are skipped too. |
| `--limit N` | `0` (all) | Creates at most N new offers. |
| `--yes` | off | Skips the confirmation prompt. |

Use this order for your first run:

```powershell
python bol_push.py check                     # keys work, economic operator?
python bol_push.py lookup 9788409591923      # does bol know your books?
python bol_push.py push --dry-run            # look at bol_offers.json
python bol_push.py push --limit 1            # one real offer
python bol_push.py status                    # for sale? if not, bol's reasons
python bol_push.py push                      # the rest
python bol_push.py push --resend --zero-sold-out   # later: sync price/stock
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Used for |
|---|---|
| `id` or `sku` | `reference` (at most 100 characters). **Required.** |
| `isbn` or `ean` | `ean`. **Required.** ISBN-10/13 or EAN; hyphens are removed. |
| `price_eur` or `price` | Unit price in EUR, including VAT, rounded half-up after `--price-factor`. |
| `condition` | Looked up in `CONDITION_MAP`. |
| `quantity` | Optional per-product stock. |
| `sold_out` | `true` means the product is skipped, or set to stock 0 with `--zero-sold-out`. |
| `title` | Log messages only. bol shows its own title, cover and description. |

## What is sent to bol.com

`POST /retailer/offers` (media type `application/vnd.retailer.v11+json`) gets this:

```json
{
  "ean": "9788409591923",
  "reference": "142534",
  "onHoldByRetailer": false,
  "condition": {"category": "NEW"},
  "pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": 8.5}]},
  "countryAvailabilities": [{"countryCode": "NL"}, {"countryCode": "BE"}],
  "fulfilment": {"method": "FBR", "schedule": "MY_DELIVERY_PROMISE"},
  "stock": {"amount": 1, "managedByRetailer": true},
  "economicOperatorId": "0c6573a2-..."
}
```

`managedByRetailer: true` means bol only subtracts orders placed after your
last stock update, so the stock you send is the stock you have. Updates are
sent as `PATCH /retailer/offers/{offerId}` with only the changed `pricing`
and/or `stock`.

## Files the script creates

| File | Contents |
|---|---|
| `bol_posted.json` | SKU mapped to `offer_id`, `ean`, `price`, `stock`, `condition` and times. Delete an entry to push that book again (an existing offer is linked, not duplicated). |
| `bol_offers.json` | The last set of offers that was built. |

## Errors and troubleshooting

bol's errors have a `title`, a `detail` and field-level `violations`. The
script prints them, for example
`! 142534: 400 Error validating request (pricing.bundlePrices: ...)`.

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | Credentials not set | Set `BOL_CLIENT_ID` and `BOL_CLIENT_SECRET`. |
| `bol.com login failed (401)` | Wrong Client ID or Secret | Create new credentials in the Seller Dashboard. |
| `403 Forbidden` | Credentials without access to offers | Check the API credentials and that your account may sell. |
| `EAN … is not in the bol catalog` | bol doesn't list that book | You can't add an offer for it. |
| `price … outside bol's 1-9999 EUR` | Price too low or too high | Correct the price or `--price-factor`. |
| `status` shows `not for sale` with a reason | Out of stock, missing economic operator, price, on hold… | Follow bol's reason. Stock 0 is expected for sold-out books. |
| `(API returned 429, retrying …)` | Rate limit (50 offer calls per second) | Retried automatically, waiting as long as bol says. |

## Limitations

- **Offers only.** The script can't create new products in bol's catalog.
  Books bol doesn't know are skipped.
- **VAT and fixed book prices.** bol prices include VAT (books: 9% in the
  Netherlands, 6% in Belgium). Dutch-language books have a fixed price in
  the Netherlands and, for a limited time, in Flanders. Check your prices
  before using `--price-factor`.
- **One price per offer.** bol's volume discounts (`bundlePrices` for 2+
  copies) aren't used.
- **Not verified:**
  - Offers API v11 is new, and bol still marks it as beta.
  - The exact delivery settings bol wants for a seller shipping from Spain.
    The default is your Seller Dashboard delivery promise
    (`MY_DELIVERY_PROMISE`), or `--delivery-days`.
- **The script hasn't been run against the real bol.com API yet.** Start with
  `check`, `lookup`, `--dry-run` and `--limit 1`.

## Testing

```powershell
python tests\test_bol.py
```

[tests/test_bol.py](../tests/test_bol.py) starts a fake bol.com API on
`127.0.0.1:8765`.

The fake checks the Basic-auth token login, the bearer token, and bol's
versioned media types on every call (v11 for offers, v10 for the catalog
lookup, the economic-operator type). It applies the v11 offer rules:
- price range and decimals
- conditions and used states
- NL/BE only
- fulfilment and delivery promise
- `409` on duplicates

It returns a `429` with `Retry-After` once, and expires a token mid-run.

The test checks the following:
- credentials errors are reported
- `check` and `lookup`
- the dry run, with and without credentials
- skips and the catalog check
- `--delivery-days` and `--countries`
- offers are created, with a single token reused for the whole run
- re-runs don't create duplicates
- `--resend` (half-up rounding, no call when nothing changed)
- `--zero-sold-out`
- `status`, with bol's reasons
- the token is refreshed after it expires
- bol's validation errors are shown, and rejected offers aren't recorded
- an existing offer is linked on `409`
- a missing economic operator is reported

Your real `bol_posted.json` is never touched, because the test runs in a
temporary folder.
