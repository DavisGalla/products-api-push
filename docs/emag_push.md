# emag_push.py reference

This script pushes books to **eMAG Marketplace**, the largest online
marketplace in Romania, which also runs in Bulgaria and Hungary. It uses the
official **eMAG Marketplace API** ("api-3", documentation v4.5.1 of
02.03.2026). Everything below describes how the script behaves today.

Each book is matched to an **existing eMAG product** by its ISBN/EAN, and
your offer (price, stock, handling time, VAT) is attached to it. Books eMAG
doesn't know are skipped: creating a new product needs the full product
documentation and a manual review by eMAG's catalogue team.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [The offer data](#the-offer-data)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Login.** Every call uses HTTP Basic authentication (API user +
   password). eMAG only accepts calls from **whitelisted IP addresses**.
   Every call is a POST with the payload in a `data` key, and eMAG answers
   with `isError`, `messages` and `results`.
2. **Catalog match.** The ISBNs of all new books are looked up with
   `GET documentation/find_by_eans`, up to 100 per call. For each book eMAG
   returns:
   - the product's `part_number_key` (PNK)
   - whether you may sell in its category (`allow_to_add_offer`)
   - whether you already have an offer on it (`vendor_has_offer`)

   A book is skipped when eMAG doesn't know it, when you have no access to
   its category, or when you already have an offer on that product that
   wasn't made by this script. eMAG allows only one offer per seller per
   product.
3. **VAT.** eMAG prices are **without VAT**, and every offer needs a
   `vat_id`. The script reads the platform's VAT rates (`vat/read`) and uses
   the one that matches `VAT_RATE`.
4. **Offer.** New offers are sent with `product_offer/save` in batches of
   50, the API limit. Each offer has:
   - a numeric `id`: your offer ID at eMAG, kept per SKU in `emag_ids.json`
   - `part_number_key`, `name`, `status` 1
   - `sale_price`, plus `min_sale_price` / `max_sale_price`: eMAG's price
     check window, which is required on the first save
   - `vat_id`, `stock`, `handling_time`
   - `currency_type: EUR` when the platform currency isn't EUR

   If eMAG reports an error for a batch, the script re-sends that batch one
   offer at a time to find the offers that were rejected and show eMAG's
   message for each. Saving the same `id` again is safe.
5. **Updates.** These use the light `offer/save`, which sends only what
   changed:
   - **`--resend`:** new price and stock. If the new price falls outside the
     saved window, a new min/max window is sent with it. Inactive offers are
     reactivated.
   - **Sold out:** `--zero-sold-out` sets stock 0, and `--offline-sold-out`
     sets status 0 (inactive).
6. **Status.** `status` reads your offers (`product_offer/read`, 100 per
   page). For each one it shows:
   - whether it is for sale, which needs: status 1, a valid price, stock > 0
     and approved documentation
   - eMAG's validation messages
   - your buy button rank, the number of offers and the best price

The script waits 0.35 s between calls, because eMAG allows 3 requests per
second outside the orders API. It retries `429` and `5xx` responses.

## Setup

1. **Become an eMAG Marketplace seller** on the platform you want:
   [marketplace.emag.ro](https://marketplace.emag.ro),
   [.bg](https://marketplace.emag.bg) or [.hu](https://marketplace.emag.hu).
   eMAG has a cross-border programme for EU companies. VAT registration and
   GPSR rules apply.
2. **Request access to the books categories** in the Marketplace interface.
   `lookup` shows whether you have it for a given book.
3. **Create an API user** and send eMAG the **IP addresses** the script runs
   from (your server). Calls from other IPs are refused.
4. **Store the credentials in Bitwarden** (project `product-pushers`), or for
   a quick local test:
   ```powershell
   $env:EMAG_USERNAME = "..."
   $env:EMAG_PASSWORD = "..."
   ```
5. **Check the settings** at the top of `emag_push.py`:

   | Setting | Default | Meaning |
   |---|---|---|
   | `PLATFORM` | `RO` | `RO` (emag.ro, RON), `BG` (emag.bg, EUR since 1 Jan 2026) or `HU` (emag.hu, HUF) |
   | `VAT_RATE` | RO 11, BG 9, HU 5 | Reduced VAT rate for books, in percent. **Verify with `check` and your accountant.** |
   | `VAT_ID` | `None` | Force a `vat_id` instead |
   | `PRICES_IN_EUR` | `True` | Send prices in EUR (`currency_type`). eMAG converts them, and recalculates EUR prices at the start of every month. |
   | `EUR_RATE` | RO 5.09, HU 390 | Only used when `PRICES_IN_EUR = False` |
   | `MIN_PRICE_FACTOR` / `MAX_PRICE_FACTOR` | 0.8 / 1.5 | The price check window around your price. Offers outside it are rejected. |
   | `HANDLING_TIME_DAYS` | 2 | Days from order to shipment. Must be one of the values `check` shows. |
   | `WAREHOUSE_ID` | 1 | 1 for a single warehouse |

6. **Run `check`.**

## Commands

| Command | What it does |
|---|---|
| `check` | Login, VAT rates (and the books VAT ID), allowed handling times |
| `lookup ISBN` | Whether eMAG knows the book: product, PNK, category, whether you may add an offer |
| `push [json_file]` | Creates and updates offers |
| `status` | Offer status, price validation, documentation status, buy button rank |

### `push`

| Option | Meaning |
|---|---|
| `--dry-run` | Write `emag_offers.json` only. No credentials needed (PNK and `vat_id` are placeholders). |
| `--limit N` | At most N new offers this run |
| `--resend` | Update price and stock of offers already pushed (only what changed) |
| `--zero-sold-out` | Stock 0 for pushed books that are now sold out |
| `--offline-sold-out` | Status inactive for pushed books that are now sold out |
| `--quantity N` | Stock when the product has no `stock`/`quantity` field (default 1) |
| `--price-factor X` | Multiply source prices |
| `--yes` | No confirmation (used by `push_all`) |

## Input JSON format

| Field | Use |
|---|---|
| `id` or `sku` | Your SKU. It gets a numeric eMAG offer ID in `emag_ids.json`. |
| `isbn` or `ean` | Catalog match. ISBN-10 is converted, and the check digit is verified. |
| `title` | `name` (max 255 characters) |
| `price_eur` or `price` | Gross price in EUR. The script removes VAT. |
| `stock` or `quantity` | Stock (default `--quantity`) |
| `condition` | Only `NewCondition` |
| `sold_out` | Skipped, or `--zero-sold-out` / `--offline-sold-out` |

## The offer data

Example from `emag_offers.json`: 8.50 EUR gross, 11 % VAT, emag.ro.

```json
{
  "id": 1,
  "part_number_key": "D5DD9BBBM",
  "name": "Dragona. Libro para bebés con rimas",
  "status": 1,
  "sale_price": 7.66,
  "min_sale_price": 6.13,
  "max_sale_price": 11.49,
  "vat_id": 4,
  "stock": [{"warehouse_id": 1, "value": 1}],
  "handling_time": [{"warehouse_id": 1, "value": 2}],
  "currency_type": "EUR"
}
```

## Files the script creates

| File | Content |
|---|---|
| `emag_offers.json` | The last new offers built (also on `--dry-run`) |
| `emag_posted.json` | Per SKU: offer ID, PNK, EAN, price, price window, currency, stock, status, times |
| `emag_ids.json` | SKU → numeric eMAG offer ID. **Keep this file**: the ID identifies your offer at eMAG. |

All three are in `.gitignore`.

## Errors and troubleshooting

| Message | Cause | What to do |
|---|---|---|
| `eMAG refused the login (401)` | Wrong user/password, no API rights, or the IP isn't whitelisted | Check the Bitwarden secrets; ask eMAG to whitelist the server IP |
| `not in the eMAG catalog` | eMAG has no product with this ISBN | Can't be sold through this script (new products need full documentation) |
| `no access to category '…'` | Your account can't sell in that category | Request access in the Marketplace interface |
| `you already have an offer on …` | An offer on that product exists already (made by hand) | Update it in eMAG, or delete it and run again |
| `! SKU: Offer N: …` | eMAG rejected the offer, e.g. failed price check or invalid VAT | Fix it; the offer is retried on the next run with the same ID |
| `No eMAG VAT rate of X% …` | `VAT_RATE` doesn't match a platform rate | Set `VAT_RATE` or `VAT_ID` (see `check`) |
| `status`: `not for sale (Invalid price)` | The price check flagged the price | Correct the price, or wait for eMAG's manual validation |

## Limitations

- **Existing products only.** New eMAG products need the full documentation
  (category characteristics, images, description) and a manual review. That
  isn't built.
- **New books only.** The API has no condition field for offers.
- **VAT rates** change, for example Romania raised the books rate from 5 %
  to 11 % in August 2025. Verify `VAT_RATE` before selling.
- **GPSR:** eMAG requires manufacturer or EU representative information for
  products. When you attach an offer to an existing eMAG product, that data
  normally comes from the product. Check the GPSR section in the Marketplace
  interface.
- **Orders, AWB (shipping labels), returns and invoices** have their own
  eMAG API resources. They aren't built yet; see
  [capabilities.md](capabilities.md).
- **Not tested against the real eMAG API**, because there is no seller
  account yet. Only the local mock below has been used.

## Testing

```powershell
python tests\test_emag.py
```

The test starts a mock of the eMAG Marketplace API on `127.0.0.1:8765`. It
follows the v4.5.1 rules:

- Basic authentication
- the `{"data": …}` envelope, and `isError` / `messages` responses
- 50 entities per save, 100 EANs per `find_by_eans`
- VAT and handling-time lists
- attaching by PNK, the min/max price window, one offer per product
- the light `offer/save`
- paginated `product_offer/read`
- `429` rate limiting

There are 23 checks. Nothing is sent to eMAG.
