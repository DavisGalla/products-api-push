# Architecture

This document describes how the product pushers solution is built: its parts,
how data and credentials flow through it, how it runs on a server, and what
has been verified so far. The setup and use of each part are covered in the
other documents in `docs/`.

- [Purpose](#purpose)
- [Overview](#overview)
- [Components](#components)
- [Marketplace integrations](#marketplace-integrations)
- [Run flow](#run-flow)
- [Data and state](#data-and-state)
- [Credentials and security](#credentials-and-security)
- [Deployment](#deployment)
- [Error handling and monitoring](#error-handling-and-monitoring)
- [Testing](#testing)
- [Current status](#current-status)
- [Adding a marketplace](#adding-a-marketplace)

## Purpose

Book products scraped from a source shop are listed automatically on twenty-two
marketplaces through their **official seller APIs**. After every run, one
email reports what happened on each marketplace.

## Overview

```mermaid
flowchart LR
    S[Scraper] -->|books.json| R
    BW[(Bitwarden<br>Secrets Manager)] -->|bws run:<br>credentials as env vars| R
    C[push_all_config.json] --> R
    R[push_all.py<br>runner] --> V[vinted_api_push.py] --> VA[Vinted Pro API]
    R --> E[ebay_push.py] --> EA[eBay Sell Inventory API]
    R --> A[amazon_push.py] --> AA[Amazon SP-API<br>9 EU stores]
    R --> CL[casadellibro_push.py] --> CLA[Casa del Libro<br>Mirakl API]
    R --> F[fnac_push.py] --> FA[Fnac Marketplace API]
    R --> AB[abebooks_push.py] --> ABA[AbeBooks / IberLibro<br>Inventory Update API]
    R --> BL[booklooker_push.py] --> BLA[booklooker.de<br>REST API 2.0]
    R --> EC[elcorteingles_push.py] --> ECA[El Corte Inglés<br>Mirakl API]
    R --> CF[carrefour_push.py] --> CFA[Carrefour<br>Mirakl API]
    R --> W[worten_push.py] --> WA[Worten PT/ES<br>Mirakl API]
    R --> IB[ibs_push.py] --> IBA[IBS.it<br>Mirakl API]
    R --> EM[empik_push.py] --> EMA[Empik PL<br>Mirakl API]
    R --> LE[leclerc_push.py] --> LEA[E.Leclerc FR<br>Mirakl API]
    R --> K[kaufland_push.py] --> KA[Kaufland.es<br>Seller API v2]
    R --> BO[bol_push.py] --> BOA[bol.com NL/BE<br>Retailer API]
    R --> CD[cdiscount_push.py] --> CDA[Cdiscount<br>Octopia Seller API]
    R --> AL[allegro_push.py] --> ALA[Allegro PL<br>REST API]
    R --> AE[aliexpress_push.py] --> AEA[AliExpress<br>Open Platform API]
    R --> TM[temu_push.py] --> TMA[Temu<br>Partner API]
    R --> TT[tiktok_push.py] --> TTA[TikTok Shop<br>Partner API]
    R --> GX[galaxus_push.py] --> GXA[Galaxus CH/DE<br>feed over SFTP]
    R --> OT[otto_push.py] --> OTA[OTTO Market DE<br>Products API v5]
    R --> L[logs/ + report.txt]
    R -->|SMTP over TLS| G[Gmail] --> M[Report email]
```

The solution has three layers:

1. **Input**: the scraper's products JSON, plus the run configuration.
2. **Pushers**: twenty-two independent scripts, one per marketplace. Each turns the
   common product format into that marketplace's API calls.
3. **Orchestration and reporting**: `push_all.py` runs the enabled pushers,
   works out each one's result, saves logs, and emails one report through
   Gmail.

Credentials come from Bitwarden Secrets Manager at run time and are never
stored in the code, the repository, or the configuration.

## Components

| Component | Responsibility | Technology |
|---|---|---|
| Scraper (separate project) | Produces `books.json`: id, title, price, ISBN, condition, images, stock | Python |
| `push_all_config.json` | Which pushers run, their options, products file, report recipients, timeouts | JSON, not in git |
| **`push_all.py`** | Runs the pushers one after another, counts results, writes logs, sends the email, prevents overlapping runs | Python standard library only |
| 22 pushers (`*_push.py`) | One per marketplace: validate products, match the catalog, create or update listings, record what was pushed | Python + `requests` (Vinted: standard library) |
| State files (`*_posted.json`) | Per marketplace, what has already been pushed, so re-runs don't duplicate | JSON, local, not in git |
| Bitwarden Secrets Manager + `bws` | Stores all credentials and injects them as environment variables at start-up | Bitwarden cloud, `bws` 2.1.0 |
| Gmail account | Sends the report (SMTP, port 465, TLS, App Password) | Gmail |
| `logs/<date_time>/` | Full output of every pusher plus the report text, per run | Text files, not in git |
| Tests (`tests/`) | A fake API server per marketplace, plus a fake mail server for the runner | Python |

Every pusher also runs on its own (for example
`python amazon_push.py push --limit 1`). The runner only calls the pushers; it
doesn't contain any marketplace logic.

## Marketplace integrations

| Marketplace | API | Authentication | How a book is listed | Documentation |
|---|---|---|---|---|
| Vinted | Vinted Pro Integrations API | Access key + signing key; every request is HMAC-SHA256 signed | Creates a new item (own photos, text, price). Processed asynchronously. | Official, public |
| eBay (EU) | Sell Inventory API | OAuth refresh token | Inventory item → offer → publish | Official, public |
| Amazon (ES, DE, FR, IT, NL, BE, PL, SE, IE; UK optional) | Selling Partner API: Catalog Items + Listings Items | Login with Amazon refresh token | Finds the existing Amazon page by ISBN in each store, adds an offer (price, condition, stock). Converts SEK/PLN/GBP prices. | Official, public |
| Casa del Libro | Mirakl seller API | API key | Matches the catalog by EAN, uploads all offers as one CSV import, reads the error report | Mirakl standard |
| Fnac | Fnac Marketplace API v2.6 (XML) | Partner ID + shop ID + key → session token | Sends offers by EAN in batches, polls the batch status | Official, public |
| AbeBooks / IberLibro (+ ZVAB, by invitation) | AbeBooks Inventory Update API (XML) | Username + API key | Describes its own copy of a book; no catalog match needed. Add / update / delete. The same inventory is listed on all AbeBooks sites, including ZVAB. | Official PDF |
| booklooker.de (Germany) | booklooker REST API 2.0 | API key → 10-minute token | Own copy of a book (no catalog match); all offers in one tab-separated article file in booklooker's template format; new books completed from the VLB | Official, public |
| El Corte Inglés | Mirakl seller API | API key | Same flow as Casa del Libro | Mirakl standard; El Corte Inglés doesn't publish its own documentation |
| Carrefour (ES; FR is a separate instance) | Mirakl seller API | API key | Same flow as Casa del Libro | Mirakl standard; Carrefour doesn't publish its own documentation |
| Worten (Portugal + Spain, one shop) | Mirakl seller API | API key | Same flow as Casa del Libro; one price for both countries | Mirakl standard; Worten doesn't publish its own documentation |
| IBS.it (Italy) | Mirakl seller API | API key | Same flow as Casa del Libro | Mirakl standard; IBS doesn't publish its own documentation |
| E.Leclerc (France) | Mirakl seller API | API key | Same flow as Casa del Libro | Mirakl standard; E.Leclerc doesn't publish its own documentation |
| Empik (Poland) | Mirakl seller API | API key | Same flow as Casa del Libro; EUR prices converted to PLN (`EUR_TO_PLN`) | Mirakl standard; Empik doesn't publish its own documentation |
| Kaufland.es (other storefronts optional) | Kaufland Seller API v2 | Client key + secret key; every request is HMAC-SHA256 signed | Checks the catalog by EAN, then creates a unit (offer) per book; `id_offer` makes re-sends update it | Official, public |
| bol.com (Netherlands + Belgium) | bol.com Retailer API, Offers API v11 | Client ID + secret → short-lived token (client credentials) | Checks the catalog by EAN, then creates an offer per book (new or used); economic operator for GPSR | Official, public |
| Cdiscount (France) | Octopia Seller API (`/seller/v2`) | Client ID + secret → 2-hour token, plus Seller ID | Checks the catalog by EAN, then sends offers (new or used) in an Upsert offer package and reads each offer's result | Official, public |
| Allegro (Poland) | Allegro REST API | Client ID + secret, one-time device-code authorization (12 h token, single-use refresh token, max 3 months) | Offer on the catalog product by EAN (new or used), PLN price, account shipping/after-sales settings and GPSR producer | Official, public (OpenAPI) |
| AliExpress (local seller, aliexpress.es) | AliExpress Open Platform API | App key + secret (HMAC-SHA256 signed) and the seller's OAuth token, refreshed automatically | Creates its own product per book; category, attributes and images come from AliExpress's EAN database when it knows the ISBN | Official, public |
| Temu (local seller, temu.com/es) | Temu Partner API (`bg.local.goods.*`) | App key + secret (MD5-signed requests) and the access token from Seller Center | Creates its own product per book in one configured category; Temu reviews the product and its price | Official docs need a seller login; built from a public daily mirror |
| TikTok Shop (Spain, EU) | TikTok Shop Partner API (202309+) | App key + secret (HMAC-SHA256 signed) and the seller's OAuth token, refreshed automatically | Creates its own product per book in one configured category, with GPSR IDs; TikTok reviews it | Official docs are a web app; built from a generated OpenAPI spec and a widely used client |
| Galaxus (Switzerland, Germany) | Galaxus data feed (stage 1, offer assignment): `StockPricingData` CSV on the Galaxus SFTP server | SFTP user + password; the server's host key is pinned | One feed file with every offer (ISBN, price excl. VAT in CHF or EUR, stock), matched to the Galaxus catalog by GTIN; uploaded as `tmp_` and renamed; imported every 30 minutes | Official, public (Confluence) |
| OTTO Market (Germany) | OTTO Market API: Products v5 + Availability v1 | Client ID + secret of a Partner Connect self-app → token (client credentials, scopes `products availability`) | Creates its own product per book (category, brand, images, price, GPSR distributor) in batches of 500, waits for the update task and reads the rejected products; then sets stock and the shipping profile | Official, public (OpenAPI) |

All pushers read the same input format and apply the same base rules: skip
sold-out or incomplete products, map the condition, and convert ISBN-10 to
EAN-13 where the API needs it.

## Run flow

1. A scheduler (cron or Windows Task Scheduler) starts
   `bws run -- python push_all.py`.
2. `bws` signs in to Bitwarden with the machine's access token, fetches the
   secrets of the `product-pushers` project, and starts `push_all.py` with them
   as environment variables.
3. `push_all.py` takes a lock file, so a second run can't start while one is
   still going.
4. For each enabled pusher in the config:
   1. If its credentials are missing, it's reported as `NOT CONFIGURED` and not run.
   2. Otherwise, the runner reads the pusher's state file, runs it as a
      separate process with `--yes` and a timeout, then reads the state file
      again.
   3. The result is worked out:
      - **pushed**: new or changed state entries
      - **skipped**: `SKIP` lines in the output
      - **rejected**: `!` lines and batch errors
      - **failed**: a non-zero exit code or a timeout
5. The output is saved to `logs/<date_time>/<pusher>.log`.
6. The report (a plain-text version and an HTML table, with the full log
   attached) is emailed through Gmail.
7. The lock is released. The exit code is `0` (OK), `1` (a pusher failed) or
   `2` (the email couldn't be sent).

Pushers run one after another, not in parallel. That keeps within each
marketplace's rate limits, and one pusher failing doesn't stop the others.

## Data and state

- **Input:** one products JSON (scraper output). The format is shown in
  [products.example.json](../products.example.json).
- **State:** each pusher keeps a JSON file of what it pushed (SKU → listing ID,
  time). This prevents duplicate listings on re-runs, and it's also how the
  runner counts what was pushed.
- **Updates:**
  - `--resend` re-sends price and stock (Casa del Libro, Fnac, AbeBooks, booklooker,
    El Corte Inglés, Carrefour, Worten, IBS.it, Empik, E.Leclerc, Kaufland, bol.com, Cdiscount, Allegro, AliExpress, Temu, TikTok Shop, OTTO Market; Galaxus re-sends its whole feed when anything changed). Vinted, eBay and Amazon only
    create listings so far; their APIs support updates (see
    [capabilities.md](capabilities.md)).
  - AbeBooks can also delete listings for books that are now sold out, and
    Kaufland, bol.com and Cdiscount can set their stock to 0 (`--zero-sold-out`), Allegro can end the offer (`--end-sold-out`), and AliExpress, Temu
    and TikTok Shop can take them offline (`--offline-sold-out`). OTTO Market can do both (`--zero-sold-out`, `--offline-sold-out`). Galaxus keeps sold-out books with stock 0, or leaves them out of the feed (`--drop-sold-out`).
- **Output files:**
  - logs per run
  - the last generated offer file per marketplace (CSV/XML), so you can check
    exactly what was sent

## Credentials and security

| Decision | Reason |
|---|---|
| All credentials live in **Bitwarden Secrets Manager** (project `product-pushers`) | One managed place, team access, access log, change a key without touching the server. The free plan is enough. |
| The server has only one **machine-account access token**, with read-only access to that one project | Smallest possible secret on disk. Each machine can be revoked separately. |
| Credentials reach the scripts only as **environment variables** (`bws run`) | No secrets in code, git or config. The secrets store can be swapped (`.env`, Doppler, Vault) without code changes. |
| Gmail uses an **App Password** on a dedicated account | Revocable without changing the account password; a personal mailbox isn't exposed. |
| Mail is sent only over **TLS** | The runner refuses to send the password to a server without TLS. |
| `.env`, config, state and logs are in **`.gitignore`** | Nothing local or secret reaches GitHub. Every commit is also scanned for secrets before it's pushed. |

The full setup steps are in
[push_all.md → Where credentials are stored](push_all.md#where-credentials-are-stored).

## Deployment

| Item | Value |
|---|---|
| Runtime | Python 3.8 or newer, `requests`, `bws` (Galaxus also `paramiko`) |
| Code | This repository, cloned on the server |
| Configuration on the server | `push_all_config.json` and the Bitwarden access token (file with mode 600 on Linux, or a user environment variable on Windows) |
| Schedule | Once a day, after the scraper. Linux: cron. Windows: Task Scheduler (commands in [push_all.md](push_all.md#running-it-on-a-server)). |
| Input | The scraper must run on the same server, or copy `books.json` to it before the push |

## Error handling and monitoring

| Situation | Behaviour |
|---|---|
| One product is invalid | Skipped with its reason; the others continue |
| A marketplace rejects one offer | Shown in the report with the marketplace's message; not recorded, so it's retried next run |
| Temporary API error (429/5xx, AbeBooks 1xx) | Retried automatically with backoff by the Amazon, Casa del Libro, Fnac, AbeBooks, El Corte Inglés, Carrefour, Worten, IBS.it, Empik, E.Leclerc, Kaufland, bol.com, Cdiscount, Allegro, AliExpress, Temu, TikTok Shop and OTTO Market pushers (Galaxus: the SFTP upload is retried). Vinted and eBay report the error, and the item is retried on the next run. |
| Wrong credentials or a pusher crash | That pusher is `FAILED`, with its last output lines in the email; the others still run |
| A pusher hangs | Killed after `timeout_minutes`, reported as `FAILED` |
| Two runs overlap | The second one stops (lock file) |
| The email can't be sent | Exit code 2, visible in the scheduler; the report is still in `logs/` |
| Bitwarden unreachable | The run doesn't start; visible only in the scheduler or the cron log, since the email password is in Bitwarden too |

## Testing

Every component has an automated test that runs on a single machine, needs no
credentials, and doesn't contact any real API:

| Test | What is faked | Checks |
|---|---|---|
| `test_vinted_api.py` | Vinted Pro API (including HMAC signature verification) | 15 |
| `test_ebay.py` | eBay APIs | 9 |
| `test_amazon.py` | SP-API: Catalog, Listings, Login with Amazon | 21 |
| `test_casadellibro.py` | Mirakl API | 21 |
| `test_fnac.py` | Fnac XML API | 17 |
| `test_abebooks.py` | AbeBooks Inventory Update API | 22 |
| `test_booklooker.py` | booklooker REST API 2.0 (token, account readiness, article file upload, error IDs) | 18 |
| `test_elcorteingles.py` | Mirakl API | 21 |
| `test_carrefour.py` | Mirakl API | 21 |
| `test_worten.py` | Mirakl API | 21 |
| `test_ibs.py` | Mirakl API | 21 |
| `test_empik.py` | Mirakl API (with PLN conversion) | 22 |
| `test_leclerc.py` | Mirakl API | 21 |
| `test_kaufland.py` | Kaufland Seller API (including HMAC signature verification) | 22 |
| `test_bol.py` | bol.com Retailer API, Offers v11 (token login, media types, catalog check, 409 linking, reasons) | 21 |
| `test_cdiscount.py` | Octopia Seller API (token, SellerId, catalog check, offer packages, paginated results) | 21 |
| `test_allegro.py` | Allegro REST API (device flow, rotating refresh token, catalog by GTIN, 201/202 creation, end/activate) | 22 |
| `test_aliexpress.py` | AliExpress Open Platform (signature, tokens, EAN lookup, photo upload, product post) | 26 |
| `test_temu.py` | Temu Partner API (signature, category template, variation, photo upload, goods add, stock/price/sale status) | 22 |
| `test_tiktok.py` | TikTok Shop Partner API (signature, tokens, shop cipher, attributes/rules, GPSR, photo upload, create, stock/price, activate/deactivate) | 28 |
| `test_galaxus.py` | Galaxus SFTP server (real SFTP via paramiko: pinned host key, login, tmp_ upload + rename, retries) and the documented feed import | 22 |
| `test_otto.py` | OTTO Market API (token scopes, Products v5 update tasks, brands/categories, prices, active status, marketplace status, Availability 207 partial failures) | 25 |
| `test_push_all.py` | Fake pushers + fake mail server | 24 |
| **Total** | | **483, all passing** |

The fakes enforce the rules from each marketplace's documentation: required
fields, limits, error codes, and asynchronous processing.

## Current status

| Part | Status |
|---|---|
| All 22 pushers | Built and passing their tests. **Not yet run against the real marketplaces**, because there are no seller accounts or API credentials yet. |
| Runner + email report | Built and tested. **Verified for real on 2026-11-05**: credentials from Bitwarden, test email and dry-run report delivered through Gmail. |
| Bitwarden Secrets Manager | Set up and working (project, machine account, token, `bws` 2.1.0). |
| Server | Not yet deployed. The commands and steps are documented. |

**Next steps:**

1. Get seller accounts and API access for each marketplace.
2. Add their credentials to Bitwarden.
3. Do a safe first run per marketplace: `--dry-run` or `--validate-only`,
   then `--limit 1`.
4. Deploy to the server and schedule it.

**Known constraints:**
- New books in Spain, Portugal, Italy, France, Germany and the Netherlands have legally fixed prices, so
  `--price-factor` must not exceed the cover price.
- Vinted, Amazon and the Mirakl marketplaces process listings asynchronously:
  "pushed" means accepted, and each pusher's `status` command shows the final
  state.
- The El Corte Inglés, Carrefour, Worten, IBS.it, Empik and E.Leclerc API details are based on the Mirakl
  standard, so their first real runs will confirm them.

## Adding a marketplace

1. Write `<name>_push.py`. It should:
   - read the common products JSON
   - read its credentials from environment variables
   - support `--yes` and a safe mode (`--dry-run` or `--validate-only`)
   - record successes in `<name>_posted.json`
   - print `SKIP …` for skipped products and `   ! …` for rejected ones
2. Add an entry to `PUSHERS` in `push_all.py` (script, state file, credential
   names, safe-mode flag) and to `push_all_config.example.json`.
3. Add its secrets to Bitwarden and its names to `.env.example`.
4. Add `tests/test_<name>.py` with a fake of its API, and `docs/<name>_push.md`.
