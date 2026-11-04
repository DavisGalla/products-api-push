# Product pushers

Push a JSON file of products to marketplaces through their official APIs:

| Script | Marketplace | Auth | Docs |
|---|---|---|---|
| `vinted_api_push.py` | Vinted (Vinted Pro Integrations API) | API token | [docs/vinted_api_push.md](docs/vinted_api_push.md) |
| `ebay_push.py` | eBay EU (Sell Inventory API) | OAuth refresh token | [docs/ebay_push.md](docs/ebay_push.md) |

Both scripts read the same product format, which is the scraper output in
[products.example.json](products.example.json). They skip sold-out products,
products with no photos and products with no price, and they remember what
they've already pushed so a re-run doesn't create duplicates.

```powershell
pip install -r requirements.txt   # only ebay_push.py needs this; the Vinted script uses only the standard library
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

## First run with real credentials

Both scripts have only been tested against local mocks of the APIs. Before you
push your whole catalogue, try one item at a time:

- **Vinted:**
  1. `ontologies` lists categories, which confirms your token works.
  2. `push --validate-only` reports all items as valid.
  3. `push --limit 1 --draft` followed by `status` shows the item as created.
- **eBay:**
  1. `--no-publish --limit 1` creates an offer without errors.
  2. `--limit 1` publishes one listing. Check it on eBay, especially the
     category and any required aspects such as Author.

## Tests

```powershell
python tests\test_vinted_api.py
python tests\test_ebay.py
```

Each test starts a fake API on `127.0.0.1:8765` and runs the script against
[tests/test_products.json](tests/test_products.json). They need no
credentials, and nothing is sent to Vinted or eBay.
