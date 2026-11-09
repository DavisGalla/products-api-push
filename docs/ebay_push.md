# ebay_push.py reference

This script pushes products to eBay EU marketplaces through the official
**eBay Sell Inventory API**. For each product it creates an inventory item,
then an offer, then publishes the offer. One run can list on one eBay site or
on several (`--markets`). Everything below describes how it behaves today.

- [Setup](#setup)
- [Usage](#usage)
- [Options](#options)
- [Input JSON format](#input-json-format)
- [How products are converted](#how-products-are-converted)
- [What happens during a run](#what-happens-during-a-run)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## Setup

**Requirements:** Python 3.8 or newer, plus the `requests` package.

```powershell
pip install -r requirements.txt
```

**eBay developer account.** You only need to do this once.

1. Register at <https://developer.ebay.com> and create a **Production** keyset.
   For testing, also create a **Sandbox** keyset.
2. In the developer portal, open **User Tokens** and sign in as your seller
   account. Get a **refresh token** with the scopes `sell.inventory` and
   `sell.account`.
3. In **eBay Seller Hub**, opt in to **Business Policies**, then create
   **for each eBay site you sell on**:
   - a shipping (fulfillment) policy
   - a payment policy
   - a return policy

   Also create at least one **inventory location**. The location is shared
   by all sites.

**Environment variables:**

```powershell
$env:EBAY_CLIENT_ID     = "..."
$env:EBAY_CLIENT_SECRET = "..."
$env:EBAY_REFRESH_TOKEN = "v^1.1#..."
```

These variables are optional. By default the script uses, for each site, the
first policy of each kind on that site, and the first location it finds. The
policy overrides apply to every site, so only use them with a single site.

| Variable | Overrides |
|---|---|
| `EBAY_FULFILLMENT_POLICY_ID` | Shipping policy |
| `EBAY_PAYMENT_POLICY_ID` | Payment policy |
| `EBAY_RETURN_POLICY_ID` | Return policy |
| `EBAY_LOCATION_KEY` | Inventory location (`merchantLocationKey`) |

## Usage

```powershell
python ebay_push.py <json_file> [options]
```

Use this order for your first run:

```powershell
python ebay_push.py books.json --dry-run                     # show payloads, nothing is created
python ebay_push.py books.json --sandbox --limit 1           # eBay test site (needs sandbox keys)
python ebay_push.py books.json --no-publish --limit 1        # real account, listing NOT live
python ebay_push.py books.json --limit 1                     # one real live listing
python ebay_push.py books.json                               # everything (ebay.es)
python ebay_push.py books.json --markets EBAY_ES,EBAY_DE      # several sites in one run
python ebay_push.py books.json --markets all                  # all 11 sites
```

When the script is about to publish live listings, it asks you to type `yes`.
Use `--yes` to skip that question.

## Options

| Option | Default | Meaning |
|---|---|---|
| `json_file` | required | Products file. |
| `--market` | `EBAY_ES` | One site: `EBAY_ES`, `EBAY_DE`, `EBAY_FR`, `EBAY_IT`, `EBAY_NL`, `EBAY_AT`, `EBAY_BE`, `EBAY_IE`, `EBAY_GB`, `EBAY_CH` or `EBAY_PL`. Sets the language, currency and category tree. |
| `--markets` | – | Several sites in one run, comma separated (`EBAY_ES,EBAY_DE,EBAY_FR`), or `eur` (the 8 euro sites) or `all` (all 11). Each site is listed separately; a site without business policies is skipped with a message. |
| `--category-id` | suggested | eBay leaf category for every item. If you leave it out, eBay's taxonomy API suggests one from each title. |
| `--quantity` | `1` | Stock quantity per listing. |
| `--language` | `BOOK_LANGUAGE` | Value of the book's *Language* aspect. By default the books' language (`BOOK_LANGUAGE = "es"`) in each site's own words: `Español`, `Spanisch`, `Espagnol`, `Spagnolo`, `Spaans`, `Hiszpański`, `Spanish`. |
| `--limit N` | `0` (all) | Processes at most N new items per site. |
| `--no-publish` | off | Creates the inventory item and offer but doesn't publish them. |
| `--dry-run` | off | Prints the payloads. Without credentials, it doesn't call eBay at all. |
| `--sandbox` | off | Uses `api.sandbox.ebay.com`. This needs sandbox keys. |
| `--yes` | off | Skips the confirmation prompt before publishing. |

## Input JSON format

The file can be a list of products or `{"products": [...]}`. These are the
fields the script reads:

| Field | Purpose |
|---|---|
| `id` or `sku` | Your SKU (required, at most 50 characters). It's also used to detect duplicates. |
| `title` | Listing title (cut to 80 characters). |
| `description` or `description_html` | `description` is plain text, and line breaks become `<br>`. `description_html` is used unchanged. |
| `price_eur` or `price` | Price in EUR. The UK, Swiss and Polish sites get it converted to GBP, CHF and PLN with `EUR_RATE` at the top of the script (update the rates regularly). |
| `images`, `image_urls`, `photo_urls` or `image` | **Public** image URLs (up to 24 are used). |
| `brand` | Sent as the *Publisher* aspect. |
| `isbn` | Sent as the ISBN aspect and product identifier, with hyphens removed. |
| `author` | Sent as the *Author* aspect. Book categories often require it. |
| `language` | Book language. Falls back to `--language`. |
| `condition` | See [Condition](#condition). |
| `sold_out` | `true` means the product is skipped. |

Example (scraper format):

```json
{
  "id": "142534",
  "title": "Dragona. Libro para bebés con rimas",
  "price_eur": 8.5,
  "brand": "Cuentos con Valores",
  "isbn": "9788409591923",
  "condition": "NewCondition",
  "description": "Dragona. Libro para bebés con rimas...",
  "images": ["https://.../photo.jpg"]
}
```

## How products are converted

### Inventory item (`PUT /sell/inventory/v1/inventory_item/{sku}`)

- `condition` comes from the [condition mapping](#condition).
- `availability.shipToLocationAvailability.quantity` is set from `--quantity`.
- `product.title`, `product.description` and `product.imageUrls` come from the product.
- `product.aspects`:
  - **Book Title**, and **Language** (from the product or `--language`)
  - **ISBN**, **Author** and **Publisher**, when the product has them
- `product.isbn` is set when the product has an ISBN.

### Offer (`POST` or `PUT /sell/inventory/v1/offer`)

- `format` is `FIXED_PRICE`, and `availableQuantity` comes from `--quantity`.
- `categoryId` comes from `--category-id` or the taxonomy suggestion.
- The price is formatted to 2 decimals, in the market's currency.
- `listingPolicies` holds your fulfillment, payment and return policy IDs.
- `merchantLocationKey` is your inventory location.
- `includeCatalogProductDetails` is `true`, so eBay can fill details from its
  catalog when it recognises the ISBN.

### Condition

| Source value | eBay condition |
|---|---|
| `NewCondition` (or missing) | `NEW` |
| `UsedCondition` | `USED_VERY_GOOD` |
| `RefurbishedCondition` | `SELLER_REFURBISHED` |
| `DamagedCondition` | `FOR_PARTS_OR_NOT_WORKING` |
| an eBay enum value, for example `USED_GOOD` | used as-is |

Edit `CONDITION_MAP` at the top of the script to change this. Each category
only allows certain conditions. If eBay rejects one for books, switch that
mapping to an allowed value such as `USED_GOOD`.

## What happens during a run

1. A product is skipped if it has no SKU, title, price or image URL, if it's
   sold out, or if `ebay_posted.json` already records it as done for this
   market.
2. The script logs in. It swaps your refresh token for a user token, and gets
   an application token for the taxonomy API.
3. It looks up your policies and location, and stops if any are missing.
4. For each product, it creates or replaces the inventory item, then creates
   or updates the offer for that SKU, then publishes it. It waits 0.5 seconds
   between products.
5. The result (`offerId`, `listingId`, `published`) is saved to `ebay_posted.json`.

If a product fails, the error is printed and the run continues with the next
one. Running the script again retries only the products that aren't done, and
offers that already exist are updated rather than duplicated.

## Files the script creates

These are created in the folder you run the script from.

| File | Contents |
|---|---|
| `ebay_posted.json` | `MARKET:SKU` mapped to `offerId`, `listingId` and `published`. Delete an entry to push that product again. |

## Errors and troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `KeyError: 'EBAY_CLIENT_ID'` | The environment variables aren't set | Set the three `EBAY_*` variables. |
| `token error 400 ... invalid_grant` | The refresh token is expired or wrong, or it's for a different environment | Generate a new user token (sandbox and production are separate). |
| `No inventory location found` | No location exists | Create one in Seller Hub, or set `EBAY_LOCATION_KEY`. |
| `Missing shipping/return business policy` | Business policies aren't set up for that market | Opt in to Business Policies and create them. |
| `no category suggestion; pass --category-id` | The taxonomy API found nothing for the title | Pass `--category-id`. |
| Publish fails with a missing aspect, for example `Author` | The category requires that aspect | Add the field (for example `author`) to your products. |
| Publish fails with a condition error | The category doesn't allow that condition | Change `CONDITION_MAP`. |

## Limitations

- **Images must be public URLs.** Local files aren't uploaded.
- **Several sites, one copy.** With `--markets`, the same book is listed separately on each site. If you have one copy, it can sell on two sites at once until stock is synced; a book listed on one site can also be shown to buyers abroad if your shipping policy allows it.
- **Exchange rates** for GBP, CHF and PLN are fixed in `EUR_RATE`, not fetched live.
- **Creating and updating listings only.** Ending listings, syncing stock and handling orders aren't implemented.
- **The category is suggested from the title** unless you pass `--category-id`, so check what it suggests.
- **The script hasn't been run against the real eBay API yet.** Follow the first-run order under [Usage](#usage).

## Testing

```powershell
python tests\test_ebay.py
```

[tests/test_ebay.py](../tests/test_ebay.py) starts a fake eBay API on
`127.0.0.1:8765` and runs the script against
[tests/test_products.json](../tests/test_products.json). The fake API covers
OAuth, policies, location, taxonomy, inventory, offers and publishing. The test
checks the following:

- the dry run works
- invalid products are skipped
- titles are limited to 80 characters
- ISBNs are formatted correctly
- prices are formatted correctly
- category, policies and location are set
- conditions are mapped
- nothing is published twice on a re-run
- `--markets`: several sites in one run, each with its own policies, the
  right currency (GBP conversion checked) and the language in the site's
  words; a site without policies is skipped; the state keeps sites apart;
  `eur` expands to the 8 euro sites

Your real `ebay_posted.json` is never touched, because the test runs in a
temporary folder.
