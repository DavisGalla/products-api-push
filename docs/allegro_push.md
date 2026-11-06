# allegro_push.py reference

This script pushes books to **Allegro** (allegro.pl), the largest
marketplace in Poland and Central Europe. It uses the official **Allegro
REST API** (<https://developer.allegro.pl>, gateway `https://api.allegro.pl`).
Everything below describes how the script behaves today.

Allegro has a **product catalog**. Each offer references the book's catalog
product by its **ISBN/EAN**, and Allegro supplies the title, images and
parameters. Books that aren't in the catalog are skipped. Allegro sells in
**Polish zloty (PLN)**, so your EUR prices are converted with `EUR_TO_PLN`
(default 4.25), or `--eur-rate`. **New and used books** can both be sold.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to Allegro](#what-is-sent-to-allegro)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Authorization (once):** `auth` runs Allegro's **device code** flow, so
   you don't need a website or a redirect URL.
   1. The script asks Allegro for a code (`POST /auth/oauth/device`).
   2. You open the printed link (`allegro.pl/skojarz-aplikacje`) and confirm
      the code with your seller account.
   3. The script waits and receives the tokens.

   The access token lasts 12 hours. The script refreshes it automatically.
   The refresh token can be used **only once**, so the new one is saved
   every time. The authorization lasts at most 3 months; after that, run
   `auth` again.
2. **Account settings:** every offer needs a shipping rates set, a return
   policy, an implied warranty (*rękojmia*) and a GPSR responsible producer
   from your account. The script uses the first of each, unless you set the
   IDs in the settings. If one is missing, nothing is sent.
3. **Catalog check:** `GET /sale/products?phrase=<EAN>&mode=GTIN`. Books
   Allegro doesn't know are skipped.
4. **Create:** the script sends one `POST /sale/product-offers` per book.
   - **Product:** `{id: EAN, idType: GTIN}`.
   - **Condition:** the `Stan` parameter (new or used).
   - **Price** in PLN, **stock**, delivery, and after-sales conditions.
   - **GPSR:** the responsible producer and safety information.
   - **Reference:** your product ID as `external.id`.

   Offers are published straight away (`publication.status: ACTIVE`). If
   Allegro answers `202 Accepted`, the script polls the operation until it's
   completed.
5. **Record:** successful offers are saved in `allegro_posted.json`.
6. **Sync:**
   - `--resend` updates price and stock (`PATCH /sale/product-offers/{id}`),
     only when they changed.
   - `--end-sold-out` ends sold-out offers with a publication command (`END`).
   - A book that comes back in stock is re-activated by `--resend` (`ACTIVATE`).

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Seller account.** You need an Allegro **business** account. Sellers
from other EU countries are accepted. Two-step login must be enabled.

**2. App.** At <https://apps.developer.allegro.pl>:
1. Register an application of the type **"the app will not have access to a
   browser" (device)**.
2. Accept the REST API terms.
3. Note the **Client ID** and **Client Secret**.

Allegro also has a **sandbox** for testing: create an app at
apps.developer.allegro.pl.allegrosandbox.pl and set `ALLEGRO_SANDBOX=1`.

**3. Allegro account settings** (in Sales Center / *Moje Allegro*):
- a **shipping rates set** (*cennik dostawy*)
- a **return policy** (*warunki zwrotów*)
- an **implied warranty** (*warunki reklamacji / rękojmia*)
- a **responsible producer** (GPSR), for example the publisher

`check` lists what it finds.

**4. Credentials.** Add them to Bitwarden as `ALLEGRO_CLIENT_ID` and
`ALLEGRO_CLIENT_SECRET` (see [push_all.md](push_all.md#where-credentials-are-stored)),
or for a quick local test:

```powershell
$env:ALLEGRO_CLIENT_ID = "..."
$env:ALLEGRO_CLIENT_SECRET = "..."
```

**5. Authorize the app.**

```powershell
python allegro_push.py auth
# open the printed link, log in with your Allegro seller account, confirm the code
```

The tokens are saved in `allegro_token.json`. That file is in `.gitignore`
and never goes to GitHub.

On a server, copy `refresh_token` from that file into Bitwarden as
`ALLEGRO_REFRESH_TOKEN`. The script uses it when there's no token file yet.
Because Allegro's refresh token is single-use, the token file must then be
kept on the server too, so that the latest token is always available.

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `EUR_TO_PLN` | Exchange rate for your EUR prices (`4.25`). `--eur-rate` overrides it. Check it regularly. |
| `HANDLING_TIME` | Time to ship, in ISO 8601 (`P2D` = 2 days). |
| `CONDITION_PARAMETER_ID`, `CONDITION_VALUES` | Allegro's condition parameter `Stan` (`11323`), with `11323_1` = new and `11323_2` = used. Conditions that aren't listed are skipped. |
| `SHIPPING_RATES_ID`, `RETURN_POLICY_ID`, `IMPLIED_WARRANTY_ID`, `RESPONSIBLE_PRODUCER_ID` | Account settings to use. Empty means the first one in your account. |
| `SAFETY_TEXT` | GPSR safety information sent with every book (Polish, plain text). |
| `INVOICE` | Invoice type (`VAT`). |

## Commands

| Command | What it does |
|---|---|
| `auth` | One-time authorization with a device code. Saves the tokens. |
| `check` | Shows your account, and the shipping rates, return policy, implied warranty and responsible producer that will be used. |
| `lookup ISBN` | Shows whether Allegro's catalog knows a book, with its product and category. |
| `push` | Creates, updates, ends or re-activates offers (see below). |
| `status` | Shows each offer: ACTIVE or ENDED, PLN price, stock, and units sold in the last 30 days. |

### `push`

```powershell
python allegro_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Stock per offer. A product's own `quantity` takes precedence. |
| `--price-factor` | `1.0` | Multiplies every source price. |
| `--eur-rate` | `EUR_TO_PLN` | EUR → PLN exchange rate. |
| `--handling-time` | `P2D` | Time to ship. |
| `--resend` | off | Updates price and stock of books that were already pushed, when they changed. |
| `--end-sold-out` | off | Ends pushed offers whose book is now `sold_out`. |
| `--no-match-check` | off | Skips the catalog lookup. Unknown books are then rejected by Allegro instead. |
| `--dry-run` | off | Writes `allegro_offers.json` and sends nothing. Without credentials, the lookups are skipped too. |
| `--limit N` | `0` (all) | Creates at most N new offers. |
| `--yes` | off | Skips the confirmation prompt. |

Use this order for your first run:

```powershell
python allegro_push.py auth                     # once
python allegro_push.py check                    # all account settings found?
python allegro_push.py lookup 9788409591923     # does Allegro know your books?
python allegro_push.py push --dry-run           # look at allegro_offers.json
python allegro_push.py push --limit 1           # one real offer
python allegro_push.py status                   # ACTIVE?
python allegro_push.py push                     # the rest
python allegro_push.py push --resend --end-sold-out   # later: sync price/stock
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Used for |
|---|---|
| `id` or `sku` | `external.id`, your reference. **Required.** |
| `isbn` or `ean` | The catalog product (`idType: GTIN`). **Required.** ISBN-10/13 or EAN. |
| `price_eur` or `price` | Price in EUR, converted to PLN (`× --price-factor × --eur-rate`) and rounded half-up to grosze: 8.50 EUR → 36.13 PLN. |
| `condition` | Looked up in `CONDITION_VALUES`. |
| `quantity` | Optional per-product stock. |
| `sold_out` | `true` means the product is skipped, or its offer ended with `--end-sold-out`. |
| `title` | Log messages only. Allegro shows the catalog product's title and images. |

## What is sent to Allegro

`POST /sale/product-offers` (media type `application/vnd.allegro.public.v1+json`) gets this:

```json
{
  "productSet": [{
    "product": {"id": "9788409591923", "idType": "GTIN"},
    "responsibleProducer": {"type": "ID", "id": "..."},
    "safetyInformation": {"type": "TEXT", "description": "Książka. ..."}
  }],
  "parameters": [{"id": "11323", "valuesIds": ["11323_1"]}],
  "sellingMode": {"format": "BUY_NOW", "price": {"amount": "36.13", "currency": "PLN"}},
  "stock": {"available": 1, "unit": "UNIT"},
  "delivery": {"handlingTime": "P2D", "shippingRates": {"id": "..."}},
  "afterSalesServices": {"returnPolicy": {"id": "..."}, "impliedWarranty": {"id": "..."}},
  "payments": {"invoice": "VAT"},
  "external": {"id": "142534"},
  "publication": {"status": "ACTIVE"}
}
```

The last batch the script built is saved in `allegro_offers.json`.

## Files the script creates

| File | Contents |
|---|---|
| `allegro_posted.json` | SKU mapped to `offer_id`, `ean`, `price` (PLN), `stock`, `status` and times. |
| `allegro_offers.json` | The last set of offers that was built. |
| `allegro_token.json` | Access and refresh tokens. **Secret**, not in git. |

## Errors and troubleshooting

Allegro errors have a code, a message and a `path` to the field. The script
prints them, for example
`! 142534: 422 Product with GTIN … was not found (productSet[0].product)`.

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | Client ID or Secret not set | Set `ALLEGRO_CLIENT_ID` and `ALLEGRO_CLIENT_SECRET`. |
| `No Allegro authorization yet` | The app was never authorized | Run `auth`. |
| `Authorize again` | The refresh token was used or expired (3 months) | Run `auth`. |
| `Missing in your Allegro account: …` | No shipping rates, return policy, implied warranty or responsible producer | Create it on Allegro (see `check`). |
| `EAN … is not in the Allegro catalog` | Allegro doesn't list that book | You can't add an offer for it this way. |
| `Parameter 'Stan' is required` | Condition value doesn't fit the category | Check the category's parameters, then fix `CONDITION_VALUES`. |
| `403 Forbidden` | The app lacks the sale/offers scopes | Check the app in apps.developer.allegro.pl. |
| `(API returned 429, retrying …)` | Rate limit | Retried automatically. |

## Limitations

- **Catalog books only.** Books that aren't in Allegro's catalog are skipped.
  Allegro allows proposing new products, but the script doesn't do that.
- **Prices in PLN, at a fixed rate.** `EUR_TO_PLN` isn't updated
  automatically. Prices include Polish VAT (books: 5%). Poland has no fixed
  book price law.
- **One shipping rates set, return policy and warranty** for all books.
- **Only the Polish site (allegro.pl).** Allegro can also show offers on
  allegro.cz, allegro.sk and allegro.hu (*additional marketplaces*), but the
  script doesn't use them.
- **Not verified:**
  - The condition parameter IDs (`11323`, `11323_1`, `11323_2`) for the books
    categories. They're settings, and Allegro's error names the parameter if
    they're wrong.
  - Whether sellers shipping from Spain need extra delivery settings.
- **The script hasn't been run against the real Allegro API yet.** Start with
  `auth`, `check`, `lookup`, `--dry-run` and `--limit 1`, or try the sandbox
  first with `ALLEGRO_SANDBOX=1`.

## Testing

```powershell
python tests\test_allegro.py
```

[tests/test_allegro.py](../tests/test_allegro.py) starts a fake Allegro API
and OAuth server on `127.0.0.1:8765`.

The fake implements:
- the device code flow (pending, then confirmed)
- single-use refresh tokens
- bearer token and media type checks on every call
- the account settings lists and the catalog search by GTIN
- offer creation, both `201` and `202` with operation polling
- `PATCH`, the END / ACTIVATE commands and the offer list

It applies the documented offer rules:
- a catalog product, GPSR safety information and a responsible producer
- the condition parameter
- a PLN price with 2 decimals and stock of at least 1
- a valid handling time and shipping rates
- the after-sales conditions

It also returns a `429` once.

The test checks the following:
- credential and authorization errors are reported
- the device flow, and that tokens aren't printed
- `check` and `lookup`
- the dry run, with and without credentials (PLN conversion)
- skips and the catalog check
- creation through both response paths
- re-runs don't create duplicates
- `--resend` (half-up rounding in PLN, no call when nothing changed)
- `--end-sold-out` and re-activation
- `status`
- Allegro's validation errors are shown
- token refresh with rotation, and an expired authorization
- `ALLEGRO_REFRESH_TOKEN`
- a missing GPSR producer stops the run before anything is sent

Your real `allegro_posted.json` and `allegro_token.json` are never touched,
because the test runs in a temporary folder.
