# temu_push.py reference

This script pushes books to **Temu** as a **local seller in Europe** (for
example temu.com/es). It uses Temu's Open Platform "Partner API": the
`bg.local.goods.*` calls on the EU gateway
`https://openapi-b-eu.temu.com/openapi/router`. Everything below describes how
the script behaves today.

Like AliExpress, Temu has no catalog to attach an offer to, so **every book
becomes its own product**, with a name, description, photos, category
attributes, a variation, a shipping template, a weight and a size. Temu then
**reviews each new product and its price** before it goes on sale.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to Temu](#what-is-sent-to-temu)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Category plan:** all books go into one Temu leaf category
   (`CATEGORY_ID`). The script reads that category's template from Temu
   (`bg.local.goods.template.get`):
   - A required **ISBN** attribute is filled with each book's ISBN automatically.
   - Other required attributes, such as language, come from `PROPERTIES`.
     `attributes` prints them ready to paste.
   - Every Temu SKU needs a **variation**. If the category allows custom
     variations, the script creates one named `SPEC_NAME` (`Standard`) once,
     with `bg.local.goods.spec.id.get`, and caches it.

   Anything still missing stops the run before a single product is sent.
2. **Photos:** each photo is uploaded to Temu by URL
   (`bg.local.goods.image.upload`), because Temu only accepts its own image
   links. Uploaded photos are cached.
3. **Create:** the script sends one `bg.local.goods.add` per book. It sends
   your product ID as `outGoodsSn` and `outSkuSn`, and the ISBN as the external
   product ID (`externalProductType` 3 = ISBN). The price is in EUR.
4. **Record:** successful products are saved in `temu_posted.json` with
   Temu's `goodsId` and `skuId`.
5. **Sync:**
   - `--resend` sets the stock (`bg.local.goods.stock.edit`, full update).
   - If the price changed, it sends a price change
     (`bg.local.goods.priceorder.change.sku.price`), which Temu reviews.
   - `--offline-sold-out` takes sold-out books off sale
     (`bg.local.goods.sale.status.set`).
   - A book that comes back in stock is put on sale again by `--resend`.

Every request is signed as Temu documents it. The signature is an MD5 over
`app_secret`, the sorted parameters (nested values as compact JSON), and
`app_secret` again, as uppercase hex. The implementation is checked against
the worked example in Temu's documentation.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Seller account.** You need a Temu **local seller** account for Spain,
from the EU Seller Center at seller-eu.temu.com.

**2. Self-developed app.** At <https://partner-eu.temu.com>:
1. Create a Partner Platform account, and choose **"Self-developed
   applications seller"**. The partner type can't be changed later.
2. Link your Seller Center account. It must be the primary account, and it
   can only have one self-developed app.
3. Fill in the app information and the compliance and security
   questionnaires.
4. Wait for Temu to approve the app.
5. Note the app's **App Key** and **App Secret**.
6. Register the **public IP address** of the computer or server that runs
   the script. Calls from other IPs are rejected.

**3. Authorize the app.**
1. In Seller Center (seller-eu.temu.com), go to **Open platform →
   Authorization management → Authorize a new app**.
2. Choose the app and grant the product permissions.
3. Copy the **Access Token**. The authorization list shows when it expires.

**4. Shipping template.** Create at least one shipping template in Seller
Center. `check` lists them.

**5. Credentials.** Add them to Bitwarden as `TEMU_APP_KEY`,
`TEMU_APP_SECRET` and `TEMU_ACCESS_TOKEN` (see
[push_all.md](push_all.md#where-credentials-are-stored)), or for a quick local
test:

```powershell
$env:TEMU_APP_KEY = "..."
$env:TEMU_APP_SECRET = "..."
$env:TEMU_ACCESS_TOKEN = "..."
```

**6. Category.**
1. Run `categories --find book`, then `categories --parent <id>` until you
   reach a **leaf** category that isn't marked `[not sellable]`. Or ask Temu
   with `recommend "<a book title>"`.
2. Run `attributes <id>` and paste the lines for its required attributes into
   `PROPERTIES`.
3. Set `CATEGORY_ID`.

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `REGION`, `CURRENCY` | `eu` and `EUR` for Spain. `us` and `global` are the other Temu gateways. |
| `LANGUAGE` | Language of category and attribute names in `categories` and `attributes`. |
| `CATEGORY_ID` | Temu leaf category for all books. Required for real runs. |
| `PROPERTIES` | Required attributes that can't be filled automatically (pasted from `attributes`). |
| `SPEC_IDS`, `SPEC_NAME` | Variation. Empty means automatic: a custom value named `SPEC_NAME` is created. |
| `ALLOWED_CONDITIONS` | Source conditions that are pushed. Only `NewCondition` by default. |
| `FREIGHT_TEMPLATE_ID` | Shipping template. Empty means your first template. |
| `SHIPMENT_LIMIT_DAYS` | Days from order to shipment (`2`). |
| `DEFAULT_WEIGHT_G`, `PACKAGE_CM` | Parcel weight (g) and size (cm), used when a product has no `weight_kg`. |

## Commands

| Command | What it does |
|---|---|
| `check` | Tests the keys and token. Shows the shop (`mallId`), when the token expires, missing API permissions and the shipping templates. |
| `categories [--parent ID] [--find TEXT]` | Browses Temu's categories. Marks leaf and unsellable ones. |
| `recommend "TITLE"` | Shows Temu's suggested category for a title. |
| `attributes CATEGORY_ID` | Shows the category's attributes (required first), as ready-to-paste `PROPERTIES` lines, and how the variation is handled. |
| `push` | Creates, updates or hides products (see below). |
| `status` | Shows each product's Temu status: ON SALE, price review, in review, PRICE REJECTED, NEEDS COMPLETING, off sale or sold out. |

### `push`

```powershell
python temu_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Stock per product. A product's own `quantity` takes precedence. |
| `--price-factor` | `1.0` | Multiplies every source price, rounded half-up to cents. See the [fixed book price](#limitations). |
| `--category` | `CATEGORY_ID` | Temu leaf category. |
| `--freight-template` | first template | Shipping template ID. |
| `--shipment-days` | `2` | Days from order to shipment. |
| `--resend` | off | Updates stock and, if it changed, the price of books that were already pushed. |
| `--offline-sold-out` | off | Takes pushed books that are now `sold_out` off sale. |
| `--dry-run` | off | Writes `temu_products.json` and sends nothing. Without credentials or `CATEGORY_ID`, placeholders are used. |
| `--limit N` | `0` (all) | Creates at most N new products. |
| `--yes` | off | Skips the confirmation prompt. |

Use this order for your first run:

```powershell
python temu_push.py check                       # token, permissions, shipping template
python temu_push.py categories --find book      # then --parent <id> down to a leaf
python temu_push.py attributes <id>             # fill PROPERTIES, set CATEGORY_ID
python temu_push.py push --dry-run              # look at temu_products.json
python temu_push.py push --limit 1              # one real product
python temu_push.py status                      # price review / review / on sale?
python temu_push.py push                        # the rest
python temu_push.py push --resend --offline-sold-out   # later: sync stock/price, hide sold-out
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Used for |
|---|---|
| `id` or `sku` | `outGoodsSn` and `outSkuSn`, your unique codes. **Required.** |
| `isbn` or `ean` | `externalProductId` (type ISBN) and the ISBN attribute. **Required.** ISBN-10/13 or EAN. |
| `title` | Product name. Characters Temu rejects (`~ ! * $ ? _ { } # < > \| ; ^ ® © ™`) are removed. **Required.** |
| `description` | Plain-text description (at most 2000 characters), with the ISBN added. |
| `price_eur` or `price` | Base price in EUR, rounded half-up after `--price-factor`. |
| `images` or `image` | 1 to 10 photos. **Required.** |
| `condition` | Must be in `ALLOWED_CONDITIONS`. |
| `quantity`, `weight_kg` | Optional per-product stock and parcel weight. |
| `sold_out` | `true` means the product is skipped, or taken off sale with `--offline-sold-out`. |

## What is sent to Temu

`bg.local.goods.add` gets this, plus the signed common parameters:

```json
{
  "goodsBasic": {"goodsName": "Dragona. Libro para bebés con rimas", "catId": 44936, "outGoodsSn": "142534"},
  "goodsServicePromise": {"shipmentLimitDay": 2, "fulfillmentType": 1, "costTemplateId": "LFT-..."},
  "goodsProperty": {"goodsProperties": [
    {"pid": 501, "templatePid": 9001, "refPid": 77, "value": "9788409591923"},
    {"pid": 12, "templatePid": 9002, "refPid": 12, "vid": 381, "value": "Español"}]},
  "goodsDesc": "Dragona. Libro para bebés con rimas...\nISBN: 9788409591923",
  "skuList": [{
    "outSkuSn": "142534", "specIdList": [266386378],
    "price": {"listPriceType": 1, "basePrice": {"amount": "8.50", "currency": "EUR"}},
    "quantity": 1, "weight": "400.0", "weightUnit": "g",
    "length": "24.0", "width": "17.0", "height": "3.0", "volumeUnit": "cm",
    "images": ["https://img.kwcdn.com/local-image/..."],
    "externalProductType": 3, "externalProductId": "9788409591923"
  }]
}
```

The IDs in `goodsProperty` and `specIdList` come from your category's
template. The last batch the script built is saved in `temu_products.json`.

## Files the script creates

| File | Contents |
|---|---|
| `temu_posted.json` | SKU mapped to `goods_id`, `sku_id`, `ean`, `price`, `quantity`, `category`, `on_sale` and times. Delete an entry to create that book again. |
| `temu_products.json` | The last set of products that was built. |
| `temu_cache.json` | Uploaded photo URLs and the generated variation ID, so they aren't requested twice. |

## Errors and troubleshooting

Temu errors have a numeric code and a message. The script prints both, for
example `! 142534: 150011066 The input image is incorrect, the aspect ratio is not 1:1.`

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | Keys or token not set | Set `TEMU_APP_KEY`, `TEMU_APP_SECRET` and `TEMU_ACCESS_TOKEN`. |
| `Check TEMU_APP_SECRET` (3000001) | Wrong App Secret | Copy it from partner-eu.temu.com again. |
| `Authorize the app again` (3000031/3000034) | Token wrong, expired or replaced | Authorize again in Seller Center and update `TEMU_ACCESS_TOKEN`. |
| `Add this computer's public IP address` (5000003) | IP not on the app's whitelist | Add it in partner-eu.temu.com. |
| `CATEGORY_ID is not set` | No category yet | See `categories`, `recommend` and `attributes`. |
| `Category … needs: …` | Required attribute or variation missing | Run `attributes <id>`, then fill in `PROPERTIES` or `SPEC_IDS`. |
| `150011066` / `150011065` (image ratio or size) | Photo isn't square or is under 800×800 px | Use square photos of at least 800 px. Temu requires this for non-clothing products. |
| `150010011` (product name) | Characters Temu doesn't allow | Simplify the title in your JSON. |
| `150011010` (keyword attribute required) | Category needs another attribute | Add it to `PROPERTIES`. |
| `status` shows `PRICE REJECTED` or `price review` | Temu is reviewing or rejected the price | Check the price task in Seller Center. Respect the fixed book price. |
| `status` shows `NEEDS COMPLETING` / `needs more information` | Compliance or GPSR information missing | Complete it in Seller Center. |
| `(Temu: RATE_LIMIT_EXCEED_EXCEPTION, retrying …)` | Rate limit (20 calls per second) | Retried automatically. |

## Limitations

- **Photos must be square.** Temu requires 1:1 photos of at least 800×800 px
  for non-clothing products. Book covers are usually portrait, so they may be
  rejected (error `150011066`) until the scraper delivers square images, for
  example a cover on a white background.
- **One category for all books**, with one set of `PROPERTIES`. If books
  belong in different categories, run them separately with `--category`.
- **New books only.** Used books are only allowed in shops that Temu has
  approved as second-hand (`ALLOWED_CONDITIONS`).
- **Spanish fixed book price.** New books have a price fixed by law
  (Ley 10/2007, at most 5% discount). Temu reviews prices and may propose a
  lower one. Don't accept a price below the legal minimum.
- **Compliance information** (GPSR responsible person, origin) isn't sent.
  If Temu asks for it, `status` shows "needs more information". Complete it
  in Seller Center.
- **Not verified:**
  - Temu's documentation needs a seller login. This script follows a public
    daily mirror of it ([opastorello/temu-api-docs](https://github.com/opastorello/temu-api-docs)),
    whose API reference lists the fields but doesn't describe them. Field
    meanings come from Temu's integration guides and examples.
  - Whether Spanish accents are accepted in product names. The guide says
    "English letters, numbers and symbols", but Spanish is a supported
    language on the Spanish site.
- **The script hasn't been run against the real Temu API yet.** Start with
  `check`, `attributes`, `--dry-run` and `--limit 1`.

## Testing

```powershell
python tests\test_temu.py
```

[tests/test_temu.py](../tests/test_temu.py) checks the signature against the
worked example in Temu's documentation. It then starts a fake Temu router on
`127.0.0.1:8765`.

The fake recomputes the signature of every request, checks the app key,
token, timestamp and `data_type`, and implements every call the script uses.
It applies the documented rules:
- required attributes and a variation on every SKU
- leaf, sellable categories and existing shipping templates
- a price with at most 2 decimals, in EUR
- Temu-hosted images (one cover is rejected for its aspect ratio)
- the ISBN code type and unique SKUs

It answers one call with a rate-limit error, to test the retry.

The test checks the following:
- credentials, secret and token errors are reported
- `check`, `categories`, `recommend` and `attributes`
- the dry run, with placeholders
- a missing category or attribute stops the run before anything is sent
- products are created with the ISBN attribute and a generated variation
- photos are uploaded and cached
- an image rejection is shown and not recorded
- re-runs don't create duplicates
- `--resend` updates stock and price (half-up), and sends no price change when nothing changed
- `--offline-sold-out`, and back on sale on a later `--resend`
- `status`

Your real `temu_posted.json` is never touched, because the test runs in a
temporary folder.
