# leclerc_push.py reference

This script pushes products to the **E.Leclerc marketplace** (France, e.leclerc). The marketplace runs on **Mirakl**, so the script uses the
standard Mirakl seller API, the same one `casadellibro_push.py` and
`carrefour_push.py` and `ibs_push.py` use. Everything below describes how it behaves today.

E.Leclerc doesn't publish its seller API documentation, so the script follows
Mirakl's standard calls. Your shop's specifics (API URL, condition codes,
whether a logistic class is required) come from your seller back office via
`check` and `states`.

E.Leclerc is one of France's largest retailers, and it also runs bookshops
(Espaces Culturels). Its non-food marketplace is for professional sellers
based in, and shipping from, the euro zone, so a Spanish shop is eligible.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [The offer file](#the-offer-file)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

As a E.Leclerc marketplace seller, you add an **offer** (price, stock, condition) to an
existing product, matched by **EAN**. An ISBN-13 is an EAN, and ISBN-10s are
converted automatically.

1. **Catalog check:** the script looks up all the EANs with Mirakl API **P31**,
   at most 100 per request. Books that aren't in the catalog are skipped.
2. **Upload:** the script writes all the offers into a single CSV file and
   uploads it as an offer import with **OF01**, using `import_mode=NORMAL`.
   That mode only creates or updates the offers in the file and never deletes
   other offers.
3. **Wait:** the script polls **OF02** until the import is `COMPLETE`.
4. **Errors:** if any lines failed, the script downloads the error report
   (**OF03**) and prints E.Leclerc's reason for each one.
5. **Record:** only successful offers are recorded in `leclerc_posted.json`.

E.Leclerc shows its own title, cover and description. Your scraped text
and photos aren't sent.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**Seller account.** You need an approved seller account on the E.Leclerc
marketplace. E.Leclerc approves sellers itself, so contact the marketplace team
to apply (integrator guides give account.marketplace@e-leclerc.com). If your
back office has no API key option, ask E.Leclerc seller support to enable API
access.

**API key and URL.** In the seller back office (the Mirakl interface), open
your user menu and choose **API Key** (*Clé API*). Copy the key, and the API
base URL your shop uses, which looks like `https://<instance>.mirakl.net`. If
the URL isn't shown there, ask E.Leclerc seller support for it. A key from
another Mirakl marketplace (Carrefour, Fnac, ...) doesn't work here.

```powershell
$env:LECLERC_API_URL = "https://<instance>.mirakl.net"   # a trailing /api is also fine
$env:LECLERC_API_KEY = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
$env:LECLERC_SHOP_ID = "1234"   # optional, only if your key manages several shops
```

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `STATE_CODES` | Your condition mapped to E.Leclerc's offer state code. Only `NewCondition` → `11` (Mirakl's default "New") is set. Run `states` and add codes for used books if you sell them. |
| `PRODUCT_ID_TYPE` | How products are identified in the offer file (`EAN`). |
| `DEFAULT_LEADTIME_DAYS` | Days until you ship (`3`). |

## Commands

| Command | What it does |
|---|---|
| `check` | Tests the URL and key, and prints your shop name, ID and state (A01). |
| `states` | Lists the offer condition codes for `STATE_CODES`. |
| `push` | Uploads offers (see below). |
| `status` | Lists your offers on E.Leclerc (OF21), showing whether each is active, its price and its stock. |

### `push`

```powershell
python leclerc_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Stock per offer. A product's own `quantity` takes precedence. |
| `--leadtime` | `DEFAULT_LEADTIME_DAYS` | Days until you ship. |
| `--logistic-class` | empty | Logistic class code, if your shop's shipping setup requires one. |
| `--price-factor` | `1.0` | Multiplies every source price. See the [fixed book price](#limitations). |
| `--resend` | off | Also re-sends offers that were already pushed. Use this to update price or stock. |
| `--no-match-check` | off | Skips the catalog lookup. Unknown books are then rejected in the error report instead. |
| `--dry-run` | off | Writes `leclerc_offers.csv` and uploads nothing. |
| `--limit N` | `0` (all) | Uploads at most N offers. |
| `--yes` | off | Skips the confirmation prompt. |
| `--wait` / `--poll` | `600` / `15` | Maximum seconds to wait for the import, and seconds between status checks. |

Use this order for your first run:

```powershell
python leclerc_push.py check                 # key and URL work?
python leclerc_push.py push --dry-run        # look at leclerc_offers.csv
python leclerc_push.py push --limit 1        # one real offer
python leclerc_push.py status                # is it LIVE?
python leclerc_push.py push                  # the rest
```

To update prices or stock later, change your JSON and run `push --resend`.

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Purpose |
|---|---|
| `id` or `sku` | Your offer SKU (at most 40 characters). |
| `isbn` or `ean` | **Required.** ISBN-10, ISBN-13 or EAN. Hyphens and spaces are removed, and ISBN-10 is converted to EAN-13. |
| `price_eur` or `price` | Price in EUR, including VAT. Rounded half-up to cents after `--price-factor` (8.075 → 8.08). |
| `condition` | Looked up in `STATE_CODES`. Defaults to `NewCondition`. |
| `quantity` | Optional per-product stock. |
| `sold_out` | `true` means the product is skipped. |
| `title` | Stored as the internal description (only you see it), and used in log messages. |

A product is skipped if it has no SKU, no price, no ISBN/EAN or an invalid
one, if its condition has no state code, if it's sold out, or if it isn't in
the E.Leclerc catalog.

## The offer file

The offer file is a CSV separated by `;`, in the standard Mirakl OF01 format:

```
sku;product-id;product-id-type;description;internal-description;price;quantity;state;leadtime-to-ship;logistic-class;update-delete
142534;9788409591923;EAN;;Dragona. Libro para bebés con rimas;8.50;1;11;3;;update
```

Each run also saves the file it uploads as `leclerc_offers.csv`, so you can
check exactly what was sent.

## Files the script creates

| File | Contents |
|---|---|
| `leclerc_posted.json` | SKU mapped to `ean`, `import_id` and `pushed_at`. Delete an entry to push that offer again, or use `--resend`. |
| `leclerc_offers.csv` | The last offer file that was built or uploaded. |

## Errors and troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | The URL or key isn't set | Set `LECLERC_API_URL` and `LECLERC_API_KEY`. |
| `401 Unauthorized` | The key is wrong, or the URL belongs to a different instance | Copy the key and URL from the back office again. |
| `no state code for condition …` | That condition isn't in `STATE_CODES` | Run `states` and add the code. |
| `EAN … is not in the E.Leclerc catalog` | E.Leclerc doesn't sell that book | You can't list it with an offer. |
| `! SKU: …` after an import | E.Leclerc rejected that line | Read the message. Common causes are a price outside the allowed range, a missing logistic class, or a shop that isn't open. |
| `Still RUNNING after 600s` | The import is slow | Run `push` again later. Nothing was recorded, and re-uploading is safe. |
| `status` shows `inactive` | The offer exists but isn't sellable (for example, stock is 0 or the shop is paused) | Check it in the back office. |

## Limitations

- **Offers only.** The script can't create new products in E.Leclerc's catalog.
  E.Leclerc's marketplace catalog is mainly non-food general merchandise.
  `push --dry-run` shows which of your books it lists.
- **Fixed book prices.** New books in France have a price fixed by law
  (loi Lang: at most 5% below the publisher's price, including for sellers
  from other EU countries). Prices include French VAT (books: 5.5%).
  Don't use `--price-factor` to go above the cover price.
- **Shop-specific required fields.** If E.Leclerc requires extra offer fields
  for your shop, the import's error report names them. They would then need
  adding to `OFFER_COLUMNS` in the script.
- **Used conditions aren't set up.** Add their codes yourself after running `states`.
- **The API URL is shop-specific.** E.Leclerc doesn't publish it, so take
  it from your back office or ask seller support.
- **The script hasn't been run against the real E.Leclerc API yet.**
  Start with `check`, `--dry-run` and `--limit 1`.

## Testing

```powershell
python tests\test_leclerc.py
```

[tests/test_leclerc.py](../tests/test_leclerc.py) starts a fake Mirakl
server on `127.0.0.1:8765`. It mocks A01, offer states, P31, OF01, OF02, OF03
and OF21. It parses the uploaded multipart CSV, validates every line, reports
`RUNNING` once to test the polling, and returns an error report for rejected
lines. The test checks the following:

- missing or wrong credentials are reported
- `check` and `states` work
- the dry-run CSV has the right format
- products are skipped for the right reasons
- condition codes, EAN normalisation, price, quantity and lead time are written correctly
- an upload is polled until it completes
- nothing is uploaded twice on a re-run
- `--resend` updates price and stock, with half-up rounding
- errors from the report are shown, and rejected lines aren't recorded
- `status` reports the offers

Your real `leclerc_posted.json` is never touched, because the test runs in
a temporary folder.
