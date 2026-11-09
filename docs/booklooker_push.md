# booklooker_push.py reference

This script pushes books to **booklooker.de**, a large German marketplace
for new, used and antiquarian books. It uses the official **booklooker REST
API 2.0** (<https://www.booklooker.de/pages/rest_api.php>, gateway
`https://api.booklooker.de/2.0`). Everything below describes how the script
behaves today.

booklooker works like AbeBooks: you list **your own copy** of each book, and
no catalog match is needed. The script uploads all books as one
**tab-separated article file** in booklooker's own format, the same columns
as booklooker's official upload template. **New books** are completed from
the German VLB catalog by ISBN.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [The article file](#the-article-file)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Login:** the script exchanges your API key for a token
   (`POST /authenticate`). The token expires after 10 minutes without use,
   so the script renews it when needed.
2. **Account check:** before uploading, `GET /account?mediaType=0` confirms
   the account can sell books. That means verified, with tax data, shipping
   costs for books, payment methods, and no selling block. If not, nothing is
   sent, and the reasons are shown.
3. **Upload:** the script writes all offers into one file
   (`products_<date_time>.txt`, UTF-8) and uploads it with
   `POST /file_import`:
   - mode `dataType=0` (add / change)
   - books `mediaType=0`
   - booklooker format `formatID=1`
   - `encoding=UTF-8`

   An offer with an order number that already exists is **overwritten**,
   so re-sending is safe.
4. **Wait:** `GET /file_status` is polled until the file is `UPLOAD_DONE`.
5. **Errors:** `file_status` with `showErrors=1` lists the rejected
   articles, with booklooker's error ID. They're printed and not recorded.
   Accepted offers are saved in `booklooker_posted.json`.
6. **Sync:**
   - `--resend` re-uploads books whose price changed.
   - `--delete-sold-out` deletes sold-out books (`DELETE /article`).
     booklooker removes them within 2 hours.
7. **Status:** `status` asks `GET /article_status` for every pushed book:
   ACTIVE, SOLD or DELETED.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**1. Account.** You need a booklooker.de seller account. To offer **new
books** it must be a **commercial** (*gewerblich*) account, because of the
German fixed book price law. Used and antiquarian books can be sold from
any account. In the account, set up:
- verification
- tax data
- shipping costs for books
- payment methods

`check` shows what's missing.

**2. API key.** On booklooker.de, generate your personal **API key** (the
API page links to it).

**3. Credentials.** Add it to Bitwarden as `BOOKLOOKER_API_KEY` (see
[push_all.md](push_all.md#where-credentials-are-stored)), or for a quick local
test:

```powershell
$env:BOOKLOOKER_API_KEY = "..."
```

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `CONDITION_MAP` | Your condition mapped to booklooker's `Zustand` (1 = like new … 4 = heavily worn) and `Neuware?` (new: `ja`/`nein`). Conditions that aren't listed are skipped. |
| `LANGUAGE` | Language of your books as shown on booklooker (`Spanisch`). |
| `CATEGORY_NO` | Optional booklooker category number (*Sparten-Nr.*), from the template's *Bücher_Sparten* sheet. |
| `PRICE_DECIMAL` | Decimal separator for prices in the file (`,` → `8,50`). |
| `DEFAULT_WEIGHT_G` | Parcel weight in grams when a product has no `weight_kg`. |

| Source condition | `Zustand` | `Neuware?` |
|---|---|---|
| `NewCondition` (or missing) | 1 | ja |
| `UsedCondition` | 2 | nein |
| `RefurbishedCondition` | 2 | nein |
| `DamagedCondition` | 4 | nein |

## Commands

| Command | What it does |
|---|---|
| `check` | Tests the API key, and shows whether the account can sell books and, if not, why (e.g. `SHIPPING_COSTS_MISSING`). |
| `push` | Uploads offers in one article file and reads booklooker's results (see below). |
| `status` | Shows every pushed offer: ACTIVE, SOLD or DELETED. |

### `push`

```powershell
python booklooker_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--price-factor` | `1.0` | Multiplies every source price, rounded half-up to cents. See the [fixed book price](#limitations). |
| `--resend` | off | Re-uploads books that were already pushed, when their price changed. |
| `--delete-sold-out` | off | Deletes pushed books that are now `sold_out`. |
| `--dry-run` | off | Writes `booklooker_articles.txt` and sends nothing. |
| `--limit N` | `0` (all) | Uploads at most N new offers. |
| `--yes` | off | Skips the confirmation prompt. |
| `--wait` / `--poll` | `900` / `15` | Maximum seconds to wait for booklooker to process the file, and seconds between checks. |

Use this order for your first run:

```powershell
python booklooker_push.py check                  # API key works, account ready to sell books?
python booklooker_push.py push --dry-run         # look at booklooker_articles.txt
python booklooker_push.py push --limit 1         # one real offer
python booklooker_push.py status                 # ACTIVE?
python booklooker_push.py push                   # the rest
python booklooker_push.py push --resend --delete-sold-out   # later: sync prices, delete sold-out
```

## Input JSON format

This is the same scraper format that the other pushers use. Optional fields
fill more columns.

| Field | Column | Notes |
|---|---|---|
| `id` or `sku` | `Bestell-Nr` | Your order number. **Required.** |
| `title` | `Titel` | **Required.** |
| `price_eur` or `price` | `Ihr Preis in €` | **Required.** Rounded half-up, with `PRICE_DECIMAL`. |
| `isbn` or `ean` | `ISBN` | **Required for new books**, optional for used ones. ISBN-10 is converted. |
| `condition` | `Zustand (1-4)`, `Neuware?` | See `CONDITION_MAP`. |
| `description` | `Beschreibung` | Tabs and line breaks are replaced with spaces. |
| `images` / `image` | `Cover-URL` | The first photo URL. |
| `author`, `publisher` (or `brand`), `year`, `binding`, `pages`, `weight_kg` | `Autor`, `Verlag`, `Jahr`, `Einband`, `Seiten`, `Gewicht in g` | Optional. |
| `sold_out` | | `true` means the product is skipped, or deleted with `--delete-sold-out`. |

## The article file

The file is tab-separated, with the column names in the first line and one
offer per line. The 27 columns are those of booklooker's books template
(*booklooker_vorlage*, sheet *Bücher*), in that order:

```
Sparten-Nr.  Autor  Titel  Verlag  Auflage  Jahr  Ort  Einband  Zustand (1-4)  Beschreibung  Sprache  ISBN  Seiten
Format  Bestell-Nr  Gewicht in g  Ihr Preis in €  unbenutzt  unbenutzt  Cover-URL  Stichwort  unbegrenzte Stückzahl?
Neuware?  Erstausgabe?  Signiert?  unbenutzt  Persönliche Notiz
```

Each run also saves the file it uploads as `booklooker_articles.txt`, so you
can check exactly what was sent.

## Files the script creates

| File | Contents |
|---|---|
| `booklooker_posted.json` | Order number mapped to `isbn`, `price`, `condition`, `file`, `status` and times. Delete an entry to push that book again (the offer is overwritten, not duplicated). |
| `booklooker_articles.txt` | The last article file that was built or uploaded. |

## Errors and troubleshooting

booklooker answers `status: NOK` with a code, and rejected articles come
with an error ID. The script prints both, for example
`! 142534: error 12 (new-book price differs from the fixed price): …`.

| Message | Cause | Fix |
|---|---|---|
| `Missing environment variables` | API key not set | Set `BOOKLOOKER_API_KEY`. |
| `AUTHENTICATION_FAILED` | Wrong API key | Generate a new key on booklooker.de. |
| `not ready to sell books: …` | `ACCOUNT_NOT_VERIFIED`, `TAX_DATA_MISSING`, `SHIPPING_COSTS_MISSING`, `PAYMENT_METHODS_MISSING` or `SELLING_BLOCKED` | Complete the account on booklooker.de. |
| `error 4` (new books in a private account) | New books offered from a private account | Switch to a commercial account, or offer used books only. |
| `error 12` (new-book price differs from the fixed price) | The price isn't the German fixed book price | Use the fixed price (don't use `--price-factor` on new books). |
| `error 15` (no data for this ISBN) | booklooker's VLB catalog doesn't know the ISBN | Common for books published outside Germany. Offer them as used, or ask booklooker. |
| `error 1` / `error 2` | Duplicate order number, or identical offers | Make sure every product has its own `id`/`sku`. |
| `error 11` (price or currency missing) | Price not recognised | Check the format, and try `PRICE_DECIMAL = "."`. |
| `still UPLOAD_IN_PROGRESS` | booklooker is processing a large file | Run `push` again later. Nothing was recorded, and re-uploading is safe. |
| `QUOTA_EXCEEDED` | Too many calls per minute | The script waits 60 seconds and retries. |

## Limitations

- **German fixed book price (Buchpreisbindung).** New books must be sold at
  their fixed German price, and booklooker rejects anything else (error 12).
  Only commercial accounts may offer new books.
- **New books from Spain may be rejected.** booklooker checks new books
  against Germany's VLB catalog, which often doesn't list Spanish-language
  books (error 15). Used books aren't affected.
- **One copy per offer.** booklooker's books template has no quantity column,
  so each offer is one copy (`unbegrenzte Stückzahl? = nein`).
- **Photos only as a URL.** The first photo goes in `Cover-URL`. booklooker's
  separate photo ZIP upload isn't used.
- **Not verified:**
  - The decimal separator booklooker expects in prices. It's `,` by default,
    as in a German Excel export (`PRICE_DECIMAL`). booklooker's documentation
    only refers to its template and daten@booklooker.de.
- **The script hasn't been run against the real booklooker API yet.** Start
  with `check`, `--dry-run` and `--limit 1`. You can also upload
  `booklooker_articles.txt` by hand on booklooker.de (*Daten-Upload*) to
  check the format first.

## Testing

```powershell
python tests\test_booklooker.py
```

[tests/test_booklooker.py](../tests/test_booklooker.py) starts a fake
booklooker REST API on `127.0.0.1:8765`.

The fake:
- authenticates the API key, with expiring tokens
- checks the account's readiness to sell
- receives the multipart article file (UTF-8, tab-separated) and checks its
  27-column template header
- processes it over several polls

It applies booklooker's documented error IDs:
- duplicate order number
- empty title
- missing price
- no VLB data for a new book
- a new-book price that differs from the fixed price

It also serves the error list, `article_status` and `DELETE article`, and
returns an HTTP 503 once.

The test checks the following:
- API key and account-readiness errors are reported
- the file format: header, columns, the new and used book rows
- skips (new books need an ISBN)
- the upload parameters and polling
- recording, with one login per run
- re-runs don't upload again
- `--resend` (half-up rounding with a decimal comma; only changed prices)
- the fixed-price rejection for a new book
- `--delete-sold-out` with token renewal
- `status`
- a slow file is safe to retry

Your real `booklooker_posted.json` is never touched, because the test runs
in a temporary folder.
