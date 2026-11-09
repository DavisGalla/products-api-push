# galaxus_push.py reference

This script pushes books to **Galaxus**, the Digitec Galaxus marketplace in
Switzerland (galaxus.ch) and Germany (galaxus.de). Galaxus has **no REST API
for listings**. Partners deliver a **data feed file** that Galaxus imports
every 30 minutes. The rules come from the official technical integration
docs (public Confluence space "Technische Integration",
<https://confdg.atlassian.net/wiki/spaces/PHCM>). Everything below describes
how the script behaves today.

The script implements **stage 1, offer assignment** ("Etappe 1:
Angebotszuweisung"). Your offers are attached to products that **already
exist** in the Galaxus catalog, matched by GTIN (ISBN-13 for books). The
script writes one `StockPricingData` CSV file with all offers and uploads it
to the Galaxus SFTP server.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Commands](#commands)
- [Input JSON format](#input-json-format)
- [The feed file](#the-feed-file)
- [Files the script creates](#files-the-script-creates)
- [Errors and troubleshooting](#errors-and-troubleshooting)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Build the feed.** Every valid book becomes one line, with:
   - its ID as `ProviderKey`
   - its ISBN as `MainGtin`
   - the price excluding VAT
   - the VAT rate
   - the stock

   The file is always the **full assortment**. A book that is no longer in the
   file goes offline at Galaxus; this is how Galaxus documents removing offers.
2. **Skip uploads that change nothing.** If the feed is identical to the last
   one uploaded, it isn't uploaded again. After 20 hours it is uploaded anyway,
   because Galaxus wants at least one update a day (`REUPLOAD_AFTER_HOURS`).
3. **Connect.** The script connects to `ftp.digitecgalaxus.ch:22` over SFTP.
   Before it sends the password, it checks the server's host key against the
   keys Galaxus publishes. If the key doesn't match, it stops without logging
   in.
4. **Upload safely.**
   1. The file is written as `tmp_<name>` and its size is checked.
   2. The file is renamed over the configured name, replacing the old file in
      one step (`posix-rename`). On servers that don't support that, the old
      file is removed and the new one renamed.
   3. The size is checked again.

   The Galaxus docs ask for this: without it, Galaxus may download a
   half-written, empty file. A failed upload is retried up to 3 times.
5. **Record the result.** `galaxus_posted.json` lists every offer in the
   uploaded feed. `push_all` counts new and changed entries as "pushed".

Galaxus then imports the file within about 30 minutes. **Import errors
(rejected lines) appear only in the Galaxus Partner Portal** (Katalog →
Feeds), not on the SFTP server, so the script can't read them.

## Setup

1. **Become a Galaxus marketplace partner.** Galaxus onboarding sets up your
   feed and sends:
   - an SFTP user name and password
   - the exact feed file name (by default the script uses
     `StockPricingData_<ProviderName>.csv`)

   Used books need a separate contract annex ("Gebrauchtwaren"), so by default
   only new books are offered.
2. **Install paramiko** (the SFTP library):
   ```powershell
   pip install paramiko
   ```
3. **Store the credentials in Bitwarden** (project `product-pushers`), or for
   a quick local test set them in PowerShell:
   ```powershell
   $env:GALAXUS_SFTP_USER = "..."
   $env:GALAXUS_SFTP_PASSWORD = "..."
   $env:GALAXUS_PROVIDER = "YourShopName"     # letters, digits, _ or -
   ```
   Optional variables:

   | Variable | Use |
   |---|---|
   | `GALAXUS_FEED_NAME` | The exact file name from onboarding, if it isn't `StockPricingData_<provider>.csv` |
   | `GALAXUS_SFTP_DIR` | Upload folder on the server (default: the login folder) |
   | `GALAXUS_SFTP_HOSTKEY` | A new host key (`ssh-ed25519 AAAA…`), if Galaxus renews it |
   | `GALAXUS_SFTP_HOST`, `GALAXUS_SFTP_PORT` | Other server (used by the test) |

4. **Check the settings** at the top of `galaxus_push.py`:

   | Setting | Default | Meaning |
   |---|---|---|
   | `MARKET` | `CH` | `CH` = galaxus.ch, prices in CHF (CHF is the only currency allowed there). `DE` = galaxus.de, prices in EUR. |
   | `EUR_TO_CHF` | `0.94` | Source prices are in EUR. Update the rate, or pass `--chf-rate`. |
   | `VAT_RATE` | CH `2.6`, DE `7` | Reduced VAT rate for books. |
   | `ALLOWED_CONDITIONS` | `NewCondition` | Source conditions that are offered. |
   | `DEFAULT_STOCK` | `1` | Stock when the product has no `stock`/`quantity` field. |

5. **Run `check`.**

Be careful with the password: after several failed logins, Galaxus blocks your
IP address for 3 hours. If your server has a firewall, these are the Galaxus SFTP
IP addresses: 88.198.35.84, 85.10.200.14, 116.203.25.19 and 40.68.213.70.
Connect by host name; use the IP addresses only for whitelisting.

## Commands

| Command | What it does |
|---|---|
| `check` | Logs in to the SFTP server and shows whether the feed file is there, with its size and time |
| `push [json_file]` | Builds the feed and uploads it, if something changed |
| `status` | Shows what the last uploaded feed contained, and the file currently on the server |

### `push`

| Option | Meaning |
|---|---|
| `--dry-run` | Write `galaxus_feed.csv` only. No credentials needed, and nothing is uploaded. |
| `--drop-sold-out` | Leave sold-out books out of the feed. By default they stay in it with stock 0. |
| `--force` | Upload even if the feed didn't change. Also needed to upload an **empty** feed, which takes every offer offline. |
| `--limit N` | Add at most N new offers this run. Offers already in the feed always stay. |
| `--price-factor X` | Multiply source prices (for example `1.1` = +10 %) |
| `--chf-rate X` | EUR → CHF rate for this run |
| `--yes` | Don't ask for confirmation (used by `push_all`) |

The whole feed is sent every time, so there is no `--resend`: a changed price
or stock is in the next upload automatically.

## Input JSON format

The same products JSON as the other pushers. The script uses these fields:

| Field | Use |
|---|---|
| `id` or `sku` | `ProviderKey`. At most 50 plain ASCII characters, unique, and never reused for another book. |
| `isbn` or `ean` | `MainGtin`. ISBN-10 is converted to EAN-13, and the check digit is verified. |
| `price_eur` or `price` | Price including VAT, in EUR |
| `condition` | Only `NewCondition` by default |
| `stock` or `quantity` | Optional. If missing, `DEFAULT_STOCK` is used. |
| `sold_out` | `true` → stock 0, or left out with `--drop-sold-out` |

A book is skipped, with the reason shown, if it has:

- no ID, or an ID that isn't plain ASCII
- no price
- no ISBN/EAN, or an invalid one
- a condition that isn't allowed
- the same ISBN as another book already in the feed
- the same ID as another book already in the feed

## The feed file

The file follows the Galaxus file format rules:

- UTF-8, `;` as the separator
- a header row
- text fields in double quotes
- `.` as the decimal separator, with no thousands separators
- no scientific notation

Example (galaxus.ch):

```
ProviderKey;MainGtin;ManufacturerKey;PriceExclVat_CHF;VatRatePercentage;QuantityOnStock
"T-NEW";9788409591923;"9788409591923";7.79;2.6;1
```

| Column | Value |
|---|---|
| `ProviderKey` | The book's ID |
| `MainGtin` | ISBN-13 |
| `ManufacturerKey` | The ISBN again. Books have no manufacturer part number, and the ISBN is the publisher's article number. |
| `PriceExclVat_CHF` / `PriceExclVat_EUR` | Source price × price factor (× CHF rate) ÷ (1 + VAT), rounded half-up to cents. For example, 8.50 EUR × 0.94 ÷ 1.026 = 7.79 CHF. |
| `VatRatePercentage` | `2.6` (CH) or `7` (DE) |
| `QuantityOnStock` | Stock, 0 when sold out |

Don't open and save the file in Excel. Excel turns long numbers such as
ISBNs into scientific notation (9.79E+12), which Galaxus rejects.

## Files the script creates

| File | Content |
|---|---|
| `galaxus_feed.csv` | The last feed that was built (also on `--dry-run`) |
| `galaxus_posted.json` | One entry per offer in the last uploaded feed: GTIN, price, stock, since, updated_at |
| `galaxus_upload.json` | File name, SHA-256, size and time of the last upload |

All three are in `.gitignore`.

## Errors and troubleshooting

| Message | Cause | What to do |
|---|---|---|
| `showed an unknown host key` | The server's key isn't one of the published Galaxus keys | Check the current keys in the Galaxus docs ("3 Datenübermittlung"), then set `GALAXUS_SFTP_HOSTKEY` |
| `rejected the user name/password` | Wrong login | Fix the Bitwarden secrets. Don't retry many times, or your IP is blocked for 3 hours. |
| `cannot connect to …` | Network, firewall, or IP block | Check port 22 outbound, or wait 3 hours after failed logins |
| `upload of … failed after 3 tries` | The connection dropped during the upload | Run again. Nothing is recorded until an upload succeeds. |
| `The new feed is EMPTY` | No valid book (wrong file?) | Check the products file. Use `--force` only if you really want every offer offline. |
| `Feed unchanged … nothing uploaded` | Normal | Nothing to do |
| Offers missing on galaxus.ch | Galaxus has no catalog product for that ISBN, or rejected the line | Check Partner Portal → Katalog → Feeds |

## Limitations

- **Only books already in the Galaxus catalog get an offer.** Creating new
  products (stage 2: `ProductData` file with titles, descriptions, images and
  specifications) isn't built yet.
- **New books only** by default. Used books need the Gebrauchtwaren contract
  annex. Ask Galaxus how they should be marked before you allow other
  conditions.
- **Import errors** are visible only in the Partner Portal.
- **galaxus.de:** in Germany, new books have a fixed price
  (Buchpreisbindung). The price you send must match the fixed price.
- **Orders** come as XML files (ORDP) on the same SFTP server. Galaxus also
  expects confirmations, delivery notes and invoices back as XML (ORDR, DELR,
  EXPINV). None of this is built yet; see
  [capabilities.md](capabilities.md).
- **Not tested against the real Galaxus server**, because there is no partner
  account yet. Only the local mock below has been used.

## Testing

```powershell
pip install paramiko
python tests\test_galaxus.py
```

The test starts a **real SFTP server** (built with paramiko) on `127.0.0.1`,
with its own temporary host key and folder. It checks:

- no password is sent to a server with an unknown host key
- wrong-password handling
- the `tmp_` upload and rename, the remove-and-rename fallback, and a retry
  after a failed write
- the upload is skipped when nothing changed, `--force`, the empty-feed guard,
  `--drop-sold-out`, `--limit`, and `status`

It then imports the uploaded file the way the Galaxus docs describe:

- UTF-8, `;`, header, quoted text
- unique ASCII `ProviderKey`
- valid GTIN check digits
- decimals without thousands separators
- currency suffix on the price column

There are 22 checks. Nothing is sent to Galaxus.
