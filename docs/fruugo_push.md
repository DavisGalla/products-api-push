# fruugo_push.py reference

This script pushes books to **Fruugo**, a marketplace that sells in about 40
countries and translates listings and prices for each of them. Fruugo has
**no listing API**. Every retailer publishes a **product feed** (CSV or XML)
at a fixed web address, and Fruugo downloads it each time a new version is
published, at least once a day. Fruugo's Order API only handles orders.

The script builds that feed from books.json and publishes it. It follows
Fruugo's retailer documentation (Retailer Support wiki,
[fruugo.atlassian.net/wiki/spaces/RR](https://fruugo.atlassian.net/wiki/spaces/RR):
"Fruugo Feed", "Field Specification", "CSV", "Lead Time", "Country
Restrictions", "Media Product Attributes"). Everything below describes how
the script behaves today.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [The feed](#the-feed)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Feed.** Each book becomes one feed row with Fruugo's mandatory fields:
   - `ProductId` and `SkuId` (both your SKU)
   - `ISBN`: Fruugo says it is mandatory for books, and the EAN field stays empty
   - `Brand` (the publisher), `Category`, `Imageurl1`
   - `StockStatus` and `StockQuantity`
   - `Title` and `Description`
   - `NormalPriceWithVAT` and `VATRate`

   It also sends `Language`, `Currency`, `Manufacturer` and the media
   attributes for books (`Attribute2` language, `Attribute3` publisher).
2. **Images.** Fruugo allows at most **150 characters per image URL** and
   needs images of at least **400 × 400 px**. The scraped image URLs are
   167–235 characters long, so the script:
   - downloads each image once (cached in `fruugo_images/`)
   - checks its format and size
   - publishes it next to the feed as `img/<hash>.<ext>`

   An image is only replaced on Fruugo when its file name changes, and the
   name comes from the source URL. Up to 5 images per book are used.
3. **Publish.** Images are written first and the feed last, so the feed never
   points to a missing image. Each file is written as `tmp_<name>` and then
   renamed, so Fruugo never downloads a half-written file. There are two ways
   to publish:
   - **Local folder** (`FRUUGO_FEED_DIR`): the folder your web server serves
     at the feed URL, for example on the server where the pushers run.
   - **SFTP** (`FRUUGO_SFTP_*`): upload to your own web host. The host key
     must be pinned, so the script never logs in to an unknown server.
4. **Check.** The script then downloads the feed from its public URL and
   compares it with what it published. If they differ, look for a CDN or
   cache, or a folder that isn't the one served at the URL.
5. **Only when something changed.** An unchanged feed isn't published again,
   except once every `REPUBLISH_AFTER_HOURS` (20 h), because Fruugo expects a
   fresh feed every day. `--force` publishes anyway.
6. **Sold out and removed books.** The feed is always the full assortment.
   - **Sold out:** `StockStatus` `OUTOFSTOCK` with stock 0 ("may return").
     With `--drop-sold-out` it is `NOTAVAILABLE` instead ("permanently out of
     stock").
   - **Gone from books.json:** the book stays in the feed as `NOTAVAILABLE`
     for `NOTAVAILABLE_KEEP_DAYS` (30 days), so Fruugo removes it, and is left
     out after that.
   - **Safety:** if more than half of the published books would disappear at
     once (for example a broken books.json), the script stops. Pass
     `--allow-mass-removal` if that is really what you want.
   - **Empty feed:** a feed without any available book is never published.

## Setup

1. **Become a Fruugo retailer** ([sell.fruugo.com](https://sell.fruugo.com)).
   Fruugo's integration team reviews the feed before your products go live.
2. **Choose the feed location.** The feed must be at a fixed `http` or `https`
   URL that Fruugo can download. A hard-to-guess folder name keeps it out of
   sight, for example `https://feeds.example.com/k7f3x9/fruugo.csv`. Keep the
   URL short: image URLs next to it must stay under 150 characters, and
   `check` warns if they won't.
3. **Store the settings in Bitwarden** (project `product-pushers`), or for a
   quick local test:
   ```powershell
   $env:FRUUGO_FEED_URL = "https://feeds.example.com/k7f3x9/fruugo.csv"
   $env:FRUUGO_FEED_DIR = "D:\www\feeds\k7f3x9"    # the folder served at that URL
   ```
   For SFTP to a web host, set these instead of `FRUUGO_FEED_DIR`:

   | Variable | Meaning |
   |---|---|
   | `FRUUGO_SFTP_HOST`, `FRUUGO_SFTP_PORT` | The host and port (default 22) |
   | `FRUUGO_SFTP_USER` | The login |
   | `FRUUGO_SFTP_PASSWORD` or `FRUUGO_SFTP_KEY_FILE` | A password, or a key file |
   | `FRUUGO_SFTP_DIR` | The folder on the host that is served at the feed URL |
   | `FRUUGO_SFTP_HOSTKEY` | The host key, e.g. `ssh-ed25519 AAAA...` from `ssh-keyscan`. Compare it with what your host publishes. Separate several keys with `;`. |

   SFTP needs `pip install paramiko`.
4. **Check the settings** at the top of `fruugo_push.py`:

   | Setting | Default | Meaning |
   |---|---|---|
   | `CATEGORY` | (empty) | **Required.** The full Fruugo category path, in English, exactly as in Fruugo's category tree. Products without an accurate category don't go live. |
   | `CATEGORY_MAP` | `{}` | Optional, per source category: `{"Libros infantiles": "<Fruugo path>"}` |
   | `VAT_RATE` | `4` | VAT rate of your registered country in % (Spain: 4 % for books). **Verify with your accountant.** |
   | `CURRENCY` / `LANGUAGE` | `EUR` / `es` | Currency of the prices and language of the texts |
   | `BOOK_ATTRIBUTES` | `{"Attribute2": "Spanish"}` | Fixed media attributes. Fruugo's book mapping: 1 Format, 2 Language, 3 Publisher, 4 Length, 5 Author, 6 Release Date. Tell Fruugo's integration team which ones you use. |
   | `PUBLISHER_ATTRIBUTE` | `Attribute3` | Filled with the publisher (`brand`) |
   | `LEAD_TIME_DAYS` | `None` | Days from order to dispatch. `None` = Fruugo's 24 h default; otherwise at least 2. |
   | `PACKAGE_WEIGHT_G` | `None` | Weight in grams. Mandatory if your Fruugo shipping is priced by weight. |
   | `COUNTRIES` | (empty) | Empty = your account's countries; or an include list such as `ES PT FR` |
   | `MIRROR_IMAGES` | `auto` | `auto` copies images whose URL is over 150 characters, `always` copies all of them, `never` copies none |
   | `NOTAVAILABLE_KEEP_DAYS` | 30 | How long removed books stay in the feed as `NOTAVAILABLE` |

5. **Run `check`**, then `push --dry-run`, and look through `fruugo_feed.csv`.
6. **Run `push`**, and send the feed URL to Fruugo's integration team.

## Commands

| Command | What it does |
|---|---|
| `check` | Settings, whether the target folder or SFTP login works, and what the feed URL answers |
| `push [json_file]` | Builds the feed, publishes images and feed, then checks the public URL |
| `status` | What the last feed contained per book, and whether the URL still serves it |

### `push`

| Option | Meaning |
|---|---|
| `--dry-run` | Write `fruugo_feed.csv` only. No settings or credentials needed, and no image downloads. |
| `--limit N` | At most N new books this run |
| `--drop-sold-out` | Sold-out books become `NOTAVAILABLE` (default: `OUTOFSTOCK`, stock 0) |
| `--force` | Publish, and upload all images again, even if nothing changed |
| `--allow-mass-removal` | Allow retiring more than half of the published books at once |
| `--no-verify` | Don't download the feed URL after publishing |
| `--quantity N` | Stock when the book has no `stock`/`quantity` field (default 1) |
| `--price-factor X` | Multiply source prices |
| `--yes` | No confirmation (used by `push_all`) |

## Input JSON format

| Field | Use |
|---|---|
| `id` or `sku` | `ProductId` and `SkuId` (at most 50 characters, no spaces) |
| `isbn` or `ean` | `ISBN`. ISBN-10 is converted, and the check digit is verified. A non-ISBN EAN goes to `EAN`. |
| `title` | `Title` (max 150 characters) |
| `description` | `Description`, as plain text: URLs and e-mail addresses are removed (Fruugo forbids them), and the text is cut to 5000 characters. Falls back to the title. |
| `brand` or `publisher` | `Brand`, `Manufacturer` and the publisher attribute |
| `category` | Looked up in `CATEGORY_MAP`, otherwise `CATEGORY` |
| `price_eur` or `price` | `NormalPriceWithVAT` (gross price, 2 decimals) |
| `stock` or `quantity` | `StockQuantity` (default `--quantity`) |
| `images` or `image` | `Imageurl1` to `Imageurl5` |
| `condition` | Only `NewCondition`: the feed has no condition field |
| `sold_out` | New books are not listed. Published ones become `OUTOFSTOCK` (or `NOTAVAILABLE`). |

## The feed

The feed is CSV, as the "CSV" page of Fruugo's documentation describes it:

- comma separated, UTF-8
- a header row with Fruugo's field names
- every text field in double quotes, with inner quotes doubled (`46""`)
- line breaks allowed inside quoted fields
- numbers without quotes, with a decimal point

Example row (shortened):

```csv
ProductId,SkuId,ISBN,EAN,Brand,Manufacturer,Category,Imageurl1,…,StockStatus,StockQuantity,…,Title,Description,Language,Currency,NormalPriceWithVAT,VATRate,…,Attribute2,Attribute3,…
"142534","142534","9788410413283",,"Cuentos con Valores","Cuentos con Valores","<your Fruugo category>","https://feeds.example.com/k7f3x9/img/1f0c9a2be4d87a6c3e51.jpg",…,"INSTOCK",1,…,"Dragona. Libro para bebés con rimas","…","es","EUR",8.50,4,…,"Spanish","Cuentos con Valores",…
```

## Files the script creates

| File | Content |
|---|---|
| `fruugo_feed.csv` | The last feed built (also on `--dry-run`) |
| `fruugo_posted.json` | Per SKU: ISBN, status, stock, price, image URLs, the feed row, times. Removed books also have `gone_since`. |
| `fruugo_publish.json` | Hash, size and time of the last publish, the target, and the images already uploaded |
| `fruugo_images/` | Downloaded images, and a `.rejected` note for images that failed the checks, so they aren't downloaded again |

All of them are in `.gitignore`.

## Errors and troubleshooting

| Message | Cause | What to do |
|---|---|---|
| `CATEGORY is not set` | No Fruugo category path | Set `CATEGORY` (or `CATEGORY_MAP`) from Fruugo's category tree |
| `set FRUUGO_FEED_DIR ... or FRUUGO_SFTP_HOST` | No publish target | Set one of them (Bitwarden) |
| `unknown host key` | The SFTP server's key isn't the pinned one | Compare it with your host's published key; update `FRUUGO_SFTP_HOSTKEY` only if it's genuine |
| `image is 200x200 px, Fruugo needs at least 400x400` | Image too small | Use a bigger image. The book is skipped if it has no usable image. |
| `mirrored image URL is … characters (max 150)` | Feed URL too long | Use a shorter folder name or domain |
| `... answered 200 but with a different file` | The URL doesn't serve the folder you write to, or a cache | Check the web server's folder and caching |
| `Refusing: N of M published books would become NOTAVAILABLE` | Most books vanished from books.json | Check the scraper output; if it's right, use `--allow-mass-removal` |
| Products don't appear on Fruugo | Feed or product errors | Check the Fruugo Retailer Portal and the e-mails from the integration team: Fruugo doesn't report them through an API |

## Limitations

- **No live status from Fruugo.** Fruugo shows feed and product errors only in
  its Retailer Portal and through its integration team. `status` can only show
  what was published and whether the URL serves it.
- **One language.** Titles and descriptions are sent in `LANGUAGE`, and Fruugo
  translates them for other countries.
- **New books only.** The feed has no condition field.
- **Image checks** cover format and size (400 × 400 px). The white background
  and the ban on watermarks and placeholders aren't checked.
- **Title case.** Fruugo asks for titles in title case. The script keeps the
  titles as they are (Spanish titles normally use sentence case).
- **The VAT rate** is the one of your registered country (`VAT_RATE`). Verify
  it before selling.
- **GPSR:** Fruugo's feed has only a `Manufacturer` field, which gets the
  publisher. Check with Fruugo what else it needs for your account.
- **Orders** (download, confirm, ship, cancel, return) have their own Fruugo
  Order API. They aren't built yet; see [capabilities.md](capabilities.md).
- **Not tested with Fruugo itself**, because there is no retailer account
  yet. Only the local mock below has been used.

## Testing

```powershell
python tests\test_fruugo.py
```

The test plays the retailer's side on `127.0.0.1` (free ports):

- **A web server** that serves the "scraped" images (long URLs, different
  sizes and formats) and the published folder, as Fruugo would download it.
- **An SFTP server** (paramiko, throw-away host key) for the upload mode. It
  checks the pinned host key, `tmp_` uploads with rename, and creating the
  `img/` folder.

The published feed is read back against Fruugo's CSV rules:

- mandatory fields, the ISBN field for books
- quoting, image URLs of at most 150 characters, images of at least 400 px
- the `StockStatus` values
- the `NOTAVAILABLE` handling and the safety checks

There are 36 checks. Nothing is sent to Fruugo. The SFTP checks need
`pip install paramiko`.
