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
| `carrefour_push.py` | Carrefour marketplace (Mirakl seller API) | API key | [docs/carrefour_push.md](docs/carrefour_push.md) |
| `worten_push.py` | Worten marketplace, Portugal + Spain (Mirakl seller API) | API key | [docs/worten_push.md](docs/worten_push.md) |
| `ibs_push.py` | IBS.it marketplace, Italy (Mirakl seller API) | API key | [docs/ibs_push.md](docs/ibs_push.md) |
| `kaufland_push.py` | Kaufland.es and other Kaufland storefronts (Kaufland Seller API v2) | client key + secret key | [docs/kaufland_push.md](docs/kaufland_push.md) |
| `bol_push.py` | bol.com, Netherlands + Belgium (Retailer API, Offers v11) | client ID + secret | [docs/bol_push.md](docs/bol_push.md) |
| `aliexpress_push.py` | AliExpress as a local seller, e.g. aliexpress.es (AliExpress Open Platform API) | app key + secret, seller authorization (OAuth) | [docs/aliexpress_push.md](docs/aliexpress_push.md) |
| `temu_push.py` | Temu as a local seller in Europe, e.g. temu.com/es (Temu Partner API) | app key + secret + access token from Seller Center | [docs/temu_push.md](docs/temu_push.md) |
| `tiktok_push.py` | TikTok Shop Spain and other EU shops (TikTok Shop Partner API) | app key + secret, seller authorization (OAuth) | [docs/tiktok_push.md](docs/tiktok_push.md) |

**`push_all.py`** runs all the enabled pushers in one go and emails one report
from Gmail ("Vinted 5 pushed, Amazon 1 rejected, Fnac failed ..."). See
[Run everything and get an email report](#run-everything-and-get-an-email-report).

**What can be automated** (listings, sales, messages, leads, payouts) on each
marketplace, and what stays manual:
[docs/capabilities.md](docs/capabilities.md) and the PDF
[docs/marketplace_capabilities.pdf](docs/marketplace_capabilities.pdf).

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

## Carrefour quick start

Carrefour's marketplace also runs on Mirakl, so this works like the Casa del
Libro and El Corte Inglés pushers: books are matched to Carrefour's catalog by
EAN/ISBN, and offers are uploaded as one import. Take the API key and your
shop's API URL from the seller back office.

```powershell
$env:CARREFOUR_API_URL = "https://<instance>.mirakl.net"
$env:CARREFOUR_API_KEY = "..."

python carrefour_push.py check            # key and URL work?
python carrefour_push.py push --dry-run   # writes carrefour_offers.csv, uploads nothing
python carrefour_push.py push --limit 1   # one real offer
python carrefour_push.py status
```

---

## Worten quick start

Worten (Portugal and Spain) also runs on Mirakl, so this works like the
Carrefour pusher: books are matched to Worten's catalog by EAN/ISBN, and
offers are uploaded as one import. One shop sells in both countries. Take the
API key and your shop's API URL from the seller back office.

```powershell
$env:WORTEN_API_URL = "https://<instance>.mirakl.net"
$env:WORTEN_API_KEY = "..."

python worten_push.py check            # key and URL work?
python worten_push.py push --dry-run   # writes worten_offers.csv, uploads nothing
python worten_push.py push --limit 1   # one real offer
python worten_push.py status
```

---

## IBS.it quick start

IBS.it (Italy) also runs on Mirakl, so this works like the Carrefour and Worten
pushers: books are matched to IBS's catalog by EAN/ISBN, and offers are
uploaded as one import. IBS approves sellers itself (professional sellers with
a VAT number). Take the API key and your shop's API URL from the seller back
office.

```powershell
$env:IBS_API_URL = "https://<instance>.mirakl.net"
$env:IBS_API_KEY = "..."

python ibs_push.py check            # key and URL work?
python ibs_push.py push --dry-run   # writes ibs_offers.csv, uploads nothing
python ibs_push.py push --limit 1   # one real offer
python ibs_push.py status
```

---

## Kaufland.es quick start

Books are matched to Kaufland's catalog by EAN/ISBN, and an offer ("unit") is
created on the Spanish storefront. Every request is HMAC-signed with your
keys. Get the Client Key and Secret Key in the Seller Portal under *Settings →
API*, and set up a shipping group and a default warehouse first.

```powershell
$env:KAUFLAND_CLIENT_KEY = "..."
$env:KAUFLAND_SECRET_KEY = "..."

python kaufland_push.py check            # keys work, storefront es available?
python kaufland_push.py push --dry-run   # writes kaufland_units.json, sends nothing
python kaufland_push.py push --limit 1   # one real offer
python kaufland_push.py status           # LIVE? if not, Kaufland's reasons
```

---

## bol.com quick start

Books are matched to bol's catalog by EAN/ISBN, and an offer is created for the
Netherlands and Belgium. New and used books can be sold. Create the Client ID
and Client Secret in the Seller Dashboard under *Instellingen → API
Instellingen*, and set up your delivery promise and an economic operator (GPSR)
there too.

```powershell
$env:BOL_CLIENT_ID = "..."
$env:BOL_CLIENT_SECRET = "..."

python bol_push.py check            # keys work, economic operator?
python bol_push.py push --dry-run   # writes bol_offers.json, sends nothing
python bol_push.py push --limit 1   # one real offer
python bol_push.py status           # for sale in NL/BE? if not, bol's reasons
```

---

## AliExpress quick start

AliExpress has no catalog to attach offers to, so every book becomes its own
product. For ISBNs that AliExpress's EAN database knows, it supplies the
category, attributes and images. You need a developer app (App Key and Secret)
from openservice.aliexpress.com, and you authorize it once for your shop. You
also need a shipping template and an EU responsible person (GPSR) in Seller
Center.

```powershell
$env:ALIEXPRESS_APP_KEY = "..."
$env:ALIEXPRESS_APP_SECRET = "..."

python aliexpress_push.py auth-url --redirect https://your.callback/url   # open it, authorize
python aliexpress_push.py token <code>                 # saves aliexpress_token.json
python aliexpress_push.py check                        # shop, shipping template, GPSR
python aliexpress_push.py push --dry-run               # writes aliexpress_products.json
python aliexpress_push.py push --limit 1 --no-online   # one product, check it in Seller Center
python aliexpress_push.py status
```

---

## Temu quick start

Temu also creates its own product per book, and reviews each new product and
its price before it goes on sale. You need an approved self-developed app from
partner-eu.temu.com (App Key and Secret, plus your server's IP registered).
Then authorize the app in Seller Center and copy the Access Token. All books go
into one Temu category: find it, then fill in its required attributes.

```powershell
$env:TEMU_APP_KEY = "..."
$env:TEMU_APP_SECRET = "..."
$env:TEMU_ACCESS_TOKEN = "..."

python temu_push.py check                       # token, permissions, shipping template
python temu_push.py categories --find book      # find the books category (or: recommend "<title>")
python temu_push.py attributes <id>             # set CATEGORY_ID and PROPERTIES in the script
python temu_push.py push --dry-run              # writes temu_products.json
python temu_push.py push --limit 1              # one real product
python temu_push.py status                      # price review / on sale?
```

---

## TikTok Shop quick start

TikTok Shop also creates its own product per book, and reviews each new product
before it goes live. You need an app from partner.tiktokshop.com (App Key,
App Secret and Service ID), authorized once for your shop. You also need a
warehouse and, if the category requires it, an EU responsible person and
manufacturer in Seller Center. All books go into one TikTok category: find it,
then fill in its required attributes.

```powershell
$env:TIKTOK_APP_KEY = "..."
$env:TIKTOK_APP_SECRET = "..."

python tiktok_push.py auth-url --service-id <id>   # open it, authorize
python tiktok_push.py token <code>                 # saves tiktok_token.json
python tiktok_push.py check                        # shop, warehouse, GPSR
python tiktok_push.py categories --find libro      # then: attributes <id>
python tiktok_push.py push --dry-run               # writes tiktok_products.json
python tiktok_push.py push --limit 1               # one real product
python tiktok_push.py status
```

---

## Run everything and get an email report

Credentials (Gmail and the marketplaces) are stored in **Bitwarden Secrets
Manager**. Bitwarden's `bws` tool fetches them when a run starts, so no
credentials are stored in files.

```powershell
copy push_all_config.example.json push_all_config.json   # choose pushers, products file, recipients

bws run -- python push_all.py --test-email           # Gmail works?
bws run -- python push_all.py --dry-run --no-email   # every pusher in its safe mode, report printed
bws run -- python push_all.py                        # real run, report emailed
```

Pushers without credentials are reported as `NOT CONFIGURED` and skipped.
[docs/push_all.md](docs/push_all.md) covers the Bitwarden setup step by step,
the Gmail App Password, a `.env` alternative, and scheduling on a server.

---

## First run with real credentials

All fifteen scripts have only been tested against local mocks of the APIs. Before you
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
- **Carrefour:**
  1. `check` shows your shop, and `states` confirms `11` is "New".
  2. `push --dry-run` shows which books are in Carrefour's catalog.
  3. `push --limit 1` followed by `status` shows the offer as `LIVE`.
- **Worten:**
  1. `check` shows your shop, and `states` confirms `11` is "New".
  2. `push --dry-run` shows which books are in Worten's catalog.
  3. `push --limit 1` followed by `status` shows the offer as `LIVE`.
- **IBS.it:**
  1. `check` shows your shop, and `states` confirms `11` is "New".
  2. `push --dry-run` shows which books are in IBS's catalog.
  3. `push --limit 1` followed by `status` shows the offer as `LIVE`.
- **Kaufland:**
  1. `check` accepts the keys and lists `es`.
  2. `push --dry-run` shows which books are in Kaufland's catalog.
  3. `push --limit 1` followed by `status` shows the unit as `LIVE`, or Kaufland's reason it isn't.
- **bol.com:**
  1. `check` accepts the keys and lists an economic operator.
  2. `push --dry-run` shows which books are in bol's catalog.
  3. `push --limit 1` followed by `status` shows the offer as `FOR SALE in NL, BE`, or bol's reason it isn't.
- **AliExpress:**
  1. `check` shows your shop, a shipping template and a GPSR responsible person.
  2. `ean <isbn>` shows whether AliExpress knows your books, and `push --dry-run` builds them.
  3. `push --limit 1 --no-online`, check the product in Seller Center, then `status`.
- **Temu:**
  1. `check` accepts the token and lists a shipping template.
  2. `attributes <category>` shows what to put in `PROPERTIES`, and `push --dry-run` builds the products.
  3. `push --limit 1`, then `status` until Temu's price review and product review are done.
- **TikTok Shop:**
  1. `check` shows the shop, a warehouse and the GPSR responsible person and manufacturer.
  2. `attributes <category>` shows what to put in `PRODUCT_ATTRIBUTES`, and `push --dry-run` builds the products.
  3. `push --limit 1`, then `status` until TikTok's review shows it as `LIVE`.

## Tests

```powershell
python tests\test_vinted_api.py
python tests\test_ebay.py
python tests\test_amazon.py
python tests\test_casadellibro.py
python tests\test_fnac.py
python tests\test_abebooks.py
python tests\test_elcorteingles.py
python tests\test_carrefour.py
python tests\test_worten.py
python tests\test_ibs.py
python tests\test_kaufland.py
python tests\test_bol.py
python tests\test_aliexpress.py
python tests\test_temu.py
python tests\test_tiktok.py
python tests\test_push_all.py
```

Each pusher test starts a fake API on `127.0.0.1:8765` and runs the script against
[tests/test_products.json](tests/test_products.json). They need no
credentials, and nothing is sent to any marketplace. `test_push_all.py` uses
fake pushers and a fake mail server on `127.0.0.1:8766`.
