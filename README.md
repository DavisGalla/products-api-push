# Vinted product pusher

`vinted_api_push.py` creates Vinted listings from a JSON file of products,
using the official **Vinted Pro Integrations API** with your API token. It
doesn't need a browser and has no dependencies beyond Python 3.8+.

Full reference: [docs/vinted_api_push.md](docs/vinted_api_push.md)

## Quick start

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

## First run with a real token

The script has only been tested against a local mock of the API. Before you
push your whole catalogue, confirm it in this order:

1. `ontologies` lists categories. This confirms the token and request signing work.
2. `push --validate-only` reports all items as valid. Vinted checks them but creates nothing.
3. `push --limit 1 --draft` followed by `status` shows the item as created.

## Tests

```powershell
python tests\test_vinted_api.py
```

The tests run against a local fake Vinted server on port 8765. They need no
token, and nothing is sent to Vinted.
