# cdiscount_push.py reference

This script pushes books to **Cdiscount**, France's largest French-owned
marketplace. It uses the official **Octopia Seller API**
(<https://developer.octopia-io.net>, gateway `https://api.octopia-io.net/seller/v2`).
Octopia is the company that runs Cdiscount's marketplace, and the same API
serves its other sales channels. Cdiscount is sales channel `CDISFR`.
Everything below describes how the script behaves today.

Cdiscount works like bol.com and Kaufland: you add an **offer** (price,
stock, condition, delivery) to a product that already exists in Cdiscount's
catalog, identified by its **EAN**. **New and used books** can be sold.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to Octopia](#what-is-sent-to-octopia)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Login:** the script exchanges your Client ID and Client Secret for a
   token at `auth.octopia-io.net` (client credentials). The token lasts
   2 hours and is reused for the whole run, because Octopia allows at most
   500 token requests per hour. Every call also carries your `SellerId`.
2. **Catalog check:** the script looks up the ISBNs with
   `GET /products?gtin=…`, 10 per call. Books Cdiscount doesn't know are
   skipped.
3. **Offer package:** Octopia takes offers in batches called *offer packages*.
   1. The script creates one `Upsert` package for the `CDISFR` channel
      (`POST /offer-packages`).
   2. It uploads the offers into it, 100 per call
      (`POST /offer-packages/{id}/offer-requests`).
   3. It submits the package (`PATCH` with `state: Ready`).
   4. It waits until Octopia has processed it (`Integrated` or `Rejected`).

   **Upsert** creates new offers and replaces existing offers with the same
   reference, so re-sending is always safe and never creates duplicates.
4. **Results:** the script reads the result of every offer
   (`/offer-requests-results`, all pages). Integrated offers are saved in
   `cdiscount_posted.json`. Rejected or duplicated ones are printed with
   Octopia's result code and message.
5. **Sync:**
   - `--resend` re-sends books whose price or stock changed, in a new Upsert
     package.
   - `--zero-sold-out` sets stock to 0 for books that are now sold out.
6. **Status:** `status` reads your offers (`GET /offers` with the sales
   channel's feedback). It shows whether each one is active and online on
   Cdiscount, and whether it's the best offer.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Seller account.** You need an activated Cdiscount marketplace seller
account. Sellers based in other EU countries are accepted.

**2. API credentials.** At <https://developer.octopia-io.net>, go to **API
Credentials Management** and choose *Create my clientId and clientSecret*.
You get a **Client ID**, a **Client Secret** and confirmation of your
**Seller ID**. Complete the step where you authenticate as the seller, so the
Client ID is linked to your account.

**3. Credentials.** Add them to Bitwarden as `OCTOPIA_CLIENT_ID`,
`OCTOPIA_CLIENT_SECRET` and `OCTOPIA_SELLER_ID` (see
[push_all.md](push_all.md#where-credentials-are-stored)), or for a quick local
test:

```powershell
$env:OCTOPIA_CLIENT_ID = "..."
$env:OCTOPIA_CLIENT_SECRET = "..."
$env:OCTOPIA_SELLER_ID = "..."
```

**4. Delivery modes.** Set `DELIVERY_MODES` at the top of the script to what
you actually offer, with your prices. For example:
- `THD`: tracked home delivery
- `NTHD`: untracked home delivery (Cdiscount only)
- `SHD`: signed home delivery
- `PPMR`: Mondial Relay pickup point (Cdiscount only)

The full list is on <https://developer.octopia-io.net/octopia-delivery-modes/>.

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `SALES_CHANNEL_ID` | `CDISFR` (Cdiscount France). |
| `CONDITION_MAP` | Your condition mapped to Octopia's condition (see below). |
| `VAT_RATE` | VAT on books in France, in percent (`5.5`). |
| `DELIVERY_MODES` | Delivery modes offered with every book, with cost and cost per additional copy (EUR). |
| `PREPARATION_DAYS` | Days before you hand the parcel to the carrier (`2`). |

| Source condition | Octopia `condition` |
|---|---|
| `NewCondition` (or missing) | `New` |
| `UsedCondition` | `UsedVeryGoodState` |
| `RefurbishedCondition` | `UsedLikeNew` |
| `DamagedCondition` | `UsedAverageState` |

Octopia also accepts `Refurbished…` conditions, which aren't used for books.

## Commands

| Command | What it does |
|---|---|
| `check` | Tests the credentials and seller ID, and shows the shop name and state. |
| `lookup ISBN` | Shows whether Cdiscount's catalog knows a book. |
| `push` | Sends offers in an Upsert package and waits for the results (see below). |
| `status` | Shows each offer: active or inactive, online or not, price, stock, and whether it's the best offer. |
| `package ID` | Shows an earlier package's state and the result of every offer in it. Useful after a slow integration. |

### `push`

```powershell
python cdiscount_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Stock per offer. A product's own `quantity` takes precedence. Stock is shared between Octopia channels for the same product and condition. |
| `--price-factor` | `1.0` | Multiplies every source price, rounded half-up to cents. See the [fixed book price](#limitations). |
| `--preparation-days` | `2` | Days before shipping. |
| `--resend` | off | Re-sends books that were already pushed, when price or stock changed. |
| `--zero-sold-out` | off | Sets stock to 0 for pushed books that are now `sold_out`. |
| `--no-match-check` | off | Skips the catalog lookup. Unknown books are then rejected by Octopia instead. |
| `--dry-run` | off | Writes `cdiscount_offers.json` and sends nothing. Without credentials, the catalog check is skipped too. |
| `--limit N` | `0` (all) | Sends at most N new offers. |
| `--yes` | off | Skips the confirmation prompt. |
| `--wait` / `--poll` | `900` / `15` | Maximum seconds to wait for the package, and seconds between checks. |

Use this order for your first run:

```powershell
python cdiscount_push.py check                    # credentials, shop activated?
python cdiscount_push.py lookup 9788409591923     # does Cdiscount know your books?
python cdiscount_push.py push --dry-run           # look at cdiscount_offers.json
python cdiscount_push.py push --limit 1           # one real offer
python cdiscount_push.py status                   # active and online?
python cdiscount_push.py push                     # the rest
python cdiscount_push.py push --resend --zero-sold-out   # later: sync price/stock
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Used for |
|---|---|
| `id` or `sku` | `sellerExternalReference`, your unique offer reference. **Required.** |
| `isbn` or `ean` | `product.gtin`. **Required.** ISBN-10/13 or EAN; hyphens are removed. |
| `price_eur` or `price` | Price in EUR including VAT, rounded half-up after `--price-factor`. |
| `condition` | Looked up in `CONDITION_MAP`. |
| `quantity` | Optional per-product stock. |
| `sold_out` | `true` means the product is skipped, or set to stock 0 with `--zero-sold-out`. |
| `title` | Log messages only. Cdiscount shows its own title, cover and description. |

## What is sent to Octopia

Every offer request in the Upsert package looks like this:

```json
{
  "sellerExternalReference": "142534",
  "product": {"gtin": "9788409591923"},
  "condition": "New",
  "price": {"price": 8.5, "taxes": [{"code": "VAT", "value": 5.5}]},
  "deliveryModes": [{"code": "THD", "cost": 4.99, "additionalCost": 1.0}],
  "preparationTime": 2,
  "quantity": 1
}
```

The last batch the script built is saved in `cdiscount_offers.json`.

## Files the script creates

| File | Contents |
|---|---|
| `cdiscount_posted.json` | Reference mapped to `ean`, `price`, `quantity`, `condition`, `package_id` and `pushed_at`. Delete an entry to push that book again (Upsert replaces the offer, so nothing is duplicated). |
| `cdiscount_offers.json` | The last set of offer requests that was built or sent. |

## Errors and troubleshooting

Octopia API errors have a `title`, a `detail` and field-level `errors`. Offer
results have an `integrationStatus` (`Integrated`, `Rejected`, `Duplicated`)
with a result code and message, for example
`! 142534: Rejected - 8301 Product not found for this GTIN`.

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | Credentials not set | Set `OCTOPIA_CLIENT_ID`, `OCTOPIA_CLIENT_SECRET` and `OCTOPIA_SELLER_ID`. |
| `Octopia login failed (401)` | Wrong Client ID or Secret | Create new credentials at developer.octopia-io.net. |
| `403 Forbidden … OCTOPIA_SELLER_ID` | Wrong Seller ID, or the Client ID isn't linked to it | Finish the seller authentication in API Credentials Management. |
| `EAN … is not in the Cdiscount catalog` | Cdiscount doesn't list that book | You can't add an offer for it. |
| `Package … REJECTED` | Octopia stopped the whole package | Run `push` again. Upsert is safe to repeat. |
| `Package … is still IntegrationPending` | Large or slow package | Run `package <id>` later, or simply run `push` again. |
| `status` shows `INACTIVE` / `not online` | Stock 0, account not activated, price… | Check the offer in the Cdiscount seller back office. |
| `(API returned 429, retrying …)` | Rate limit | Retried automatically, waiting as long as Octopia says. |

## Limitations

- **Offers only.** The script can't create new products in Cdiscount's
  catalog, which would need Octopia's product integration API. Books
  Cdiscount doesn't know are skipped.
- **French fixed book price.** New books have a price fixed by law in France
  (loi Lang: at most 5% below the publisher's price, including for sellers
  from other EU countries). Don't use `--price-factor` to go below it, and
  keep Cdiscount's promotions within it.
- **French VAT.** Prices on Cdiscount include French VAT, which is 5.5% on
  books (`VAT_RATE`).
- **One set of delivery modes for every book** (`DELIVERY_MODES`).
- **Not verified:**
  - Octopia's documented result codes (the examples show `8000` "Offer
    created"). The script prints whatever code and message Octopia returns.
- **The script hasn't been run against the real Octopia API yet.** Start with
  `check`, `lookup`, `--dry-run` and `--limit 1`.

## Testing

```powershell
python tests\test_cdiscount.py
```

[tests/test_cdiscount.py](../tests/test_cdiscount.py) starts a fake Octopia
Seller API on `127.0.0.1:8765`.

The fake serves the client-credentials token, and checks the bearer token
and the `SellerId` and `SalesChannelId` headers. It implements `/sellers`,
the catalog lookup (up to 10 GTINs) and the full package flow:
- create, upload (at most 100 per call), submit
- integration on a later poll
- per-offer results, paged through the `Link` header

It applies the documented offer rules:
- GTIN known, condition and delivery mode values valid
- price with VAT, preparation time and quantity present
- a reference sent twice in one package is a duplicate

It also returns a `429`, rejects a package, and lets one package integrate
slowly.

The test checks the following:
- credential and seller ID errors are reported
- `check` and `lookup`
- the dry run, with and without credentials
- skips and the catalog check
- the package flow, with the token reused for the whole run
- re-runs don't create duplicates
- `--resend` updates offers (half-up rounding, no package when nothing changed)
- `--zero-sold-out`
- `status`
- rejected offers are shown, and results are read across several pages
- a rejected package is reported
- a slow package can be checked later with `package <id>`

Your real `cdiscount_posted.json` is never touched, because the test runs in
a temporary folder.
