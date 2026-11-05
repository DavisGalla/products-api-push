# aliexpress_push.py reference

This script pushes books to **AliExpress** as a local (POP) seller, for example
on aliexpress.es. It uses the official **AliExpress Open Platform** API
(<https://openservice.aliexpress.com>, gateway `https://api-sg.aliexpress.com`).
Everything below describes how the script behaves today.

AliExpress works differently from the Mirakl marketplaces and Kaufland. There
is no catalog to attach an offer to, so **every book becomes its own product**,
with a title, description, photos, category attributes, a shipping template
and a weight.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to AliExpress](#what-is-sent-to-aliexpress)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **EAN lookup:** for each book, the script asks AliExpress's EAN database
   (`aliexpress.product.eancode.info.query`) about the ISBN. If AliExpress
   knows the book, the script uses AliExpress's category, attributes and
   images. That's the easiest path, because nothing has to be mapped by hand.
2. **Fallback:** if AliExpress doesn't know the ISBN, the script uses
   `CATEGORY_ID` and `EXTRA_ATTRIBUTES` from the settings. It uploads your own
   photos to your AliExpress photo bank, because AliExpress only accepts
   images it hosts. If `CATEGORY_ID` isn't set, the book is skipped.
3. **Create:** the script sends one `aliexpress.solution.product.post` per
   book. A single SKU is created, with your product ID as `sku_code` and the
   ISBN as `ean_code`. The title and description are sent in Spanish, and
   AliExpress translates them for other countries.
4. **Online:** new products are created offline, then switched online with
   `onlineaeproduct` (in batches of 50). AliExpress may review a product
   before it shows.
5. **Record:** successful products are saved in `aliexpress_posted.json`, so a
   re-run doesn't create duplicates.
6. **Sync:**
   - `--resend` updates price and stock in batches of 20, using
     `batch.product.price.update` and `batch.product.inventory.update`.
   - `--offline-sold-out` takes products that are now sold out offline.
   - A product that comes back in stock is put online again by `--resend`.

Every request is signed as the platform's documentation requires. The
signature is an HMAC-SHA256 over the sorted parameters, keyed with your App
Secret, as uppercase hex. The implementation is checked against both worked
examples in AliExpress's documentation.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Seller account.** You need an AliExpress seller account for Spain (local
seller), with permission to list in a books category.

**2. Developer app.**
1. At <https://openservice.aliexpress.com>, register as a developer.
2. Create an application. AliExpress reviews and approves it.
3. Request the permissions for the product management APIs.
4. Note the app's **App Key** and **App Secret**.
5. Register a **callback URL** for the app. Any URL you control works,
   because you only need to copy the `code` from it.

**3. Seller Center setup.**
- At least one **shipping (freight) template**.
- An **EU responsible person** for GPSR. AliExpress rejects EU listings
  without one.
- Optionally, a **manufacturer**.

`check` shows all of these.

**4. Credentials.** Add them to Bitwarden as `ALIEXPRESS_APP_KEY` and
`ALIEXPRESS_APP_SECRET` (see [push_all.md](push_all.md#where-credentials-are-stored)),
or for a quick local test:

```powershell
$env:ALIEXPRESS_APP_KEY = "..."
$env:ALIEXPRESS_APP_SECRET = "..."
```

**5. Authorize the app for your shop.** This is done once, and again only if
the authorization expires or is revoked.

```powershell
python aliexpress_push.py auth-url --redirect https://your.callback/url
# open the printed link, log in with the SELLER account, authorize,
# then copy the code=... from the address you are sent to (valid 30 minutes)
python aliexpress_push.py token <code>
```

The tokens are saved in `aliexpress_token.json`. That file is in
`.gitignore` and never goes to GitHub. The access token is refreshed
automatically 30 minutes before it expires, and the new tokens are saved.

On a server, copy `refresh_token` from that file into Bitwarden as
`ALIEXPRESS_REFRESH_TOKEN`. The script uses it when there's no token file yet.

**6. Category for unknown ISBNs (optional).**
1. Run `categories --find libro` to find a leaf category for books.
2. Run `attributes <id>` to see its required attributes.
3. Set `CATEGORY_ID` and `EXTRA_ATTRIBUTES` at the top of the script, or pass
   `--category` and `--attribute ID=VALUE_ID`.

Without a category, only books that AliExpress's EAN database knows are pushed.

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `LANGUAGE` | Language of your titles and descriptions (`es`). |
| `CATEGORY_ID`, `EXTRA_ATTRIBUTES` | Category and attributes for books the EAN database doesn't know. `0` means those books are skipped. |
| `ALLOWED_CONDITIONS` | Source conditions that are pushed. Only `NewCondition` by default, because AliExpress listings are new goods. |
| `FREIGHT_TEMPLATE_ID` | Shipping template. `0` means your default template. |
| `MSR_EU_ID`, `MANUFACTURER_ID` | GPSR responsible person and manufacturer. `0` means the one set up in Seller Center. |
| `SHIPPING_LEAD_DAYS` | Days until you ship (`3`). |
| `DEFAULT_WEIGHT_KG`, `PACKAGE_CM` | Parcel weight and size, used when a product has no `weight_kg`. |

## Commands

| Command | What it does |
|---|---|
| `auth-url --redirect URL` | Prints the link the seller opens to authorize the app. |
| `token CODE` | Exchanges the authorization code for tokens and saves them. |
| `check` | Shows the shop (name, country, whether it may post), shipping templates, EU responsible persons and manufacturers. |
| `ean ISBN` | Shows whether AliExpress's EAN database knows a book, with its category and attributes. |
| `categories [--parent ID] [--find TEXT]` | Browses the categories your shop may list in. |
| `attributes CATEGORY_ID` | Lists a category's attributes. Required ones come first, with their value IDs. |
| `push` | Creates, updates or hides products (see below). |
| `status` | Lists your products as ONLINE, offline (with the reason), in review, or NEEDS EDITING. |

### `push`

```powershell
python aliexpress_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Stock per product. A product's own `quantity` takes precedence. |
| `--price-factor` | `1.0` | Multiplies every source price, rounded half-up to cents. See the [fixed book price](#limitations). |
| `--category` | `CATEGORY_ID` | Category for books the EAN database doesn't know. |
| `--attribute ID=VALUE_ID` | none | Extra category attribute. Repeat it for several. Added to `EXTRA_ATTRIBUTES`. |
| `--freight-template` | default template | Shipping template ID. |
| `--lead-days` | `3` | Days until you ship. |
| `--resend` | off | Updates price and stock of books that were already pushed. |
| `--offline-sold-out` | off | Takes pushed books that are now `sold_out` offline. |
| `--no-ean-lookup` | off | Doesn't use the EAN database. Every book then needs `--category` and your own photos. |
| `--no-online` | off | Creates the products but leaves them offline, so you can check them in Seller Center first. |
| `--dry-run` | off | Writes `aliexpress_products.json` and sends nothing. Without credentials, the lookups are skipped too. |
| `--limit N` | `0` (all) | Creates at most N new products. |
| `--yes` | off | Skips the confirmation prompt. |

Use this order for your first run:

```powershell
python aliexpress_push.py check                 # shop, shipping template, GPSR person OK?
python aliexpress_push.py ean 9788409591923     # does AliExpress know your books?
python aliexpress_push.py push --dry-run        # look at aliexpress_products.json
python aliexpress_push.py push --limit 1 --no-online   # one product, check it in Seller Center
python aliexpress_push.py status
python aliexpress_push.py push                  # the rest
python aliexpress_push.py push --resend --offline-sold-out   # later: sync price/stock, hide sold-out
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Used for |
|---|---|
| `id` or `sku` | `sku_code`, your unique SKU. **Required.** |
| `isbn` or `ean` | EAN lookup and `ean_code`. **Required.** ISBN-10/13 or EAN; hyphens are removed. |
| `title` | Product title (at most 218 characters). **Required.** |
| `description` | Product description. Each line becomes a paragraph, and the ISBN is added. |
| `price_eur` or `price` | SKU price in EUR, rounded half-up after `--price-factor`. |
| `images` or `image` | Up to 6 photos. Only used when AliExpress doesn't know the ISBN. |
| `condition` | Must be in `ALLOWED_CONDITIONS`. |
| `quantity`, `weight_kg` | Optional per-product stock and parcel weight. |
| `sold_out` | `true` means the product is skipped, or taken offline with `--offline-sold-out`. |

## What is sent to AliExpress

`aliexpress.solution.product.post` gets this, as the `post_product_request`
parameter:

```json
{
  "language": "es",
  "aliexpress_category_id": 200001,
  "multi_language_subject_list": [{"language": "es", "subject": "Dragona. Libro para bebés con rimas"}],
  "multi_language_description_list": [{"language": "es", "web_detail": {"version": "2.0.0",
     "moduleList": [{"type": "html", "html": {"content": "<p>Dragona...</p><p>ISBN: 9788409591923</p>"}}]}}],
  "main_image_urls_list": ["https://ae01.alicdn.com/kf/..."],
  "attribute_list": [{"aliexpress_attribute_name_id": 2, "aliexpress_attribute_value_id": 201512802}],
  "sku_info_list": [{"sku_code": "142534", "price": "8.50", "inventory": 1, "ean_code": "9788409591923"}],
  "inventory_deduction_strategy": "payment_success_deduct",
  "freight_template_id": 716259613,
  "shipping_lead_time": 3,
  "weight": "0.400", "package_length": 24, "package_width": 17, "package_height": 3,
  "product_unit": 100000015,
  "msr_eu_id": 555
}
```

The last batch the script built is saved in `aliexpress_products.json`.

## Files the script creates

| File | Contents |
|---|---|
| `aliexpress_posted.json` | SKU mapped to `product_id`, `ean`, `price`, `inventory`, `category`, `status` and times. Delete an entry to create that book again. |
| `aliexpress_products.json` | The last set of products that was built or sent. |
| `aliexpress_token.json` | Access and refresh tokens. **Secret**, not in git. |
| `aliexpress_images.json` | Your photo URLs mapped to their photo bank URLs, so photos aren't uploaded twice. |

## Errors and troubleshooting

AliExpress errors have a code (often `isv.…`) and a message. The script
prints both, for example `! 142534: isv.707010019: The required attribute value … cannot be empty`.

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | App Key or Secret isn't set | Set `ALIEXPRESS_APP_KEY` and `ALIEXPRESS_APP_SECRET`. |
| `No AliExpress authorization yet` | The app was never authorized | Run `auth-url`, then `token <code>`. |
| `IncompleteSignature` | Wrong App Secret | Copy the secret from the app console again. |
| `Authorize again` | The token expired or was revoked | Run `auth-url`, then `token <code>`. |
| `not in AliExpress's EAN database and no CATEGORY_ID set` | AliExpress doesn't know that ISBN | Set `CATEGORY_ID` (see `categories`), or skip those books. |
| `isv.707010019` (required attribute) | The category needs an attribute you didn't send | Run `attributes <category>`, then add it with `--attribute` or `EXTRA_ATTRIBUTES`. |
| `isv.-99999` (GPSR) | No EU responsible person | Add one in Seller Center (`check` lists it). |
| `isv.F00-00-10006-…` (main image) | Image not hosted by AliExpress | Happens if photo upload failed. Run again. |
| `isv.7150008` | Product limit of your seller level reached | Ask AliExpress, or list fewer books. |
| `status` shows `NEEDS EDITING` or `offline` with a reason | AliExpress's review didn't pass | Fix the product in Seller Center. |
| `(API rate limit, retrying …)` | Too many calls | Retried automatically. |

## Limitations

- **New books only.** AliExpress listings are new goods, so used books are
  skipped by default (see `ALLOWED_CONDITIONS`).
- **Spanish fixed book price.** New books have a price fixed by law
  (Ley 10/2007, at most 5% discount). Don't use `--price-factor` to go above
  the cover price. AliExpress may also show its own promotions on top.
- **One price for every country.** AliExpress supports per-country prices,
  but they aren't implemented.
- **Unknown ISBNs need a category.** All of them share `CATEGORY_ID` and
  `EXTRA_ATTRIBUTES`. If they belong in different categories, run them
  separately with `--category`.
- **Not verified:**
  - AliExpress's documentation doesn't say which countries the EAN lookup
    works for. `ean <isbn>` will show it on the first real run.
  - Two field names come from the field reference, where AliExpress's own
    sample uses different spellings: the inventory deduction value and the
    attribute ID names.
- **The script hasn't been run against the real AliExpress API yet.** Start with
  `check`, `ean`, `--dry-run` and `--limit 1 --no-online`.

## Testing

```powershell
python tests\test_aliexpress.py
```

[tests/test_aliexpress.py](../tests/test_aliexpress.py) checks the signature
against both worked examples in AliExpress's documentation. It then starts a
fake AliExpress Open Platform on `127.0.0.1:8765`.

The fake recomputes the signature of every request, rejects expired or
unknown tokens, and implements every call the script uses. It applies the
documented rules:
- required fields and leaf categories
- required category attributes
- photo-bank-only images
- the GPSR responsible person
- unique SKUs

It answers one call with a rate-limit error, to test the retry.

The test checks the following:
- credentials and authorization errors are reported
- the token exchange, automatic refresh, and `ALIEXPRESS_REFRESH_TOKEN`
- `check`, `ean`, `categories` and `attributes`
- the dry run, with and without credentials
- products created from EAN data and put online
- the fallback with photo upload, which happens only once per photo
- a missing attribute is rejected and not recorded
- `--resend` with half-up rounding
- `--offline-sold-out`, and back online on a later `--resend`
- `status`
- a missing GPSR responsible person

Your real `aliexpress_posted.json` and `aliexpress_token.json` are never
touched, because the test runs in a temporary folder.
