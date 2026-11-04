# vinted_api_push.py reference

This script pushes products to Vinted through the Vinted Pro Integrations API
(<https://pro-docs.svc.vinted.com>). Everything below describes how it behaves
today.

- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [How products are converted](#how-products-are-converted)
- [Files the script creates](#files-the-script-creates)
- [How authentication works](#how-authentication-works)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## Setup

**Requirements:** Python 3.8 or newer. The script uses only the standard
library, so there's nothing to install.

**Token.** Your Vinted Pro token is `ACCESS_KEY,SIGNING_KEY`. Production and
sandbox use separate tokens. The script reads the token from
`VINTED_PRO_TOKEN`, or from `--token` if you pass it.

```powershell
$env:VINTED_PRO_TOKEN = "access,signing"                                # current session
[Environment]::SetEnvironmentVariable("VINTED_PRO_TOKEN", "access,signing", "User")  # permanent
```

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. Use `r"..."` for Windows paths. |
| `CONDITION_CANDIDATES` | Maps your source condition to Vinted condition names (see [Condition](#condition)). |
| `STATE_FILE` | Where pushed items are recorded (`posted_api.json`). |

## Commands

The general form is `python vinted_api_push.py <command> [options]`. Every
command also accepts these options:

| Option | Meaning |
|---|---|
| `--token ACCESS,SIGNING` | Overrides `VINTED_PRO_TOKEN`. |
| `--sandbox` | Uses `https://pro-public-sandbox.svc.vinted.com` instead of production. |

### `ontologies`

Downloads Vinted's reference data, saves it to `.vinted_ontologies.json`, and
prints:

- every **leaf catalog** (only these are valid for `catalog_id`), with its full path
- **package sizes** and their IDs
- the **condition** attribute and its option IDs
- validation rules (maximum photos, minimum and maximum price)

```powershell
python vinted_api_push.py ontologies
python vinted_api_push.py ontologies --find libros   # filter catalogs by path text
```

Run this again whenever Vinted changes its categories. `push` reuses the cached
file and doesn't download it again.

### `push`

Reads the products file, converts each product, validates it with Vinted, and
creates the valid items.

```powershell
python vinted_api_push.py push [json_file] --catalog-id 2320 --package-size-id 1 [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--catalog-id` | — | Leaf catalog for every item. A product's own `catalog_id` takes precedence. |
| `--package-size-id` | — | Package size for every item. A product's own `package_size_id` takes precedence. |
| `--currency EUR\|GBP` | From ontologies | Listing currency. |
| `--default-brand` | `Otros` | Used when a product has no brand. |
| `--default-condition` | `NewCondition` | Used when a product has no condition. |
| `--price-factor` | `1.0` | Multiplies every source price. For example, `1.2` adds 20%. |
| `--draft` | off | Creates drafts instead of live listings. |
| `--validate-only` | off | Asks Vinted to validate the items and creates nothing. |
| `--limit N` | `0` (all) | Pushes at most N items in this run. |

What happens during a push:

1. Products that are already in `posted_api.json` are skipped.
2. Each product is checked locally. A product is skipped, with the reason
   printed, if it's sold out, has no public photo URL, has no price, has a title
   or description shorter than 5 characters, or is missing a catalog or package
   size.
3. Items go to `POST /api/v1/items/validate` in batches of 100. Invalid items
   are printed with Vinted's error messages and left out.
4. The valid items go to `POST /api/v1/items`. Each returned `item_id` is
   saved to `posted_api.json`.

> Getting an `item_id` back doesn't mean the item is listed yet. Vinted
> processes items in the background, so run `status` afterwards to check.

### `status`

For every item in `posted_api.json`, this command calls
`GET /api/v1/items/{id}/status` and prints the status, the Vinted listing ID
and any errors. It also saves them back to the file.

```powershell
python vinted_api_push.py status
```

## Input JSON format

The file can be a list of products or `{"products": [...]}`. The script
recognises two product formats.

### Scraper format

The script uses this format when a product has a `price_eur` field.

```json
{
  "id": "142534",
  "title": "Dragona. Libro para bebés con rimas",
  "price_eur": 8.5,
  "brand": "Cuentos con Valores",
  "isbn": "9788409591923",
  "condition": "NewCondition",
  "sold_out": false,
  "description": "Dragona. Libro para bebés con rimas...",
  "images": ["https://.../photo.jpg"]
}
```

Instead of `images` (a list), the product can have a single `image`. All other
fields are ignored.

### Simple format

The script uses this format when a product has `price` and no `price_eur`.

```json
{
  "sku": "S-1",
  "title": "Producto en formato simple",
  "description": "Artículo descrito en el formato simple.",
  "price": 5,
  "brand": "Otros",
  "image_urls": ["https://.../photo.jpg"]
}
```

`photo_urls` is accepted in place of `image_urls`.

### Optional per-product fields (both formats)

| Field | Purpose |
|---|---|
| `catalog_id`, `package_size_id` | Override the command-line values for this product. |
| `size_id`, `color_ids` | Needed for catalogs that have sizes or colours. Get the IDs from `.vinted_ontologies.json`. |
| `manufacturer`, `is_unisex` | Passed to Vinted unchanged. |
| `item_attributes` | Raw attributes, for example `[{"item_attribute_id": "condition", "item_attribute_option_ids": [1]}]`. |
| `condition` | Mapped as described under [Condition](#condition). |

## How products are converted

| Vinted field | Source | Rule |
|---|---|---|
| `item_reference` | `id` or `sku` (falls back to `title`) | Used to detect duplicates. |
| `title` | `title` | Cut to 100 characters. Must be at least 5. |
| `description` | `description` | `ISBN: …` is added if there's an ISBN. Cut to 2000 characters. |
| `price` | `price_eur` or `price` | Multiplied by `--price-factor` and rounded to 2 decimals. |
| `brand` | `brand` | Falls back to `--default-brand`. |
| `photo_urls` | `images`, `image` or `image_urls` | Only `http(s)` URLs are kept, up to the ontology's `max_photos`. |
| `isbn` | `isbn` | Sent unchanged. Only valid for book catalogs. |
| `is_draft` | `--draft` | |

### Condition

Vinted takes condition as the `condition` item attribute. The script finds that
attribute in the ontologies, then matches the product's condition against the
option names in `CONDITION_CANDIDATES`:

| Source value | Vinted option names it tries |
|---|---|
| `NewCondition` | Nuevo sin etiquetas, New without tags, Neuf sans étiquette, Neu ohne Etikett |
| `UsedCondition` | Muy bueno, Very good, Très bon état, Sehr gut |
| `RefurbishedCondition` | Bueno, Good, Bon état, Gut |

You can also put an exact Vinted option name in `condition`, such as
`"Satisfactorio"`. If nothing matches, the item is sent without a condition and
the script prints the available options.

## Files the script creates

These are created in the folder you run the script from.

| File | Contents |
|---|---|
| `posted_api.json` | `item_reference` → `item_id`, `pushed_at`, `status`, `marketplace_item_id`. Delete an entry to push that item again. |
| `.vinted_ontologies.json` | Cached ontologies. Delete it, or run `ontologies`, to refresh. |

## How authentication works

Every request carries two headers:

```
X-Vpi-Access-Key: <access key>
X-Vpi-Hmac-Sha256: t=<unix seconds>,v1=<hex HMAC-SHA256>
```

The HMAC is computed with the **signing key** over this string:

```
<timestamp>.<METHOD>.<path with query>.<access key>.<raw body>
```

Vinted rejects old timestamps, so your computer's clock must be correct.

## Errors and troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `Token missing or malformed` | No token, or no comma in it | Set `VINTED_PRO_TOKEN=access,signing`. |
| `Failed to fetch ontologies (401)` | Wrong token, wrong environment (sandbox vs production), or the clock is off | Check the token matches the environment (add `--sandbox` for a sandbox token) and sync the system clock. |
| `JSON file not found` | Wrong path | Fix `JSON_FILE` or the path you passed. |
| `no catalog_id` / `no package_size_id` | IDs weren't given | Pass `--catalog-id` / `--package-size-id`. |
| `invalid '…': catalog_id must be a leaf catalog` | A parent category was used | Pick an ID from the `ontologies` list. |
| `condition '…' not matched` | The option names differ in your country | Add your country's wording to `CONDITION_CANDIDATES`. |
| `Slot limit exceeded` (409) | Your account has no free listing slots | Nothing in that batch was created. Free up slots or push fewer items. |
| `status` shows errors | Vinted rejected the item after accepting it, for example because a photo URL can't be reached | Fix the product, delete its entry in `posted_api.json`, and push again. |

## Limitations

- **Photos must be public URLs.** The API has no upload endpoint, so local files can't be sent.
- **Creating items only.** Updating, deleting, orders and webhooks aren't implemented, although the API supports them.
- **Ontologies v1 is used.** Vinted asks integrators to stay on v1 until told to switch.
- **The script hasn't been run against the real API yet.** Follow the first-run checks in the [README](../README.md#first-run-with-a-real-token).

## Testing

```powershell
python tests\test_vinted_api.py
```

[tests/test_vinted_api.py](../tests/test_vinted_api.py) starts a fake Vinted
Pro API on `127.0.0.1:8765`. The fake API checks the HMAC signature and the
documented field rules. The test then runs the script against
[tests/test_products.json](../tests/test_products.json). It checks the
following:

- signed requests are accepted and a wrong key is rejected
- invalid products are skipped
- long titles and descriptions are truncated
- conditions are mapped correctly
- no duplicates are created on a re-run
- `status` updates
- Vinted's validation errors are shown

Your real `posted_api.json` is never touched, because the test runs in a
temporary folder.
