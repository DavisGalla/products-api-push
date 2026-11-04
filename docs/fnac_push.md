# fnac_push.py reference

This script pushes products to the **Fnac marketplace** (Fnac.es, Fnac.com,
Fnac.pt and so on) through the **Fnac Marketplace seller API v2.6**. That API
is a set of XML web services: `auth`, `offers_update`, `batch_status` and
`offers_query`. Everything below describes how the script behaves today.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to Fnac](#what-is-sent-to-fnac)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

Fnac manages its product catalog itself. Fnac now runs its catalog tooling on
Mirakl, through its "Catalog Portal". **Offers** (your price, stock and
condition on an existing product) go through Fnac's own offers API, and each
offer is matched to a product by **EAN**. An ISBN-13 is an EAN, and ISBN-10s
are converted automatically.

1. **Log in:** `auth` with your partner ID, shop ID and key returns a session token.
2. **Send:** `offers_update` sends the offers, at most 500 per call. Fnac
   answers with a **batch ID**.
3. **Wait:** the script polls `batch_status` until the batch is finished
   (`OK`, `WARNING`, `ERROR` or `FATAL`).
4. **Report:** the result for each offer is printed. Offers for products that
   aren't in Fnac's catalog are rejected by Fnac and shown with Fnac's message.
5. **Record:** only accepted offers (`OK` or `WARNING`) are recorded in `fnac_posted.json`.

Fnac shows its own title, cover and description. Your scraped text and photos
aren't sent.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**Seller account and API access.** Fnac gives approved marketplace sellers
three credentials: a **partner ID**, a **shop ID** and a **key**. All three are
UUIDs. You also need the **API URL** for your country's platform. Request API
access from Fnac Marketplace seller support, or look in your seller account or
Catalog Portal. If a key is renewed there, update it here as well.

```powershell
$env:FNAC_API_URL    = "https://vendeur.fnac.com/api.php"   # use the exact URL Fnac gives you
$env:FNAC_PARTNER_ID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
$env:FNAC_SHOP_ID    = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
$env:FNAC_KEY        = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Each Fnac country platform is a separate shop with its own credentials. To
push to several countries, run the script once per country with that
country's values.

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `PRODUCT_STATES` | Your condition mapped to Fnac's `product_state` code (see the table below). |
| `DEFAULT_COMMENT` | The offer comment buyers see, for example `"Envío en 24h"` (at most 255 characters). |
| `BATCH_SIZE` | Offers per `offers_update` call (500). |

| Fnac `product_state` | Meaning | Default mapping |
|---|---|---|
| `11` | New | `NewCondition` |
| `1` | Used, like new | `RefurbishedCondition` |
| `2` | Used, very good | `UsedCondition` |
| `3` | Used, good | — |
| `4` | Used, acceptable | `DamagedCondition` |
| `5`–`8` | The same four grades for collectible ("uncommon") items | — |

## Commands

| Command | What it does |
|---|---|
| `check` | Logs in, and prints the API version and your shop. |
| `push` | Sends offers (see below). |
| `status` | Lists your offers on Fnac (`offers_query`), showing price, stock and state. An offer with stock above 0 is shown as `LIVE`. |

Every command accepts `--sandbox`, which uses Fnac's test platform
(`marketplace.ws.fd-recette.net`). That only works with **test** credentials
from Fnac.

### `push`

```powershell
python fnac_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Stock per offer (at most 9999). A product's own `quantity` takes precedence. |
| `--comment` | `DEFAULT_COMMENT` | The offer comment buyers see. |
| `--time-to-ship` | Fnac default | Days until you ship. |
| `--price-factor` | `1.0` | Multiplies every source price. See the [fixed book price](#limitations). |
| `--resend` | off | Also re-sends offers that were already pushed. Use this to update price or stock. |
| `--dry-run` | off | Writes `fnac_offers.xml` and sends nothing. No credentials are needed. |
| `--limit N` | `0` (all) | Sends at most N offers. |
| `--yes` | off | Skips the confirmation prompt. |
| `--wait` / `--poll` | `900` / `15` | Maximum seconds to wait for each batch, and seconds between status checks. |

Use this order for your first run:

```powershell
python fnac_push.py push --dry-run        # look at fnac_offers.xml
python fnac_push.py check                 # credentials work?
python fnac_push.py push --limit 1        # one real offer
python fnac_push.py status                # is it LIVE?
python fnac_push.py push                  # the rest
```

To update prices or stock later, change your JSON and run `push --resend`.

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Purpose |
|---|---|
| `id` or `sku` | Your offer SKU (`offer_reference type="SellerSku"`). |
| `isbn` or `ean` | **Required.** ISBN-10, ISBN-13 or EAN. Hyphens and spaces are removed, and ISBN-10 is converted to EAN-13. |
| `price_eur` or `price` | Price, including VAT. Fnac accepts 0.90 to 20000. |
| `condition` | Looked up in `PRODUCT_STATES`. Defaults to `NewCondition`. |
| `quantity` | Optional per-product stock. |
| `sold_out` | `true` means the product is skipped. |
| `title` | Sent as the internal comment (only you see it), and used in log messages. |

A product is skipped before sending if it has no SKU, has no price or a price
outside 0.90–20000, has no ISBN/EAN or an invalid one, has an unmapped
condition, or is sold out.

## What is sent to Fnac

```
POST {FNAC_API_URL}/offers_update
```

```xml
<?xml version="1.0" encoding="utf-8"?>
<offers_update xmlns="http://www.fnac.com/schemas/mp-dialog.xsd"
               partner_id="…" shop_id="…" token="…">
  <offer>
    <product_reference type="Ean">9788409591923</product_reference>
    <offer_reference type="SellerSku">142534</offer_reference>
    <price>8.50</price>
    <product_state>11</product_state>
    <quantity>1</quantity>
    <description>Envío en 24h</description>
    <internal_comment>Dragona. Libro para bebés con rimas</internal_comment>
  </offer>
</offers_update>
```

The `description` element is only included when you set a comment.
`time_to_ship` is only included with `--time-to-ship`. `fnac_offers.xml`
always holds the last set of offers that was built, so you can check exactly
what was sent.

## Files the script creates

| File | Contents |
|---|---|
| `fnac_posted.json` | SKU mapped to `ean`, `batch_id`, `offer_fnac_id` and `pushed_at`. Delete an entry to push that offer again, or use `--resend`. |
| `fnac_offers.xml` | The last offers that were built or sent. |

## Errors and troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | Credentials aren't set | Set `FNAC_API_URL`, `FNAC_PARTNER_ID`, `FNAC_SHOP_ID` and `FNAC_KEY`. |
| `Fnac login failed` | A credential is wrong or was renewed, or the URL belongs to another country's platform | Check all four values in your seller account or Catalog Portal. |
| `cannot reach …` / `not XML` | Wrong `FNAC_API_URL` | Use the exact URL Fnac gave you. It ends in `api.php`. |
| `! SKU: … Product not found …` | Fnac doesn't have that EAN in its catalog | You can't list it with an offer. The message wording comes from Fnac. |
| `! SKU: … price …` | The price is rejected, for example below Fnac's minimum or a price-law problem | Fix the price in your JSON. |
| `still RUNNING after 900s` | Fnac is slow processing the batch | Run `push` again later. Nothing was recorded for that batch, and re-sending is safe. |
| `status` shows `out of stock` | The offer exists, but its quantity is 0 | Send a quantity with `--resend`. |

## Limitations

- **Offers only.** The script can't create new products in Fnac's catalog.
- **One country per run.** Each Fnac platform has its own shop and credentials.
- **Fixed book prices.** New books in Spain and France have prices fixed by
  law (at most 5% discount). Don't use `--price-factor` to go above the cover
  price.
- **No catalog check before sending.** Fnac reports unknown EANs in the batch
  result, and they're shown as rejected.
- **The script hasn't been run against the real Fnac API yet.** It was built
  from the v2.6 XML schemas. Start with `check`, `--dry-run` and `--limit 1`.

## Testing

```powershell
python tests\test_fnac.py
```

[tests/test_fnac.py](../tests/test_fnac.py) starts a fake Fnac API on
`127.0.0.1:8765`. The fake API checks the following on every request:

- the `mp-dialog` XML namespace and root element
- the partner ID, shop ID and token
- every offer field: types, EAN in the catalog, price range, state code, quantity and lengths

It also reports a batch as `RUNNING` once, to test the polling. The test checks
the following:

- missing or wrong credentials are reported
- `check` works
- a dry run works without credentials
- the XML content and condition codes are correct
- products are skipped for the right reasons
- offers for products Fnac has are accepted
- offers for products Fnac doesn't have are rejected and not recorded
- a re-run retries only the rejected offers
- `--resend` updates price and stock
- `status` lists the offers

Your real `fnac_posted.json` is never touched, because the test runs in a
temporary folder.
