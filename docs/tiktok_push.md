# tiktok_push.py reference

This script pushes books to **TikTok Shop** in Spain and the other EU shops,
through the official **TikTok Shop Partner API** (<https://partner.tiktokshop.com>,
gateway `https://open-api.tiktokglobalshop.com`, API version 202309 and
later). Everything below describes how the script behaves today.

Like AliExpress and Temu, TikTok Shop has no catalog to attach an offer to,
so **every book becomes its own product**, with a title, description, photos,
category attributes, warehouse stock, a weight and a size. TikTok **reviews
every new product** before it goes live.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to TikTok Shop](#what-is-sent-to-tiktok-shop)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Category plan:** all books go into one TikTok leaf category
   (`CATEGORY_ID`). The script reads that category's attributes and rules
   from TikTok:
   - A required **ISBN** attribute is filled with each book's ISBN automatically.
   - Other required attributes, such as language, come from
     `PRODUCT_ATTRIBUTES`. `attributes` prints them ready to paste.
   - If the category requires the EU **responsible person** and
     **manufacturer** (GPSR), the first ones set up in Seller Center are used,
     unless you set `RESPONSIBLE_PERSON_ID` and `MANUFACTURER_ID`.
   - Stock goes to your default sales warehouse, unless you set `WAREHOUSE_ID`.

   Anything still missing stops the run before a single product is sent.
2. **Photos:** each photo is downloaded and uploaded to TikTok as a file
   (`/product/202309/images/upload`, `use_case=MAIN_IMAGE`), because TikTok
   only accepts images it hosts. TikTok adjusts the aspect ratio itself.
   Uploaded photos are cached.
3. **Create:** the script sends one `POST /product/202309/products` per book.
   - The ISBN goes in as the SKU's identifier code (type `ISBN`).
   - Your product ID goes in as `external_sku_id`.
   - It also becomes the seller SKU, padded to TikTok's 6–50 characters
     (`T-NEW` becomes `SKU-T-NEW`).
   - An idempotency key stops TikTok creating a duplicate if a request is
     retried.
4. **Record:** successful products are saved in `tiktok_posted.json` with
   TikTok's product and SKU IDs.
5. **Sync:**
   - `--resend` updates stock (`inventory/update`) and price (`prices/update`),
     only when they changed.
   - `--offline-sold-out` deactivates sold-out books.
   - A book that comes back in stock is reactivated by `--resend`.

Every request is signed as TikTok documents it. The signature is an
HMAC-SHA256 with your App Secret, over the secret, the API path, the sorted
query parameters, the JSON body and the secret again, as lowercase hex. The
access token goes in the `x-tts-access-token` header. The implementation is
checked against the worked examples in TikTok's documentation.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Seller account.** You need a TikTok Shop seller account for Spain, from
seller-es.tiktok.com, with permission to sell books.

**2. App.** At <https://partner.tiktokshop.com>:
1. Create a Partner Center account and a **custom app** for your own shop.
2. Request the **Product**, **Logistics** and **Authorization** API scopes.
3. Note the app's **App Key**, **App Secret** and **Service ID**.
4. Set the app's **redirect URL**. Any URL you control works, because you
   only need to copy the `code` from it.

**3. Seller Center.**
- A **warehouse** to ship from.
- An EU **responsible person** and a **manufacturer** (GPSR), if your books
  category requires them.

`check` and `attributes` show all of this.

**4. Credentials.** Add them to Bitwarden as `TIKTOK_APP_KEY` and
`TIKTOK_APP_SECRET` (see [push_all.md](push_all.md#where-credentials-are-stored)),
or for a quick local test:

```powershell
$env:TIKTOK_APP_KEY = "..."
$env:TIKTOK_APP_SECRET = "..."
```

**5. Authorize the app for your shop.** This is done once, and again only if
the authorization expires or is revoked.

```powershell
python tiktok_push.py auth-url --service-id <service id>
# open the printed link, log in with the SELLER account, authorize,
# then copy the code=... from the address you are sent to
python tiktok_push.py token <code>
```

The tokens and your shop's ID (the "shop cipher") are saved in
`tiktok_token.json`. That file is in `.gitignore` and never goes to GitHub.
The access token is refreshed automatically 30 minutes before it expires.

On a server, copy `refresh_token` from that file into Bitwarden as
`TIKTOK_REFRESH_TOKEN`. The script uses it when there's no token file yet.
`TIKTOK_SHOP_CIPHER` is optional, and only needed if the seller authorized
several shops.

**6. Category.**
1. Run `categories --find libro`, or `recommend "<a book title>"`, to find a
   **leaf** category without restrictions (not `INVITE_ONLY` or `PROHIBITED`).
2. Run `attributes <id>` and paste the lines for its required attributes into
   `PRODUCT_ATTRIBUTES`.
3. Set `CATEGORY_ID`.

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `CURRENCY`, `LOCALE` | `EUR` and `es-ES` for Spain. |
| `CATEGORY_ID` | TikTok leaf category for all books. Required for real runs. |
| `PRODUCT_ATTRIBUTES` | Required attributes that can't be filled automatically (pasted from `attributes`). |
| `ALLOWED_CONDITIONS` | Source conditions that are pushed. Only `NewCondition` by default. |
| `WAREHOUSE_ID` | Warehouse for the stock. Empty means your default sales warehouse. |
| `RESPONSIBLE_PERSON_ID`, `MANUFACTURER_ID` | GPSR IDs, when the category requires them. Empty means the first ones in Seller Center. |
| `DEFAULT_WEIGHT_KG`, `PACKAGE_CM` | Parcel weight and size (whole cm), used when a product has no `weight_kg`. |

## Commands

| Command | What it does |
|---|---|
| `auth-url --service-id ID` | Prints the link the seller opens to authorize the app. |
| `token CODE` | Exchanges the authorization code for tokens and saves them. |
| `check` | Shows the authorized shops, warehouses, EU responsible persons and manufacturers. |
| `categories [--find TEXT]` | Searches TikTok's categories. Marks leaf categories and restrictions (`INVITE_ONLY`, `PROHIBITED`, …). |
| `recommend "TITLE"` | Shows TikTok's suggested category for a title. |
| `attributes CATEGORY_ID` | Shows the category's product attributes (required first) as ready-to-paste `PRODUCT_ATTRIBUTES` lines, and its rules (GPSR, certifications). |
| `push` | Creates, updates or deactivates products (see below). |
| `status` | Shows each product's TikTok status (LIVE, in review, REJECTED, deactivated, …), price and stock. |

### `push`

```powershell
python tiktok_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Stock per product, from 1 to 99,999. A product's own `quantity` takes precedence. |
| `--price-factor` | `1.0` | Multiplies every source price, rounded half-up to cents. See the [fixed book price](#limitations). |
| `--category` | `CATEGORY_ID` | TikTok leaf category. |
| `--warehouse` | default warehouse | Warehouse ID. |
| `--resend` | off | Updates stock and price of books that were already pushed, when they changed. |
| `--offline-sold-out` | off | Deactivates pushed books that are now `sold_out`. |
| `--dry-run` | off | Writes `tiktok_products.json` and sends nothing. Without credentials or `CATEGORY_ID`, placeholders are used. |
| `--limit N` | `0` (all) | Creates at most N new products. |
| `--yes` | off | Skips the confirmation prompt. |

Use this order for your first run:

```powershell
python tiktok_push.py check                       # shop, warehouse, GPSR
python tiktok_push.py categories --find libro     # or: recommend "<title>"
python tiktok_push.py attributes <id>             # fill PRODUCT_ATTRIBUTES, set CATEGORY_ID
python tiktok_push.py push --dry-run              # look at tiktok_products.json
python tiktok_push.py push --limit 1              # one real product
python tiktok_push.py status                      # in review / LIVE?
python tiktok_push.py push                        # the rest
python tiktok_push.py push --resend --offline-sold-out   # later: sync stock/price, hide sold-out
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Used for |
|---|---|
| `id` or `sku` | `external_sku_id`, and the seller SKU (padded to 6–50 characters). **Required.** |
| `isbn` or `ean` | Identifier code (type `ISBN`) and the ISBN attribute. **Required.** ISBN-10/13 or EAN. |
| `title` | Product title (at most 255 characters). **Required.** |
| `description` | HTML description. Each line becomes a paragraph, and the ISBN is added. |
| `price_eur` or `price` | Price in EUR, rounded half-up after `--price-factor`. |
| `images` or `image` | 1 to 9 photos (300–4000 px). **Required.** |
| `condition` | Must be in `ALLOWED_CONDITIONS`. |
| `quantity`, `weight_kg` | Optional per-product stock and parcel weight. |
| `sold_out` | `true` means the product is skipped, or deactivated with `--offline-sold-out`. |

## What is sent to TikTok Shop

`POST /product/202309/products` gets this:

```json
{
  "save_mode": "LISTING",
  "title": "Dragona. Libro para bebés con rimas",
  "description": "<p>Dragona. Libro para bebés con rimas...</p><p>ISBN: 9788409591923</p>",
  "category_id": "601450",
  "main_images": [{"uri": "tos-maliva-i-..."}],
  "product_attributes": [{"id": "100107", "values": [{"id": "1000201"}]},
                         {"id": "100990", "values": [{"name": "9788409591923"}]}],
  "skus": [{
    "seller_sku": "142534", "external_sku_id": "142534",
    "price": {"amount": "8.50", "currency": "EUR"},
    "inventory": [{"warehouse_id": "7268...", "quantity": 1}],
    "identifier_code": {"code": "9788409591923", "type": "ISBN"}
  }],
  "package_weight": {"value": "0.400", "unit": "KILOGRAM"},
  "package_dimensions": {"length": "24", "width": "17", "height": "3", "unit": "CENTIMETER"},
  "responsible_person_ids": ["..."], "manufacturer_ids": ["..."],
  "external_product_id": "142534",
  "idempotency_key": "pp-142534-9788409591923"
}
```

The attribute and category IDs come from your category. The last batch the
script built is saved in `tiktok_products.json`.

## Files the script creates

| File | Contents |
|---|---|
| `tiktok_posted.json` | SKU mapped to `product_id`, `sku_id`, `seller_sku`, `warehouse_id`, `ean`, `price`, `quantity`, `category`, `active` and times. Delete an entry to create that book again. |
| `tiktok_products.json` | The last set of products that was built. |
| `tiktok_token.json` | Access and refresh tokens, and the shop cipher. **Secret**, not in git. |
| `tiktok_cache.json` | Your photo URLs mapped to TikTok image URIs, so photos aren't uploaded twice. |

## Errors and troubleshooting

TikTok errors have a numeric code and a message. The script prints both, for
example `! 142534: 12052046 The image dimensions must be between 300x300 px and 4000x4000 px`.

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | App Key or Secret isn't set | Set `TIKTOK_APP_KEY` and `TIKTOK_APP_SECRET`. |
| `No TikTok Shop authorization yet` | The app was never authorized | Run `auth-url`, then `token <code>`. |
| `Check TIKTOK_APP_SECRET` | Wrong App Secret (signature rejected) | Copy it from Partner Center again. |
| `Authorize again` | The tokens expired or were revoked | Run `auth-url`, then `token <code>`. |
| `CATEGORY_ID is not set` | No category yet | See `categories`, `recommend` and `attributes`. |
| `Category … needs: …` | Required attribute, warehouse or GPSR information missing | Fill in `PRODUCT_ATTRIBUTES`, or add it in Seller Center (`check`). |
| Image dimension error | Photo smaller than 300×300 px or larger than 4000×4000 px | Use larger photos. |
| `status` shows `REJECTED` | TikTok's review failed | See the reason in Seller Center, fix the product there. |
| `status` shows `DEACTIVATED BY TIKTOK` or `FROZEN` | Policy issue | Check Seller Center. |
| Price update fails | The product isn't live yet, or it's in a promotion | Run `--resend` again after TikTok's review. |
| `(API returned 429, retrying …)` | Rate limit | Retried automatically. |

## Limitations

- **One category for all books**, with one set of `PRODUCT_ATTRIBUTES`. If
  books belong in different categories, run them separately with `--category`.
- **New books only** (`ALLOWED_CONDITIONS`).
- **Spanish fixed book price.** New books have a price fixed by law
  (Ley 10/2007, at most 5% discount). Don't use `--price-factor` to go above
  the cover price, and keep TikTok promotions and coupons within the legal
  discount.
- **Stock can't go to 0 through the API.** TikTok's stock range is 1 to
  99,999, so sold-out books are deactivated instead (`--offline-sold-out`).
- **Books in other EU countries.** Selling across the EU from a Spanish shop
  doesn't cover books in every country. Third-party guides say Germany and
  France exclude books because of their national rules.
- **Not verified:**
  - TikTok's documentation is a web app. This script follows the official
    reference through an OpenAPI spec generated from it
    ([isaackogan/TikTok-Shop-Api-SDKs](https://github.com/isaackogan/TikTok-Shop-Api-SDKs))
    and a widely used PHP client
    ([EcomPHP/tiktokshop-php](https://github.com/EcomPHP/tiktokshop-php)),
    which sign requests the same way.
  - The exact error codes for token and rate-limit errors. The script
    recognises them by their message.
- **The script hasn't been run against the real TikTok Shop API yet.** Start
  with `check`, `attributes`, `--dry-run` and `--limit 1`.

## Testing

```powershell
python tests\test_tiktok.py
```

[tests/test_tiktok.py](../tests/test_tiktok.py) checks the signature against
TikTok's documented examples (GET, JSON body, excluded parameters). It then
starts a fake TikTok Shop Partner API on `127.0.0.1:8765`.

The fake recomputes the signature of every request, leaving the body out for
multipart uploads. It checks the access token header and the shop cipher, and
implements every call the script uses. It applies the documented rules:
- leaf category, uploaded images and required attributes
- GPSR IDs and a sales warehouse
- whole-cm package size and a kilogram weight
- a 6–50 character seller SKU and the ISBN identifier
- no duplicate SKUs

It answers one call with HTTP 429, to test the retry.

The test checks the following:
- credentials, authorization and signature errors are reported
- the token exchange, automatic refresh, and `TIKTOK_REFRESH_TOKEN`
- the shop cipher lookup
- `check`, `categories`, `recommend` and `attributes`
- the dry run, with placeholders
- a missing category, attribute or GPSR person stops the run before anything is sent
- products are created with the ISBN attribute, GPSR IDs and the default warehouse
- photos are uploaded once, and an image rejection is shown and not recorded
- re-runs don't create duplicates
- `--resend` (half-up rounding, no calls when nothing changed)
- `--offline-sold-out`, and reactivation on a later `--resend`
- `status`

Your real `tiktok_posted.json` and `tiktok_token.json` are never touched,
because the test runs in a temporary folder.
