# cdon_push.py reference

This script pushes books to **CDON**, the Nordic marketplace (cdon.se, cdon.dk,
cdon.fi, cdon.no). It uses CDON's **merchant API** on the new platform
(reference: [docs.cdon.com](https://docs.cdon.com)). The old CDON Connect API
was closed in 2026. Everything below describes how the script behaves today.

Each book becomes **your own CDON article**, with its ISBN as the GTIN. You
don't attach an offer to an existing catalog product.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [The article data](#the-article-data)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Login.** Every call uses HTTP Basic authentication with
   `merchantID:token`, both from the CDON Merchant Center.
2. **Create.** New books are sent with `POST /v2/articles/bulk`, up to 100
   per call. Each article has:
   - SKU, ISBN (`gtin`), title, description (plain text: CDON forbids HTML),
     publisher (`brand`)
   - main image plus up to 9 more
   - condition `NEW` (`classifications`), category, `shipped_from: EU`
   - stock (`quantity`)
   - per market:
     - the price including VAT in the market's currency (SEK, DKK, EUR,
       NOK), converted from your EUR price
     - the books VAT rate
     - the shipping time
3. **Immediate check.** CDON validates each article straight away. Rejected
   articles come back with the field and the reason, for example
   `main_image: …`. They aren't recorded, so they are tried again on the next
   run.
4. **Background processing.** Accepted articles are processed afterwards. The
   script polls `POST /v1/statuses/batch` for up to `WAIT_SECONDS` (90 s) and
   shows, per book and market, `ok` or `failed` with CDON's error code:
   - `invalid_category`, `invalid_classification`, `invalid_vat_rate`
   - `permission_denied`, `sku_already_exists`

   **Special case:** if the SKU already exists at CDON (made by hand), the
   script takes over that article, and later updates go to it. Books that
   failed are tried again on the next run. Books still processing are checked
   with `status`.
5. **Updates** use `PUT /v2/articles/bulk` actions, and only what changed is
   sent:

   | Option | What it sends |
   |---|---|
   | `--resend` | `update_article_price` and `update_article_quantity`. A paused article that is back in stock is reactivated (`update_article`, status `for sale`). |
   | `--zero-sold-out` | Stock 0 |
   | `--pause-sold-out` | Status `paused` |
   | `--delete-sold-out` | `delete_article` |

6. **Rate limits:** CDON allows 9 000 requests a minute. On `429` the script
   waits until the time in the `X-RateLimit-Burst-Reset` header; on `5xx` it
   retries with backoff.

## Setup

1. **Become a CDON merchant** ([cdon.com](https://cdon.com), "Sell on CDON").
2. **Get your API credentials** in the CDON Merchant Center: the
   **Merchant ID** and an **API token**. CDON gives sandbox credentials on
   request. To use the sandbox, set
   `CDON_API_URL=https://merchants-api.sandbox.cdon.com/api`.
3. **Store them in Bitwarden** (project `product-pushers`), or for a quick
   local test:
   ```powershell
   $env:CDON_MERCHANT_ID = "..."
   $env:CDON_API_TOKEN = "..."
   ```
4. **Check the settings** at the top of `cdon_push.py`:

   | Setting | Default | Meaning |
   |---|---|---|
   | `MARKETS` | `SE`, `DK`, `FI` | Markets to sell in. `NO` is possible, but Norway is outside the EU (customs). |
   | `EUR_RATE` | SE 11.0, DK 7.46, FI 1, NO 11.7 | EUR → market currency. **Update regularly.** |
   | `VAT_RATE` | SE 6, DK 25, FI 14, NO 0 | Books VAT rate per market in %. **Verify with your accountant.** |
   | `TEXT_LANGUAGES` | `en-US` | CDON accepts only `sv-SE`, `en-US`, `da-DK`, `fi-FI` and `nb-NO`. The Spanish texts are sent under these tags. |
   | `CATEGORY` | (empty) | CDON category ID, from `categories --find book` |
   | `SHIPPING_DAYS` | (3, 7) | Delivery time to the customer, min/max days |
   | `DELIVERY_TYPE` | `None` | Optional: `mailbox`, `service_point` or `home_delivery` |
   | `SHIPPING_CONFIGURATIONS` | `{}` | Optional shipping configuration IDs per market (shown by `check`) |
   | `MANUFACTURER` | (empty) | GPSR: the publisher's name and address. Only sent when complete. |

5. **Run `check`**, then `push --dry-run`, then `push --limit 1`.

## Commands

| Command | What it does |
|---|---|
| `check` | Login, your category, shipping configurations, settings, GPSR warning |
| `categories [--find TEXT] [--market SE] [--language en-US]` | CDON categories with their IDs |
| `push [json_file]` | Creates new articles and sends updates, then waits for CDON's result |
| `status` | Per book and market: `ok`, `failed` (with the reason) or `pending` |

### `push`

| Option | Meaning |
|---|---|
| `--dry-run` | Write `cdon_articles.json` only. No credentials needed. |
| `--limit N` | At most N new articles this run |
| `--resend` | Update prices and stock that changed; reactivate paused articles |
| `--zero-sold-out` / `--pause-sold-out` / `--delete-sold-out` | What to do with pushed books that are now sold out |
| `--quantity N` | Stock when the book has no `stock`/`quantity` field (default 1) |
| `--price-factor X` | Multiply source prices |
| `--wait SECONDS` | How long to wait for CDON's processing (default 90) |
| `--yes` | No confirmation (used by `push_all`) |

## Input JSON format

| Field | Use |
|---|---|
| `id` or `sku` | Your SKU (max 64 characters) |
| `isbn` or `ean` | `gtin`. ISBN-10 is converted, and the check digit is verified. |
| `title` | Title (5–150 characters) |
| `description` | Description: HTML is removed, and the text is cut to 4096 characters. If it's shorter than 10 characters, title, publisher and ISBN are used instead. |
| `brand` or `publisher` | `brand` (max 50 characters) |
| `price_eur` or `price` | Gross price in EUR, converted per market |
| `stock` or `quantity` | Stock (default `--quantity`) |
| `images` or `image` | The first one is `main_image`; up to 9 more go in `images` |
| `condition` | Only `NewCondition`; sent as condition `NEW` |
| `sold_out` | New books are skipped. For pushed ones, see the sold-out options. |

## The article data

Example from `cdon_articles.json` (8.50 EUR, shortened):

```json
{
  "sku": "142534",
  "quantity": 1,
  "price": [
    {"market": "SE", "value": {"amount_including_vat": 94, "currency": "SEK", "vat_rate": 6}},
    {"market": "DK", "value": {"amount_including_vat": 63, "currency": "DKK", "vat_rate": 25}},
    {"market": "FI", "value": {"amount_including_vat": 8.5, "currency": "EUR", "vat_rate": 14}}
  ],
  "status": "for sale",
  "main_image": "https://…/142534-….jpg",
  "markets": ["SE", "DK", "FI"],
  "shipping_time": [{"market": "SE", "min": 3, "max": 7}, …],
  "title": [{"language": "en-US", "value": "Dragona. Libro para bebés con rimas"}],
  "description": [{"language": "en-US", "value": "…"}],
  "brand": "Cuentos con Valores",
  "gtin": "9788410413283",
  "classifications": [{"name": "CONDITION", "value": "NEW"}],
  "shipped_from": "EU"
}
```

## Files the script creates

| File | Content |
|---|---|
| `cdon_articles.json` | The last new articles built (also on `--dry-run`) |
| `cdon_posted.json` | Per SKU: ISBN, status (`ok`/`failed`/`pending`), sale status, prices, stock, status per market, CDON's error codes, times |

Both are in `.gitignore`.

## Errors and troubleshooting

| Message | Cause | What to do |
|---|---|---|
| `CDON refused the login (401)` | Wrong Merchant ID or token | Check the Bitwarden secrets (Merchant Center → API) |
| `! SKU: create rejected: field: …` | CDON's immediate validation failed | Fix the field; the book is tried again on the next run |
| `! SKU: CDON rejected create_article: SE: invalid_category` | Wrong `CATEGORY` | Find the right ID with `categories --find` |
| `… invalid_vat_rate` | The VAT rate isn't accepted for that market | Check `VAT_RATE` |
| `… permission_denied` | Your account can't sell in that market or category | Ask CDON |
| `already exists at CDON - taken over` | The SKU existed already | Nothing; updates go to that article |
| `N still processing` | CDON hasn't finished | Run `status` later |

## Limitations

- **No Spanish.** CDON shows texts only in Swedish, English, Danish, Finnish
  and Norwegian. The Spanish titles and descriptions are sent under
  `TEXT_LANGUAGES`, so Nordic buyers see Spanish text. That's fine for
  Spanish-language books, but say it in the listing if needed.
- **Exchange rates** are fixed in `EUR_RATE`. They aren't fetched live.
- **New books only.** Used books would need CDON's `REFURB` condition, which
  is meant for refurbished goods.
- **Text and image changes** of existing articles aren't sent. `--resend`
  sends only price, stock and status.
- **GPSR:** the manufacturer (publisher) is only sent when `MANUFACTURER` is
  filled in completely.
- **Orders** (accept, fulfil, cancel), shipping configurations and payment
  reports have their own CDON API resources. They aren't built yet; see
  [capabilities.md](capabilities.md).
- **Not tested against the real CDON API**, because there is no merchant
  account yet. Only the local mock below has been used.

## Testing

```powershell
python tests\test_cdon.py
```

The test starts a mock of the CDON merchant API on a free local port. It
follows the documented rules:

- Basic authentication
- bulk create and update with 100 items at most, required fields and length
  limits
- market and currency pairs, no HTML in descriptions
- the update actions and their bodies
- background processing: `pending` first, then `ok` or `failed` with error
  codes, including `sku_already_exists`
- categories, shipping configurations, and `429` rate limiting

There are 26 checks. Nothing is sent to CDON.
