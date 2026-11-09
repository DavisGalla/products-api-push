# otto_push.py reference

This script pushes books to **OTTO Market** (otto.de), Germany's
second-largest online marketplace. It uses the official **OTTO Market API**
(<https://api.otto.market/docs>):

- **Products API v5** for the product data, price and active status
- **Availability API v1** for stock and delivery

Everything below describes how the script behaves today.

On OTTO you **can't add an offer to an existing product page**: every
seller creates its own product, a "product variation" with its EAN. Each
book needs:

- one OTTO category
- a brand from OTTO's brand list
- a description
- at least one image
- the price
- the GPSR (product safety) distributor address

OTTO sells **new goods only**; the API has no condition field.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [The product data](#the-product-data)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Login.** The script requests a token with your self-app's client ID and
   secret (`POST /v1/token`, client credentials, scopes `products availability`)
   and requests a new one when it expires.
2. **Brand.** OTTO needs a `brandId` from its brand list. If the book's
   publisher is a usable OTTO brand with exactly that name, that brand is
   used; otherwise it falls back to `BRAND_ID`. The brand list is loaded once
   and cached for 7 days (`otto_brands.json`).
3. **Upload.** New books are sent with `POST /v5/products` in batches of up
   to 500, the API limit. OTTO answers with an **update task**.
4. **Wait for the result.** The script polls the task until its state is
   `done` (`--wait`, default 10 minutes). It then reads the **failed**
   products with OTTO's error code, message and field path. Rejected books
   aren't recorded, so they're tried again on the next run.
5. **Stock and delivery.** For the accepted products, it sets:
   - the stock (`POST /v1/availability/quantities`)
   - the shipping profile and processing time
     (`POST /v1/availability/product-delivery-information`)

   Both are sent in batches of 200. OTTO answers `207` when only some SKUs
   are accepted; the script reports the others.
6. **Updates.** `--resend` sends only what changed:
   - new prices with `POST /v5/products/prices`, an update task like the upload
   - new stock with the quantities endpoint
7. **Sold out.** For books that are now sold out:
   - `--zero-sold-out` sets the stock to 0
   - `--offline-sold-out` deactivates the product (`POST /v5/products/active-status`)
8. **Status.** `status` reads `GET /v5/products/marketplace-status` for every
   pushed product: `ONLINE`, `PENDING`, `RESTRICTED`, `REJECTED` or `INACTIVE`,
   with OTTO's errors and the otto.de link.

## Setup

1. **Become an OTTO Market partner.** OTTO accepts German companies, and is
   gradually opening up to other EU countries: the Netherlands, then Poland,
   Austria, France and Spain, partly as pilots. Requirements include
   OSS VAT registration and customer service in German. Check the current
   conditions with OTTO.
2. **Create a self-app** in OTTO Partner Connect with the interfaces
   **products** and **availability**. You get a client ID and a client secret.
3. **Create a shipping profile** in Partner Connect (delivery type, working
   days, processing and transport time).
4. **Store the credentials in Bitwarden** (project `product-pushers`), or for
   a quick local test:
   ```powershell
   $env:OTTO_CLIENT_ID = "..."
   $env:OTTO_CLIENT_SECRET = "..."
   ```
   `OTTO_API_URL=https://sandbox.api.otto.market` uses OTTO's sandbox.
5. **Fill in the settings** at the top of `otto_push.py`:

   | Setting | Meaning |
   |---|---|
   | `CATEGORY` | The OTTO category for books. Find it with `categories --find Buch`. |
   | `BRAND_ID` | The brand used when the publisher isn't an OTTO brand. Find it with `brands --find …`. |
   | `MATCH_PUBLISHER_BRAND` | `True` = use the publisher's own OTTO brand when its name matches exactly |
   | `ATTRIBUTES` | Fixed attributes for every book, e.g. `{"Sprache": ["Spanisch"]}`. See `attributes <category>`. |
   | `FIELD_ATTRIBUTES` | Source field → OTTO attribute, e.g. `{"author": "Autor", "pages": "Seitenanzahl"}` |
   | `VAT` | `REDUCED` (7 %, books) |
   | `SHIPPING_PROFILE_ID` | `""` = your first shipping profile |
   | `PROCESSING_TIME` | Days to prepare an order (1–99), or `DEFAULT` for the profile's value |
   | `GPSR_DISTRIBUTOR` | Your company: name, full address in one line, country code, email or URL. **Mandatory.** |

6. **Run `check`.**

## Commands

| Command | What it does |
|---|---|
| `check` | Token, shipping profiles, whether `CATEGORY` exists, `BRAND_ID` and GPSR settings |
| `categories [--find TEXT]` | Lists OTTO categories |
| `attributes CATEGORY` | The category's attributes: type, relevance, allowed values, extra requirements |
| `brands --find TEXT [--refresh]` | Usable OTTO brands and their IDs |
| `push [json_file]` | Creates products, sets stock and delivery |
| `status` | Marketplace status of every pushed product, with OTTO's errors |

### `push`

| Option | Meaning |
|---|---|
| `--dry-run` | Write `otto_products.json` only. No credentials needed. |
| `--limit N` | At most N new products this run |
| `--resend` | Update price and stock of products already pushed (only what changed) |
| `--zero-sold-out` | Stock 0 for pushed books that are now sold out |
| `--offline-sold-out` | Deactivate pushed books that are now sold out |
| `--quantity N` | Stock when the product has no `stock`/`quantity` field (default 1) |
| `--price-factor X` | Multiply source prices |
| `--category NAME` | Overrides `CATEGORY` |
| `--wait S` / `--poll S` | How long to wait for OTTO's processing, and how often to check |
| `--yes` | No confirmation (used by `push_all`) |

## Input JSON format

| Field | Use |
|---|---|
| `id` or `sku` | `sku` and `productReference` (max 50 characters, no leading/trailing spaces) |
| `isbn` or `ean` | `ean`. ISBN-10 is converted, and the check digit is verified. EANs starting with 2 aren't allowed. |
| `title` | `productLine` (max 70 characters) |
| `description` | HTML description (escaped, line breaks kept, max 2000 characters) |
| `price_eur` or `price` | `standardPrice`, EUR including VAT |
| `publisher` or `brand` | Brand match, and a bullet point |
| `author`, `binding`, `pages`, `year` | Bullet points (and attributes via `FIELD_ATTRIBUTES`) |
| `images` / `image` | `mediaAssets` of type `IMAGE`. At least one http(s) URL is required. |
| `stock` or `quantity` | Stock (default `--quantity`) |
| `condition` | Only `NewCondition` |
| `sold_out` | Skipped, or `--zero-sold-out` / `--offline-sold-out` |

## The product data

Example of one product in `otto_products.json`:

```json
{
  "productReference": "T-NEW",
  "sku": "T-NEW",
  "ean": "9788409591923",
  "productDescription": {
    "category": "<CATEGORY>",
    "brandId": "<brand ID>",
    "productLine": "Dragona. Libro para bebés con rimas",
    "description": "<p>…</p>",
    "bulletPoints": ["Verlag: Cuentos con Valores"],
    "attributes": [{"name": "Sprache", "values": ["Spanisch"]}]
  },
  "mediaAssets": [{"type": "IMAGE", "location": "https://…/cover.jpg"}],
  "pricing": {"standardPrice": {"amount": 8.5, "currency": "EUR"}, "vat": "REDUCED"},
  "compliance": {"productSafety": {"addresses": [{"name": "…", "address": "…", "regionCode": "ES",
                                                  "email": "…", "roles": ["DISTRIBUTOR"]}]}}
}
```

**Images** must be JPG or PNG (RGB), at least **500 × 1000 px** and at most
4500 px wide. Small covers are rejected by OTTO, and `status` shows the
reason.

## Files the script creates

| File | Content |
|---|---|
| `otto_products.json` | The last product data built (also on `--dry-run`) |
| `otto_posted.json` | Per SKU: EAN, price, stock, active, brand, times |
| `otto_brands.json` | Cached list of usable OTTO brands |

All three are in `.gitignore`.

## Errors and troubleshooting

| Message | Cause | What to do |
|---|---|---|
| `OTTO login failed (401)` | Wrong client ID/secret | Check the Bitwarden secrets |
| `403 Forbidden … interface in your self-app?` | The self-app lacks `products` or `availability` | Add the interface in Partner Connect |
| `CATEGORY is not set` | Setting missing | `categories --find Buch` |
| `GPSR_DISTRIBUTOR is incomplete` | GPSR address missing | Fill in `GPSR_DISTRIBUTOR` |
| `… is not an OTTO brand and BRAND_ID is not set` | No usable brand | Set `BRAND_ID` |
| `! SKU: CODE title (path)` | OTTO rejected the product | Fix the field shown; the book is retried on the next run |
| `Still pending after …s` | OTTO is slow | Run again later; re-sending is safe |
| `! stock: …` | Quantity refused for some SKUs (207) | The stock is sent again with the next `--resend` |
| `status`: `REJECTED` / `RESTRICTED` | OTTO's content check | The errors are listed under the SKU |

## Limitations

- **Books on otto.de.** OTTO added books to its marketplace with Thalia. Ask
  OTTO whether your account may sell in the books category before you start.
- **Germany only.** Prices include 7 % VAT. **Buchpreisbindung:** new books
  must be sold at the German fixed price.
- **Attributes** depend on the category. Check them with `attributes` and fill
  `ATTRIBUTES` / `FIELD_ATTRIBUTES`, or OTTO may reject the products.
- **Orders, shipments, returns and receipts** have their own OTTO APIs (see
  [capabilities.md](capabilities.md)). They aren't built yet.
- **Where the API facts come from.** The token request follows OTTO's
  developer documentation. The paths and fields follow OTTO's OpenAPI
  specification (v5 products, v1 availability), as bundled in a public SDK.
  The script has not been run against the real OTTO API, because there is no
  partner account yet.

## Testing

```powershell
python tests\test_otto.py
```

The test starts a mock of the OTTO Market API on `127.0.0.1:8765`. The mock
covers:

- the token endpoint, with scope checking
- Products v5: update tasks that go from pending to done, the failed lists,
  paginated brands, categories, prices, active status and marketplace status
- Availability v1, including `207` partial failures

The mock validates products with the field rules of the spec, for example:

- the EAN check digit, and EANs starting with 2
- a usable brand
- at least one image
- max 2 decimals in prices
- the VAT value
- the GPSR distributor

There are 25 checks. Nothing is sent to OTTO.
