# What can be automated on each marketplace

Every seller activity, per marketplace: what our scripts already do automatically, what the marketplace API lets us automate next, and what can only be done by hand.

**Legend:** ✅ our scripts do it today · 🟢 the API supports it · 🟢* API must be requested first · 🟡 only part of it (see note) · ✋ only by hand in the portal · — doesn't exist there

| Activity | Vinted | eBay | Amazon EU | Mirakl shops¹ | Fnac | AbeBooks / IberLibro | booklooker | Kaufland | bol.com | Cdiscount | Allegro | AliExpress | Temu | TikTok Shop | Galaxus | OTTO | eMAG | Fruugo | CDON | OnBuy |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Books (listings)** | | | | | | | | | | | | | | | | | | | | |
| Add a book | ✅ push | ✅ push | ✅ push (existing page) | ✅ push | ✅ push | ✅ push | ✅ push | ✅ push | ✅ push | ✅ push | ✅ push | ✅ push (own product) | ✅ push (own product) | ✅ push (own product) | ✅ push (feed by ISBN) | ✅ push (own product) | ✅ push (offer by EAN) | ✅ push (feed, own product) | ✅ push (own article) | ✅ push (offer by EAN) |
| Update price / stock | 🟢 update items | 🟢 Inventory API | 🟢 Listings API | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ next feed | ✅ push --resend | ✅ push --resend | ✅ next feed | ✅ push --resend | ✅ push --resend |
| Remove a book (sold / delisted) | 🟢 delete items | ✅ --zero-sold-out | ✅ --zero-sold-out | ✅ --zero-sold-out | 🟢 offer delete | ✅ --delete-sold-out | ✅ --delete-sold-out | ✅ --zero-sold-out | ✅ --zero-sold-out | ✅ --zero-sold-out | ✅ --end-sold-out | ✅ --offline-sold-out | ✅ --offline-sold-out | ✅ --offline-sold-out | ✅ stock 0 / --drop-sold-out | ✅ --zero / --offline-sold-out | ✅ --zero / --offline-sold-out | ✅ OUTOFSTOCK / NOTAVAILABLE | ✅ --zero / --pause / --delete | ✅ --zero / --delete |
| See if a listing is live / why rejected | ✅ status | 🟢 Inventory API | ✅ status | ✅ status + errors | ✅ status | ✋ seller account | ✅ status | ✅ status + reasons | ✅ status + reasons | ✅ status | ✅ status | ✅ status | ✅ status | ✅ status | 🟡 file only; errors in portal | ✅ status + errors | ✅ status | 🟡 feed URL only; errors in portal | ✅ status + errors | ✅ status + buy box |
| **Sales (orders)** | | | | | | | | | | | | | | | | | | | | |
| See new sales / orders | 🟢 orders API + webhook | 🟢 Fulfillment API | 🟢 Orders API | 🟢 orders API | 🟢 orders API | 🟢* Order Update API | 🟢 order API | 🟢 order units API | 🟢 orders API | 🟢 orders API | 🟢 order events API | 🟢 orders API | 🟢 orders API | 🟢 orders API | 🟢 ORDP files (SFTP) | 🟢 orders API | 🟢 order API + callback | 🟢 Order API (download) | 🟢 orders API | 🟢 orders API |
| Accept / confirm an order | — paid at checkout | — paid at checkout | — paid at checkout | 🟢 accept (required, deadline) | 🟢 accept (required) | 🟢* confirm availability | 🟢 order status API | — paid at checkout | — paid at checkout | 🟢 order validation API | — paid at checkout | — paid at checkout | — paid at checkout | — paid at checkout | 🟢 ORDR file | — paid at checkout | 🟢 acknowledge (required) | 🟢 confirm API | 🟢 accept order API | — paid at checkout |
| Ship: label / tracking / mark shipped | 🟡 label by API; drop-off by hand | 🟢 tracking upload | 🟢 confirm shipment + tracking | 🟢 tracking + mark shipped | 🟢 tracking + mark shipped | 🟢* mark shipped | 🟢 order status API | 🟢 mark sent + tracking | 🟢 shipments API | 🟢 shipping API | 🟢 shipment API | 🟢 logistics API | 🟢 shipment API | 🟢 fulfillment API | 🟢 DELR file + tracking | 🟢 shipments API | 🟢 AWB API | 🟢 ship API + tracking | 🟢 fulfill order API | 🟢 dispatch + tracking API |
| Cancel an order | 🟢 cancel API | 🟢 cancellation API | 🟢 cancel via feed | 🟢 cancel API | 🟢 refuse / cancel | 🟢* reject order | 🟢 order cancel API | 🟢 cancel API | 🟢 cancel API | 🟢 cancel API | 🟢 refund API | ✋ Seller Center | 🟢 cancel API | 🟢 cancel API | 🟢 CANP / CANR files | 🟢 cancellation API | 🟢 order API | 🟢 cancel API | 🟢 cancel order API | 🟢 cancel API |
| Returns / refunds | ✋ in app | 🟢 refund API | 🟡 refund by API; approval by hand | 🟢 refund API | 🟡 incidents API | ✋ seller account | ✋ booklooker.de | 🟢 refund API | 🟢 returns API | 🟡 via orders API | 🟢 returns API | 🟢 disputes API | 🟢 refund API | 🟢 returns API | 🟢 RETP file | 🟢 returns API | 🟢 RMA API | 🟢 return API | ✋ Merchant Center | 🟢 refund API |
| **Customers and leads** | | | | | | | | | | | | | | | | | | | | |
| Read / answer buyer messages and questions | ✋ in app only | 🟢 messages API | 🟡 send only; can't read inbox | 🟢 message threads API | 🟢 messages API | ✋ seller account | 🟢 order message API | 🟢 tickets API | ✋ Seller Dashboard | 🟢 discussions API | 🟢 messaging API | ✋ Seller Center | ✋ Seller Center | 🟢 customer service API | ✋ Partner Portal | ✋ Partner Connect | ✋ Marketplace interface | 🟡 message with order updates only | ✋ Merchant Center | ✋ Seller Control Panel |
| Leads: offers to interested buyers | ✋ buyer offers in app only | 🟢 offers to watchers | — no such feature | — no such feature | — no such feature | ✋ want lists in account | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature | — no such feature |
| Reviews / feedback | ✋ in app | 🟢 read + leave feedback | 🟡 request a review only | ✋ back office | 🟡 read comments | ✋ seller account | 🟢 order rating API | ✋ seller portal | 🟡 read ratings | ✋ seller back office | 🟢 ratings API | 🟢 evaluations API | ✋ Seller Center | ✋ Seller Center | ✋ Partner Portal | ✋ Partner Connect | ✋ Marketplace interface | ✋ Retailer Portal | ✋ Merchant Center | ✋ Seller Control Panel |
| **Money** | | | | | | | | | | | | | | | | | | | | |
| Payouts, fees, sales reports | ✋ in app | 🟢 Finances API | 🟢 Finances API + reports | 🟢 accounting API | ✋ seller portal | ✋ seller account | ✋ booklooker.de | 🟢 sales reports API | 🟢 invoices API | 🟢 financial API | 🟢 billing API | 🟢 settlement API | 🟡 order amounts only | 🟢 finance API | ✋ Partner Portal | 🟢 receipts API | 🟢 invoice API | ✋ Retailer Portal | 🟢 payment reports API | ✋ Seller Control Panel |

¹ Mirakl shops = Casa del Libro, El Corte Inglés, Carrefour, Worten (Portugal + Spain), IBS.it (Italy), Empik (Poland) and E.Leclerc (France). They run on the same platform (Mirakl), so they work the same way.  
* AbeBooks' Order Update API is separate from the listing API and has to be requested from AbeBooks.  
No marketplace has been tested with real seller accounts yet. The table is based on each marketplace's official API documentation (Vinted Pro, eBay, Amazon SP-API, Mirakl, Fnac, Kaufland, AliExpress Open Platform, Temu Partner API, TikTok Shop Partner API, bol.com Retailer API, Octopia Seller API, Allegro REST API, booklooker REST API, Galaxus technical integration, OTTO Market API, eMAG Marketplace API, Fruugo retailer documentation, CDON merchant API, OnBuy API v2).

## Summary

- **Listing books is automatic today** on all 26 marketplaces (add; price/stock updates on the Mirakl shops, Fnac, AbeBooks, Kaufland, bol.com, Cdiscount, Allegro, AliExpress, Temu, TikTok Shop, Galaxus, OTTO, eMAG, Fruugo, CDON and OnBuy).
- **Sales can be managed automatically** almost everywhere: seeing orders, accepting them, tracking numbers and cancellations all have APIs, but they aren't built yet.
- **Vinted is the most manual:** its API covers listings and orders only. Messages, offers, returns and payouts are app-only.
- **Leads** (sending offers to interested buyers) exist only on eBay. On the other marketplaces, buyer interest arrives as messages.

## Suggested next steps to manage sales

- **Stop double sales (built).** **stock_sync.py** reads new orders from the Mirakl shops, Amazon, eBay and Kaufland every 15 minutes and sets the sold book's stock to 0 on every other marketplace; cancelled orders give the stock back. Next: order readers for the remaining marketplaces, and a sold-out option for Fnac and Vinted.
- **One list of all sales.** Collect new orders from every marketplace into one place, and add them to the daily email (or more often): what sold, where, price, buyer address.
- **Auto-accept orders.** Mirakl shops, Fnac, AbeBooks, Cdiscount and eMAG require accepting each order. If it isn't accepted in time, the order is cancelled and the shop's rating drops. This can be automatic when the book is in stock.
- **Shipping.** Send the tracking numbers back to each marketplace automatically. Packing and taking parcels to the carrier stays manual.
- **Messages in one inbox.** Collect buyer messages from eBay, Mirakl, Fnac, Kaufland, Cdiscount, Allegro and TikTok Shop into the email report, so nobody has to check each portal. Vinted, AbeBooks and the Amazon inbox stay manual.

## What stays manual

- Packing and handing parcels to the carrier
- Vinted: messages, buyer offers, returns, payouts (Vinted's API doesn't offer them)
- Amazon: reading buyer messages, approving returns
- AbeBooks: messages, returns, payouts
- Reviews (except eBay)
- Opening seller accounts, contracts, bank details
