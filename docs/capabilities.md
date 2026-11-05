# What can be automated on each marketplace

Every seller activity, per marketplace: what our scripts already do automatically, what the marketplace API lets us automate next, and what can only be done by hand.

**Legend:** ✅ Automatic today: our scripts already do it · 🟢 The marketplace API supports it: we can build it · 🟢* API exists but must be requested from the marketplace / confirmed · 🟡 Only part can be automated (see note) · ✋ Only by hand in the seller portal / app · — Doesn't exist on this marketplace

| Activity | Vinted | eBay | Amazon EU | Mirakl shops¹ | Fnac | AbeBooks / IberLibro | Kaufland |
|---|---|---|---|---|---|---|---|
| **Books (listings)** | | | | | | | |
| Add a book | ✅ push | ✅ push | ✅ push (existing Amazon page) | ✅ push | ✅ push | ✅ push | ✅ push |
| Update price / stock | 🟢 update items | 🟢 Inventory API | 🟢 Listings API | ✅ push --resend | ✅ push --resend | ✅ push --resend | ✅ push --resend |
| Remove a book (sold / delisted) | 🟢 delete items | 🟢 withdraw offer | 🟢 Listings API | 🟢 delete line in import | 🟢 offer delete | ✅ delete, --delete-sold-out | ✅ --zero-sold-out |
| See if a listing is live / why rejected | ✅ status | 🟢 Inventory API | ✅ status | ✅ status + error report | ✅ status | ✋ seller account | ✅ status + reasons |
| **Sales (orders)** | | | | | | | |
| See new sales / orders | 🟢 orders API + 'sold' webhook | 🟢 Fulfillment API | 🟢 Orders API | 🟢 orders API | 🟢 orders API | 🟢* Order Update API | 🟢 order units API |
| Accept / confirm an order | — paid at checkout | — paid at checkout | — paid at checkout | 🟢 accept/refuse (required, has a deadline) | 🟢 accept/refuse (required) | 🟢* confirm availability | — paid at checkout |
| Ship: label / tracking / mark shipped | 🟡 label PDF by API; drop-off by hand | 🟢 tracking upload | 🟢 confirm shipment + tracking | 🟢 tracking + mark shipped | 🟢 tracking + mark shipped | 🟢* mark shipped | 🟢 mark sent + tracking |
| Cancel an order | 🟢 cancel API | 🟢 cancellation API | 🟢 cancel via feed | 🟢 cancel API | 🟢 refuse / cancel | 🟢* reject order | 🟢 cancel API |
| Returns / refunds | ✋ in app | 🟢 returns + refund API | 🟡 refund by API; return approval by hand | 🟢 refund API | 🟡 incidents API | ✋ seller account | 🟢 returns + refund API |
| **Customers and leads** | | | | | | | |
| Read / answer buyer messages and questions | ✋ in app only | 🟢 messages API | 🟡 can send some message types; can't read inbox | 🟢 message threads API | 🟢 messages API | ✋ seller account | 🟢 tickets API |
| Leads: offers to interested buyers | ✋ buyer offers in app only | 🟢 send offers to watchers (Negotiation API) | — no such feature | — no such feature | — no such feature | ✋ want-list matches in account | — no such feature |
| Reviews / feedback | ✋ in app | 🟢 read + leave feedback | 🟡 request a review only | ✋ back office | 🟡 read customer comments | ✋ seller account | ✋ seller portal |
| **Money** | | | | | | | |
| Payouts, fees, sales reports | ✋ in app | 🟢 Finances API | 🟢 Finances API + reports | 🟢 accounting API | ✋ seller portal | ✋ seller account | 🟢 sales reports API |

¹ Mirakl shops = Casa del Libro, El Corte Inglés, Carrefour and Worten (Portugal + Spain). They run on the same platform (Mirakl), so they work the same way.  
* AbeBooks' Order Update API is separate from the listing API and has to be requested from AbeBooks.  
No marketplace has been tested with real seller accounts yet. The table is based on each marketplace's official API documentation (Vinted Pro, eBay, Amazon SP-API, Mirakl, Fnac, Kaufland).

## Summary

- **Listing books is automatic today** on all 10 marketplaces (add; price/stock updates on the Mirakl shops, Fnac, AbeBooks and Kaufland).
- **Sales can be managed automatically** almost everywhere: seeing orders, accepting them, tracking numbers and cancellations all have APIs, but they aren't built yet.
- **Vinted is the most manual:** its API covers listings and orders only. Messages, offers, returns and payouts are app-only.
- **Leads** (sending offers to interested buyers) exist only on eBay. On the other marketplaces, buyer interest arrives as messages.

## Suggested next steps to manage sales

- **Stop double sales.** The same book is listed on up to 10 marketplaces, but there is usually 1 copy. When it sells on one, set its stock to 0 everywhere else, automatically. **This is the most important one**: without it, the same copy can be sold twice.
- **One list of all sales.** Collect new orders from every marketplace into one place, and add them to the daily email (or more often): what sold, where, price, buyer address.
- **Auto-accept orders.** Mirakl shops, Fnac and AbeBooks require accepting each order. If it isn't accepted in time, the order is cancelled and the shop's rating drops. This can be automatic when the book is in stock.
- **Shipping.** Send the tracking numbers back to each marketplace automatically. Packing and taking parcels to the carrier stays manual.
- **Messages in one inbox.** Collect buyer messages from eBay, Mirakl, Fnac and Kaufland into the email report, so nobody has to check each portal. Vinted, AbeBooks and the Amazon inbox stay manual.

## What stays manual

- Packing and handing parcels to the carrier
- Vinted: messages, buyer offers, returns, payouts (Vinted's API doesn't offer them)
- Amazon: reading buyer messages, approving returns
- AbeBooks: messages, returns, payouts
- Reviews (except eBay)
- Opening seller accounts, contracts, bank details
