# stock_sync.py reference: stop double sales

The same book is listed on many marketplaces at once:

- eBay on 11 sites and Kaufland on 9 storefronts
- Amazon, and the 7 Mirakl shops
- and so on

But the source shop usually has **one copy**. When it sells on one
marketplace, every other marketplace must stop selling it, quickly, or the
same copy is sold twice. That means a cancelled order, a bad rating and
possibly a suspended seller account.

`stock_sync.py` keeps **one stock for every marketplace**. Everything below
describes how it behaves today.

- [How it works](#how-it-works)
- [Which marketplaces](#which-marketplaces)
- [Setup](#setup)
- [Commands](#commands)
- [Running it regularly](#running-it-regularly)
- [Files](#files)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

1. **Read new orders** from each marketplace's order API, since the last run.
   Each run re-reads the last 60 minutes, so late changes aren't missed. The
   first run reads the last 72 hours. Each order line is stored once in the
   ledger (`stock_ledger.json`), whatever the overlap. **Cancelled or refused
   orders give their stock back.**
2. **Compute the stock left** for each book. That's the stock in `books.json`
   from the scraper (1 if the book has no stock field) minus **our sales that
   the source shop doesn't reflect yet**.

   A sale counts until a scraper run that is at least `SETTLE_HOURS` (24 h)
   newer than the sale. By then the source shop's stock (and so `books.json`)
   includes it.
3. **Write `books_synced.json`.** It's `books.json` with that stock, and
   `sold_out: true` where nothing is left.
4. **Update every marketplace.** Every pusher enabled in
   `push_all_config.json` runs with its own sold-out option, its usual
   options from the config, and `--yes`, for **only the books whose stock
   changed** (`books_sync_changes.json`):

   | Pushers | Option |
   |---|---|
   | Mirakl shops, Amazon, eBay, Kaufland, bol.com, Cdiscount, OTTO, eMAG, CDON, OnBuy | `--zero-sold-out` |
   | AbeBooks, booklooker | `--delete-sold-out` |
   | Allegro | `--end-sold-out` |
   | AliExpress, Temu, TikTok Shop | `--offline-sold-out` |
   | Galaxus, Fruugo (feeds) | the full synced file; sold-out books get stock 0 / `OUTOFSTOCK` in the next feed |

   When stock comes back, or drops but isn't 0, `--resend` is added for
   pushers that support it. That puts the stock back, for example after a
   cancellation.
5. **Failures are retried.** If a pusher fails, the same stock changes are
   sent again on the next run. A marketplace whose orders can't be read is
   reported, and the others still run.

## Which marketplaces

**Orders are read from:**

| Channel | API | Notes |
|---|---|---|
| Casa del Libro, El Corte Inglés, Carrefour, Worten, IBS.it, Empik, E.Leclerc | Mirakl OR11 `GET /api/orders` (`start_update_date`) | Refused / cancelled lines give stock back |
| Amazon (every EU store + UK) | Orders API **v2026-01-01** `GET /orders/2026-01-01/orders` (`lastUpdatedAfter`). v0 is deprecated. | `CANCELLED` gives stock back |
| eBay (all sites) | Fulfillment API `GET /sell/fulfillment/v1/order` (`lastmodifieddate`) | Needs the `sell.fulfillment` scope (see Setup). `CANCELED` gives stock back. |
| Kaufland (all 9 storefronts) | `GET /v2/order-units` (`ts_updated_from_iso`) | One order unit = one copy; cancelled units give stock back |
| bol.com | Retailer API v10 `GET /retailer/orders` (`status=ALL`, `change-interval-minute=60`; after a gap of over 55 minutes, `latest-change-date`) | Reports the **EAN**, which the sync matches to the book with that ISBN. `quantityCancelled` is taken off. |
| Cdiscount | Octopia `GET /orders` (`updatedAtMin`, `pageSize` 100) | `offer.sellerProductId` (EAN as fallback). `Cancelled` / `Refused` lines and orders give stock back. |
| Allegro | `GET /order/checkout-forms` (`updatedAt.gte`, `limit` 100) | `offer.external.id` is our SKU. `CANCELLED` gives stock back. |
| OTTO | Orders API v4 `GET /v4/orders` (`fromDate`, cursor paging) | One position item = one copy. `CANCELLED_BY_*` gives stock back. Needs the **orders** interface in your self-app. |
| eMAG | `order/read` (`modifiedAfter` + `modifiedBefore`, max 1 month) | `product_id` is mapped back through `emag_ids.json`. Status 0 (order or line) gives stock back. |
| CDON | `GET /v1/orders` per state (the API has no date filter) | One order = one SKU (`article_sku`). `NOT_FULFILLED` gives stock back. |
| OnBuy (every site you use) | `GET /v2/orders` (`filter[status]=all`, `filter[modified_since]`) | `products[].sku`. Cancelled statuses give stock back. |
| booklooker | `GET /2.0/order` (`dateFrom` / `dateTo`, max 1 month) | `orderNo` is our SKU (Bestell-Nr). `Canceled` items give stock back. |
| Fruugo | Order API `GET /orders/download?from=` (XML) | `skuId`; `cancelledItems` are taken off. Needs the Retailer Portal login (`FRUUGO_USERNAME`, `FRUUGO_PASSWORD`). |
| Vinted Pro | `GET /api/v1/orders?after-id=` and `GET /api/v1/orders/{id}` | `item_reference` is our SKU. `CANCELED` gives stock back. Vinted Pro is only for allowlisted businesses. |
| Temu | `bg.order.list.v2.get` (`updateAtStart` / `updateAtEnd`) | `extCode`, or the Temu SKU id mapped back through `temu_posted.json`. `canceledQuantityBeforeShipment` is taken off. Field names come from a mirror of Temu's partner docs. |
| TikTok Shop | Order API 202309 `POST /order/202309/orders/search` (`update_time_ge`) | `seller_sku` is mapped back through `tiktok_posted.json`; one line item = one copy. `CANCELLED` gives stock back. **TikTok's docs can't be read without a browser, so these field names aren't verified.** |

**Stock is taken down on** every enabled marketplace with a sold-out option
(the table in step 4). This includes marketplaces whose orders aren't read
yet: a book that sells on Amazon is taken down on bol.com too.

**Not yet:**

- **Orders aren't read** from these marketplaces yet. A sale there only
  reaches the others once the source shop's stock changes and the scraper
  runs:
  - **AbeBooks:** its Order Update API has to be requested from AbeBooks
    first.
  - **Fnac:** the order API docs need a partner login.
  - **AliExpress:** the docs can't be read without a browser.
  - **Galaxus:** orders arrive as ORDP files over SFTP.

  Adding a reader is one function in `stock_sync.py`, see `readers()`.
- **Fnac and Vinted** have no sold-out option yet, so the sync reports them
  as `CANNOT SYNC`.

## Orders: accept automatically, email report

The orders the sync reads are also handled.

**Who gets the copy.** For every book, the earliest orders get the available
copies. A later order beyond the stock is a **DOUBLE SALE**. It is never
accepted, and the email shows it so you can cancel it or find another copy.

**Automatic accept** (`AUTO_ACCEPT = True`), only for orders the stock covers:

| Marketplace | Call |
|---|---|
| Mirakl shops (Casa del Libro, El Corte Inglés, Carrefour, Worten, IBS.it, Empik, E.Leclerc) | OR21 `PUT /api/orders/{id}/accept`, every waiting line `accepted: true` |
| Cdiscount (Octopia) | `POST /orders/{id}/approval-status` with `{"approval_status": "Accepted"}` |
| eMAG | `order/acknowledge/{id}` (the order goes to "in progress") |

An order whose lines aren't all covered is not accepted, since Mirakl
requires a decision for every line. A failed accept is tried again on the
next run, and is shown in the email until it works.

**By hand:**

- **CDON:** orders need accepting, but the accept call isn't built yet.
- **Fnac and AbeBooks:** their orders aren't read yet.

**Email.** `stock_sync.py` writes `orders_report.json`, and push_all adds an
**Orders** section to the report. It lists every new order (marketplace,
order, book, status), with the ones that need you first and in red. Those
are double sales, orders to accept by hand, and failed accepts. The subject
then says "N order(s) need you", and the email is sent even with
`"when": "problems"`.

**Clearing the list.** Orders are marked as reported only after the email was
actually sent. Orders that still need you stay in every email until they're
handled or cancelled.

## Setup

1. **Credentials:** the sync uses the pushers' own credentials (Bitwarden /
   `.env`). Channels without credentials are skipped.
2. **eBay:** the refresh token must also have the scope
   `https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly`, or
   `sell.fulfillment`. If yours was made with only `sell.inventory` and
   `sell.account`, make a new one in the eBay developer portal (User Tokens)
   that includes it.
3. **Amazon:** the SP-API app needs the **Inventory and Order Tracking** role
   to read orders.
4. **Turn it on in push_all:** in `push_all_config.json`, set
   ```json
   "stock_sync": {"enabled": true}
   ```
   `push_all.py` then runs the sync first, shows it as **Stock sync** in the
   report and the email, and the pushers list `books_synced.json`. That way
   the daily run never re-lists a copy that has already been sold.
5. **Settings** at the top of `stock_sync.py`:

   | Setting | Default | Meaning |
   |---|---|---|
   | `SETTLE_HOURS` | 24 | A sale counts until `books.json` is this much newer than the sale |
   | `LOOKBACK_HOURS` | 72 | First run: read orders this far back |
   | `OVERLAP_MINUTES` | 60 | Window read again on every run |
   | `KEEP_DAYS` | 60 | Old sales are dropped from the ledger after this |
   | `DEFAULT_STOCK` | 1 | Stock of a book without a stock field |
| `AUTO_ACCEPT` | `True` | Accept Mirakl / Cdiscount / eMAG orders automatically when the stock covers them |

## Commands

```powershell
bws run -- python stock_sync.py sync              # read orders, update stock everywhere
bws run -- python stock_sync.py sync --dry-run    # pushers run in their safe mode; ledger not saved
bws run -- python stock_sync.py sync --no-apply   # read orders, write books_synced.json only
bws run -- python stock_sync.py sync --verbose    # also show every pusher's output
python stock_sync.py status                       # cursors, sales that still count, stock sent
```

`--source FILE` uses another products file. The default is `json_file` from
`push_all_config.json`. The sync and `push_all.py` share one lock, so they
never run at the same time.

## Running it regularly

The daily `push_all.py` run is too slow to stop double sales. Run the sync on
its own **every 15 minutes**. Amazon's order search allows a burst of 20
calls, refilled about once every 3 minutes, which is enough for that.

```powershell
# Windows Task Scheduler (every 15 minutes)
schtasks /Create /SC MINUTE /MO 15 /TN "Stock sync" /TR "cmd /c cd /d C:\programming\product pushers && bws run -- python stock_sync.py sync"
```

```bash
# Linux server (crontab -e)
*/15 * * * * cd /srv/product-pushers && BWS_ACCESS_TOKEN="$(cat /etc/product-pushers.token)" bws run -- python3 stock_sync.py sync >> logs/stock_sync.log 2>&1
```

## Files

| File | Content |
|---|---|
| `stock_ledger.json` | Per channel, the time orders were read up to; every order line (SKU, quantity, cancelled, time); the stock each book last has on the marketplaces |
| `books_synced.json` | `books.json` with our sales taken off |
| `books_sync_changes.json` | Only the books whose stock changed in the last run (what the pushers got) |
| `orders_report.json` | New orders and the ones that need you, for push_all's email |

All four are in `.gitignore`. **Don't delete `stock_ledger.json`**: the
sync would forget which sales were already counted.

## Limitations

- **Order readers for 22 of the 26 marketplaces.** AbeBooks, Fnac,
  AliExpress and Galaxus are still missing, see
  [Which marketplaces](#which-marketplaces).
- **Between runs** (up to 15 minutes), a copy can still sell twice. Only an
  instant notification from every marketplace would close that gap.
- **The source shop must be updated.** When we sell a copy, the source shop's
  stock has to go down, within `SETTLE_HOURS` and before the scraper runs
  again. Otherwise the sync gives the stock back when the new `books.json`
  arrives.
- **Not tested against the real order APIs**, because there are no seller
  accounts yet. Only the local mocks below have been used.

## Testing

```powershell
python tests\test_stock_sync.py
```

The test starts mocks of the four order APIs on a free local port:

- Mirakl OR11 with offset pagination
- Amazon `searchOrders` with its login and `nextToken` pages
- eBay `getOrders`, with a token checked for the `sell.fulfillment` scope
- Kaufland order units per storefront

It records the pusher command lines instead of running the pushers. It checks:

- sales counted once across overlapping runs, refused and cancelled orders
- partial stock (2 → 1)
- the changed-books file, the flags per pusher, and the full file for feeds
- a failing pusher (the change is sent again), a failing channel (the
  others still run)
- the settle rule, `status`, and push_all running the sync first

There are 26 checks. The pushers' own `--zero-sold-out` handling is tested in
their tests: Mirakl ×7, Amazon and eBay.

```powershell
python tests\test_order_readers.py
```

This test covers the other 12 order readers. Each marketplace client is
replaced by a fake that answers like that marketplace's documented order API,
and Fruugo is read over HTTP from a local XML mock. It checks:

- the filters and paging each reader sends
- SKU, EAN and ID mapping
- quantities and partial cancellations
- that a bol.com sale (EAN only) takes the right book down

There are 27 checks.

```powershell
python tests\test_orders.py
```

This test covers order handling. It uses a local Mirakl mock for the real
OR21 call, and fakes for Cdiscount and eMAG. It checks:

- the automatic accept, and that a double sale is never accepted
- eMAG's acknowledge
- CDON flagged for acceptance by hand
- a failed accept that's retried
- the dry run
- the email section and subject
- marking orders as reported after the email

There are 12 checks.
