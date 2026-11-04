# abebooks_push.py reference

This script pushes books to **AbeBooks / IberLibro** through the official
**AbeBooks Inventory Update API**. That's an XML API over TLS at
`https://inventoryupdate.abebooks.com:10027`, documented in AbeBooks'
[Inventory Update API User Documentation](https://assets.brightspot.abebooks.a2z.com/05/fb/7a341d8f4ac19c537008252ce42e/inventory-update-api-user-guide.pdf).
Everything below describes how the script behaves today.

IberLibro.com is AbeBooks' Spanish site. Books in your AbeBooks inventory are
listed there, and on the other AbeBooks sites, from the same inventory.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [What is sent to AbeBooks](#what-is-sent-to-abebooks)
- [Files the script creates](#files-the-script-creates)
- [Response codes and troubleshooting](#response-codes-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

AbeBooks works differently from Amazon, Fnac and Casa del Libro. An AbeBooks
listing describes **your copy** of a book (title, publisher, ISBN, condition,
description and price), so a book **doesn't need to exist in a catalog first**.
Books without an ISBN can be listed too.

- The API has one method, `inventoryUpdateRequest`, with at most 100 books per request.
- Each book has a `transactionType` of `add`, `update` or `delete`.
- Each book is identified by your `vendorBookID`, which is the product's `id`
  or `sku`, at most 15 characters.
- AbeBooks answers with a code for every book. `600` means success.
- An `add` or `update` **replaces all fields** of that listing, so the script
  always sends the complete book.
- **AbeBooks has no test environment.** Every request that isn't a dry run
  changes your real inventory.

## Setup

**Requirements:** Python 3.8 or newer, and `requests` (`pip install -r requirements.txt`).

**Account.** You need an AbeBooks or IberLibro **seller account**, plus your
**username** (client PIN) and **API key**. The key is in your seller account
(*Members Menu* / *Mi cuenta de vendedor*). If it isn't shown, ask AbeBooks
seller support for API access.

```powershell
$env:ABEBOOKS_USERNAME = "your seller username"
$env:ABEBOOKS_API_KEY  = "your API key"
```

**Settings at the top of the script:**

| Setting | Purpose |
|---|---|
| `JSON_FILE` | Products file used when `push` gets no path. |
| `CURRENCY` | Price currency (`EUR`). It must match your account's currency. |
| `BOOK_CONDITIONS` | Your condition mapped to the AbeBooks `bookCondition` keyword (see below). |
| `BOOKSELLER_CATALOGUE` | Optional name of your own catalogue for these books. |

| Source condition | AbeBooks `bookCondition` |
|---|---|
| `NewCondition` (or missing) | `New` |
| `UsedCondition` | `Very Good` |
| `RefurbishedCondition` | `Good` |
| `DamagedCondition` | `Poor` |

AbeBooks also accepts `As New`, `Fine` and `Fair`.

## Commands

### `push`

```powershell
python abebooks_push.py push [json_file] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `json_file` | `JSON_FILE` setting | Products file. |
| `--quantity` | `1` | Copies per listing (at most 999). A product's own `quantity` takes precedence. |
| `--price-factor` | `1.0` | Multiplies every source price. See the [fixed book price](#limitations). |
| `--pictures` | off | Sends up to 5 image URLs per book. See the [Limitations](#limitations) before using it. |
| `--resend` | off | Also re-sends books that were already pushed, as `update`. Use this to update price or stock. |
| `--delete-sold-out` | off | Deletes previously pushed books that are now `sold_out` in your JSON. |
| `--dry-run` | off | Writes `abebooks_request.xml`, with the API key hidden, and sends nothing. No credentials are needed. |
| `--limit N` | `0` (all) | Sends at most N books. |
| `--yes` | off | Skips the confirmation prompt. |

### `delete`

```powershell
python abebooks_push.py delete 142534 142550
```

This deletes those listings by `vendorBookID`. According to AbeBooks, you can
reinstate a deleted book later with an `update`, which is what `--resend`
sends.

Use this order for your first run:

```powershell
python abebooks_push.py push --dry-run          # look at abebooks_request.xml
python abebooks_push.py push --limit 1          # one real listing; check it on IberLibro
python abebooks_push.py push                    # the rest
python abebooks_push.py push --resend --delete-sold-out   # later: sync price/stock, remove sold-out books
```

## Input JSON format

This is the same scraper format that the other pushers use.

| Field | Sent as | Notes |
|---|---|---|
| `id` or `sku` | `vendorBookID` | **Required.** At most 15 characters, unique per book. |
| `title` | `title` | One of title, author or publisher is required. |
| `author` | `author` | Optional. The scraped data has no author. |
| `publisher` or `brand` | `publisher` | |
| `price_eur` or `price` | `price` (with `currency`) | **Required.** Multiplied by `--price-factor`. |
| `isbn` | `isbn` | Optional. Hyphens and spaces are removed. |
| `description` | `description` | Cut to 4000 characters. |
| `condition` | `bookCondition` | Looked up in `BOOK_CONDITIONS`. |
| `binding` (`hard` or `soft`) | `binding` | Optional. |
| `publish_year` or `year` | `publishYear` | Optional, 4 digits. |
| `quantity` | `quantity amount` | Optional per-product stock. |
| `images`, `image_urls` or `image` | `pictureList` | Only sent with `--pictures` (up to 5). |
| `sold_out` | — | `true` means the product is skipped, or deleted with `--delete-sold-out`. |

## What is sent to AbeBooks

```xml
<?xml version='1.0' encoding='ISO-8859-1'?>
<inventoryUpdateRequest version="1.0">
  <action name="bookupdate">
    <username>…</username>
    <password>…API key…</password>
  </action>
  <AbebookList>
    <Abebook>
      <transactionType>add</transactionType>
      <vendorBookID>142534</vendorBookID>
      <title>Dragona. Libro para bebés con rimas</title>
      <publisher>Cuentos con Valores</publisher>
      <price currency="EUR">8.50</price>
      <description>…</description>
      <bookCondition>New</bookCondition>
      <isbn>9788409591923</isbn>
      <quantity amount="1" />
    </Abebook>
  </AbebookList>
</inventoryUpdateRequest>
```

The request is encoded as ISO-8859-1, as the API requires. Accented Spanish
characters are fine.

## Files the script creates

| File | Contents |
|---|---|
| `abebooks_posted.json` | `vendorBookID` mapped to `isbn`, `title` and `pushed_at`. Delete an entry to push that book again as `add`. |
| `abebooks_request.xml` | The last request that was built, with the API key replaced by `API_KEY_HIDDEN`. |

## Response codes and troubleshooting

| Code | Meaning | What the script does or what to do |
|---|---|---|
| `600` | Success | The book is recorded, or removed from the record after a delete. |
| `601` | Book ID isn't valid | Use an `id` of 1–15 characters. The script checks this before sending. |
| `602` | Book list too large | Not expected, because the script sends at most 100 books per request. |
| `603` | Illegal transaction type | Not expected. |
| `604` | Bad price | Check the price in your JSON. |
| `606` | Bad book data | A title, author or publisher is missing, or a field is invalid. |
| `110` | Wrong username or API key | The script stops. Fix `ABEBOOKS_USERNAME` or `ABEBOOKS_API_KEY`. |
| `100`, `103`, `105`, `106`, `111` | Temporary server or network error | Retried automatically with backoff. |
| `104`, `108`, `109` | Invalid XML, or an action that isn't allowed | Reported. Contact AbeBooks if it persists. |

## Limitations

- **No test environment.** Use `--dry-run` and `--limit 1` before a full run.
- **No status check.** The API can't list your inventory. Check your listings
  in the AbeBooks or IberLibro seller account.
- **Pictures are opt-in.** AbeBooks blocks image URLs from sites where you're
  unlikely to own the copyright (Amazon, eBay and similar), and your scraped
  images come from the source shop. Only use `--pictures` with photos you're
  allowed to use. AbeBooks usually shows a stock cover for books with an ISBN.
- **Fixed book prices.** New books in Spain have a price fixed by law (Ley
  10/2007, at most 5% discount). Don't use `--price-factor` to go above the
  cover price.
- **Replace semantics.** Every `add` or `update` overwrites all fields of the
  listing, including fields you edited by hand in the AbeBooks back office.
- **The script hasn't been run against the real AbeBooks server yet.**

## Testing

```powershell
python tests\test_abebooks.py
```

[tests/test_abebooks.py](../tests/test_abebooks.py) starts a fake AbeBooks
server on `127.0.0.1:8765`. The fake server applies the guide's rules:

- ISO-8859-1 XML header, version, action and credentials (`110` on a mismatch)
- at most 100 books per request
- `vendorBookID` length (`601`), price format (`#.##`, `604`) and required title/author/publisher (`606`)
- field lengths, quantity of at most 999, and at most 5 pictures

It also returns a temporary `105` once, to test the retry. The test checks the
following:

- missing or wrong credentials are reported
- the dry run works without credentials and hides the API key
- the right books are listed and skipped (no ISBN needed)
- the fields, the condition mapping and the ISBN cleanup are correct
- accents survive the ISO-8859-1 encoding
- `--pictures` and quantity are sent
- nothing is sent twice on a re-run
- `--resend` sends `update` with new prices
- `--delete-sold-out` and `delete` remove listings
- server errors for individual books are shown, and those books aren't recorded

Your real `abebooks_posted.json` is never touched, because the test runs in a
temporary folder.
