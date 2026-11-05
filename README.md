# Product pushers

Push a JSON file of products to marketplaces through their official APIs:

| Script | Marketplace | Auth | Docs |
|---|---|---|---|
| `vinted_api_push.py` | Vinted (Vinted Pro Integrations API) | API token | [docs/vinted_api_push.md](docs/vinted_api_push.md) |
| `ebay_push.py` | eBay EU (Sell Inventory API) | OAuth refresh token | [docs/ebay_push.md](docs/ebay_push.md) |
| `amazon_push.py` | Amazon EU: ES, DE, FR, IT, NL, BE, PL, SE, IE (+ UK) (Selling Partner API) | LWA refresh token | [docs/amazon_push.md](docs/amazon_push.md) |
| `casadellibro_push.py` | Casa del Libro marketplace (Mirakl seller API) | API key | [docs/casadellibro_push.md](docs/casadellibro_push.md) |
| `fnac_push.py` | Fnac marketplace: Fnac.es, Fnac.com... (Fnac Marketplace API v2.6) | partner ID + shop ID + key | [docs/fnac_push.md](docs/fnac_push.md) |
| `abebooks_push.py` | AbeBooks / IberLibro (AbeBooks Inventory Update API) | username + API key | [docs/abebooks_push.md](docs/abebooks_push.md) |
| `elcorteingles_push.py` | El Corte Inglés marketplace (Mirakl seller API) | API key | [docs/elcorteingles_push.md](docs/elcorteingles_push.md) |

All scripts read the same product format, which is the scraper output in
[products.example.json](products.example.json). They skip sold-out and
incomplete products, and they remember what they've already pushed so a re-run
doesn't create duplicates.

```powershell
pip install -r requirements.txt   # for every pusher except Vinted, which uses only the standard library
```

---

## Vinted quick start

**1. Set your token.** You get it from the Vinted Pro integrations settings.
It has the form `ACCESS_KEY,SIGNING_KEY`.

```powershell
$env:VINTED_PRO_TOKEN = "your_access_key,your_signing_key"
```

**2. Point the script at your products file.** Edit `JSON_FILE` at the top of
`vinted_api_push.py`, or pass the path on the command line.

```python
JSON_FILE = r"C:\programming\scraper\books.json"
```

**3. Look up your category and package size IDs.**

```powershell
python vinted_api_push.py ontologies --find libros
```

**4. Validate, then push.**

```powershell
python vinted_api_push.py push --catalog-id <id> --package-size-id <id> --validate-only
python vinted_api_push.py push --catalog-id <id> --package-size-id <id> --limit 1 --draft
python vinted_api_push.py push --catalog-id <id> --package-size-id <id>
```

**5. Check the results.** Vinted creates listings asynchronously, so check
back after a minute.

```powershell
python vinted_api_push.py status
```

---

## eBay quick start

**1. One-time setup.** Get a developer keyset and a seller refresh token, then
create your business policies and an inventory location in Seller Hub. The
steps are in [docs/ebay_push.md](docs/ebay_push.md#setup).

**2. Set your credentials.**

```powershell
$env:EBAY_CLIENT_ID     = "..."
$env:EBAY_CLIENT_SECRET = "..."
$env:EBAY_REFRESH_TOKEN = "v^1.1#..."
```

**3. Try it safely, then push.**

```powershell
python ebay_push.py books.json --dry-run
python ebay_push.py books.json --no-publish --limit 1   # created, not live
python ebay_push.py books.json --limit 1                # one live listing
python ebay_push.py books.json --market EBAY_ES         # everything
```

---

## Amazon EU quick start

Books are matched to existing Amazon pages by ISBN in each marketplace, and
your offer (price, condition, stock) is added to them. A book that a store
doesn't have is skipped in that store only. One refresh token covers all EU
stores. The steps for getting it are in
[docs/amazon_push.md](docs/amazon_push.md#setup).

Prices for SE (SEK), PL (PLN) and UK (GBP) are converted with `EUR_RATES` at
the top of the script, so update those rates first.

```powershell
$env:AMAZON_LWA_CLIENT_ID     = "amzn1.application-oa2-client...."
$env:AMAZON_LWA_CLIENT_SECRET = "..."
$env:AMAZON_REFRESH_TOKEN     = "Atzr|..."
$env:AMAZON_SELLER_ID         = "A1B2C3D4E5F6G7"

python amazon_push.py push --validate-only                  # Amazon.es check, nothing is listed
python amazon_push.py push --limit 1                        # one real offer on Amazon.es
python amazon_push.py push --markets ES,DE,FR               # chosen stores
python amazon_push.py push --markets all --validate-only    # all 9 EU stores, check only
python amazon_push.py push --markets all                    # all 9 EU stores
python amazon_push.py status
```

---

## Casa del Libro quick start

Casa del Libro's marketplace runs on Mirakl. Books are matched to Casa del
Libro's catalog by EAN/ISBN, and your offers (price, stock, condition) are
uploaded as a single import. Take the API key and your shop's API URL from the
seller back office (user menu → *API Key*).

```powershell
$env:CASADELLIBRO_API_URL = "https://<instance>.mirakl.net"
$env:CASADELLIBRO_API_KEY = "..."

python casadellibro_push.py check            # key and URL work?
python casadellibro_push.py push --dry-run   # writes casadellibro_offers.csv, uploads nothing
python casadellibro_push.py push --limit 1   # one real offer
python casadellibro_push.py status
python casadellibro_push.py push --resend    # later: re-send everything to sync price and stock
```

---

## Fnac quick start

Books are matched to Fnac's catalog by EAN/ISBN, and your offers (price,
stock, condition) are sent in batches. Fnac rejects offers for books it
doesn't have, and the script shows Fnac's message for each one. Ask Fnac
seller support (or check your seller account / Catalog Portal) for your
partner ID, shop ID, key and API URL. Each Fnac country is a separate shop.

```powershell
$env:FNAC_API_URL    = "https://vendeur.fnac.com/api.php"   # the URL Fnac gives you
$env:FNAC_PARTNER_ID = "..."
$env:FNAC_SHOP_ID    = "..."
$env:FNAC_KEY        = "..."

python fnac_push.py push --dry-run    # writes fnac_offers.xml, no credentials needed
python fnac_push.py check             # credentials work?
python fnac_push.py push --limit 1    # one real offer
python fnac_push.py status
python fnac_push.py push --resend     # later: re-send everything to sync price and stock
```

---

## AbeBooks / IberLibro quick start

AbeBooks listings describe your own copy of a book, so no catalog match is
needed, and books without an ISBN work too. IberLibro is AbeBooks' Spanish
site and shows the same inventory. **AbeBooks has no test environment**, so
start with `--dry-run` and `--limit 1`.

```powershell
$env:ABEBOOKS_USERNAME = "..."
$env:ABEBOOKS_API_KEY  = "..."

python abebooks_push.py push --dry-run                    # writes abebooks_request.xml (key hidden)
python abebooks_push.py push --limit 1                    # one real listing; check it on IberLibro
python abebooks_push.py push                              # the rest
python abebooks_push.py push --resend --delete-sold-out   # later: sync price and stock, remove sold-out books
python abebooks_push.py delete 142534                     # remove a listing
```

---

## El Corte Inglés quick start

El Corte Inglés's marketplace also runs on Mirakl, so this works like the Casa
del Libro pusher. Books are matched to El Corte Inglés's catalog by EAN/ISBN,
and your offers are uploaded as one import. Take the API key and your shop's
API URL from the seller back office (user menu → *API Key*).

```powershell
$env:ELCORTEINGLES_API_URL = "https://<instance>.mirakl.net"
$env:ELCORTEINGLES_API_KEY = "..."

python elcorteingles_push.py check            # key and URL work?
python elcorteingles_push.py states           # condition codes your shop uses
python elcorteingles_push.py push --dry-run   # writes elcorteingles_offers.csv, uploads nothing
python elcorteingles_push.py push --limit 1   # one real offer
python elcorteingles_push.py status
python elcorteingles_push.py push --resend    # later: re-send everything to sync price and stock
```

---

## First run with real credentials

All seven scripts have only been tested against local mocks of the APIs. Before you
push your whole catalogue, try one item at a time:

- **Vinted:**
  1. `ontologies` lists categories, which confirms your token works.
  2. `push --validate-only` reports all items as valid.
  3. `push --limit 1 --draft` followed by `status` shows the item as created.
- **eBay:**
  1. `--no-publish --limit 1` creates an offer without errors.
  2. `--limit 1` publishes one listing. Check it on eBay, especially the
     category and any required aspects such as Author.
- **Amazon:**
  1. `push --validate-only` reports the offers as `VALID`.
  2. `push --limit 1` followed by `status` (after a few minutes) shows the offer as `LIVE`.
  3. `push --markets all --validate-only` shows which other stores accept the offers.
- **Casa del Libro:**
  1. `check` shows your shop.
  2. `push --dry-run` builds the offer file. Look through it.
  3. `push --limit 1` followed by `status` shows the offer as `LIVE`.
- **Fnac:**
  1. `check` logs in.
  2. `push --dry-run` builds the XML. Look through it.
  3. `push --limit 1` reports the offer as accepted, and `status` shows it as `LIVE`.
- **AbeBooks / IberLibro:**
  1. `push --dry-run` builds the request. Look through it.
  2. `push --limit 1` returns code `600`. Find the book in your seller account or on IberLibro.
- **El Corte Inglés:**
  1. `check` shows your shop, and `states` confirms `11` is "New".
  2. `push --dry-run` builds the offer file. Look through it.
  3. `push --limit 1` followed by `status` shows the offer as `LIVE`.

## Tests

```powershell
python tests\test_vinted_api.py
python tests\test_ebay.py
python tests\test_amazon.py
python tests\test_casadellibro.py
python tests\test_fnac.py
python tests\test_abebooks.py
```

Each test starts a fake API on `127.0.0.1:8765` and runs the script against
[tests/test_products.json](tests/test_products.json). They need no
credentials, and nothing is sent to any marketplace.
