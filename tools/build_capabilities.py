"""
Builds docs/marketplace_capabilities.pdf (2 pages) and docs/capabilities.md from
the same data: what is automated per marketplace and activity, and the plan.
Edit the table data (GROUPS and the per-marketplace columns) and texts here,
never the generated files.

    pip install -r tools/requirements.txt
    python tools/build_capabilities.py        # writes both files in docs/
"""
import sys
from pathlib import Path
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak)

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------ data
# B = built (automatic today), A = API exists (can be automated, not built yet),
# C = API exists but must be requested/confirmed, P = partly possible, M = manual only, N = not applicable
LEGEND = {
    "B": ("AUTOMATIC", "our scripts do it today"),
    "A": ("CAN AUTOMATE", "the API supports it"),
    "C": ("CAN AUTOMATE*", "API must be requested first"),
    "P": ("PARTLY", "only part of it (see note)"),
    "M": ("MANUAL", "only by hand in the portal"),
    "N": ("N/A", "doesn't exist there"),
}
MD_ICON = {"B": "✅", "A": "🟢", "C": "🟢*", "P": "🟡", "M": "✋", "N": "—"}

MARKETS = ["Vinted", "eBay", "Amazon EU", "Mirakl shops¹", "Fnac", "AbeBooks / IberLibro", "Kaufland"]

GROUPS = [
    ("Books (listings)", [
        ("Add a book", [("B", "push"), ("B", "push"), ("B", "push (existing page)"), ("B", "push"),
                        ("B", "push"), ("B", "push"), ("B", "push")]),
        ("Update price / stock", [("A", "update items"), ("A", "Inventory API"), ("A", "Listings API"),
                                 ("B", "push --resend"), ("B", "push --resend"), ("B", "push --resend"),
                                 ("B", "push --resend")]),
        ("Remove a book (sold / delisted)", [("A", "delete items"), ("B", "--zero-sold-out"), ("B", "--zero-sold-out"),
                                            ("B", "--zero-sold-out"), ("A", "offer delete"),
                                            ("B", "--delete-sold-out"), ("B", "--zero-sold-out")]),
        ("See if a listing is live / why rejected", [("B", "status"), ("A", "Inventory API"), ("B", "status"),
                                                    ("B", "status + errors"), ("B", "status"),
                                                    ("M", "seller account"), ("B", "status + reasons")]),
    ]),
    ("Sales (orders)", [
        ("See new sales / orders", [("A", "orders API + webhook"), ("A", "Fulfillment API"), ("A", "Orders API"),
                                   ("A", "orders API"), ("A", "orders API"), ("C", "Order Update API"),
                                   ("A", "order units API")]),
        ("Accept / confirm an order", [("N", "paid at checkout"), ("N", "paid at checkout"), ("N", "paid at checkout"),
                                      ("A", "accept (required, deadline)"),
                                      ("A", "accept (required)"), ("C", "confirm availability"),
                                      ("N", "paid at checkout")]),
        ("Ship: label / tracking / mark shipped", [("P", "label by API; drop-off by hand"),
                                                  ("A", "tracking upload"), ("A", "confirm shipment + tracking"),
                                                  ("A", "tracking + mark shipped"), ("A", "tracking + mark shipped"),
                                                  ("C", "mark shipped"), ("A", "mark sent + tracking")]),
        ("Cancel an order", [("A", "cancel API"), ("A", "cancellation API"), ("A", "cancel via feed"),
                            ("A", "cancel API"), ("A", "refuse / cancel"), ("C", "reject order"), ("A", "cancel API")]),
        ("Returns / refunds", [("M", "in app"), ("A", "refund API"),
                              ("P", "refund by API; approval by hand"), ("A", "refund API"),
                              ("P", "incidents API"), ("M", "seller account"), ("A", "refund API")]),
    ]),
    ("Customers and leads", [
        ("Read / answer buyer messages and questions", [("M", "in app only"), ("A", "messages API"),
                                                       ("P", "send only; can't read inbox"),
                                                       ("A", "message threads API"), ("A", "messages API"),
                                                       ("M", "seller account"), ("A", "tickets API")]),
        ("Leads: offers to interested buyers", [("M", "buyer offers in app only"),
                                               ("A", "offers to watchers"),
                                               ("N", "no such feature"), ("N", "no such feature"),
                                               ("N", "no such feature"), ("M", "want lists in account"),
                                               ("N", "no such feature")]),
        ("Reviews / feedback", [("M", "in app"), ("A", "read + leave feedback"), ("P", "request a review only"),
                               ("M", "back office"), ("P", "read comments"), ("M", "seller account"),
                               ("M", "seller portal")]),
    ]),
    ("Money", [
        ("Payouts, fees, sales reports", [("M", "in app"), ("A", "Finances API"), ("A", "Finances API + reports"),
                                         ("A", "accounting API"), ("M", "seller portal"), ("M", "seller account"),
                                         ("A", "sales reports API")]),
    ]),
]

FOOTNOTES = [
    "¹ Mirakl shops = Casa del Libro, El Corte Inglés, Carrefour, Worten (Portugal + Spain), IBS.it (Italy), Empik (Poland) and E.Leclerc (France). They run on the same platform (Mirakl), so they "
    "work the same way.",
    "* AbeBooks' Order Update API is separate from the listing API and has to be requested from AbeBooks.",
    "No marketplace has been tested with real seller accounts yet. The table is based on each marketplace's "
    "official API documentation (Vinted Pro, eBay, Amazon SP-API, Mirakl, Fnac, Kaufland, AliExpress Open Platform, Temu Partner API, TikTok Shop Partner API, bol.com Retailer API, Octopia Seller API, Allegro REST API, booklooker REST API, Galaxus technical integration, OTTO Market API, eMAG Marketplace API, Fruugo retailer documentation, CDON merchant API, OnBuy API v2).",
]

PLAN = [
    ("1. Stop double sales (built)", "<b>stock_sync.py</b> reads new orders from 22 marketplaces every 15 minutes "
     "and sets the sold book's stock to 0 on every other marketplace; cancelled orders give the stock back. "
     "Still to add: order readers for AbeBooks, Fnac, AliExpress and Galaxus, and a sold-out option for Fnac "
     "and Vinted."),
    ("2. One list of all sales (built)", "The daily email has an <b>Orders</b> section: every new order from "
     "22 marketplaces, and the ones that need a person (double sales, orders to accept by hand) first."),
    ("3. Auto-accept orders (built)", "Mirakl shops, Cdiscount and eMAG orders are accepted automatically when the "
     "stock covers them; a double sale is never accepted. Fnac, AbeBooks and CDON orders still have to be accepted "
     "by hand (shown in the email)."),
    ("4. Shipping", "Send the tracking numbers back to each marketplace automatically. Packing and "
     "taking parcels to the carrier stays manual."),
    ("5. Messages in one inbox", "Collect buyer messages from eBay, Mirakl, Fnac, Kaufland, Cdiscount, Allegro and TikTok Shop into the email "
     "report, so nobody has to check each portal. Vinted, AbeBooks and the Amazon inbox stay manual."),
]
STAYS_MANUAL = [
    "Packing and handing parcels to the carrier",
    "Vinted: messages, buyer offers, returns, payouts (Vinted's API doesn't offer them)",
    "Amazon: reading buyer messages, approving returns",
    "AbeBooks: messages, returns, payouts",
    "Reviews (except eBay)",
    "Opening seller accounts, contracts, bank details",
]

# AliExpress column (Open Platform API), one cell per activity row above, in order.
ALIEXPRESS = [("B", "push (own product)"), ("B", "push --resend"), ("B", "--offline-sold-out"),
              ("B", "status"),
              ("A", "orders API"), ("N", "paid at checkout"), ("A", "logistics API"),
              ("M", "Seller Center"), ("A", "disputes API"),
              ("M", "Seller Center"), ("N", "no such feature"), ("A", "evaluations API"),
              ("A", "settlement API")]
_cells = [cells for _, rows in GROUPS for _, cells in rows]
assert len(_cells) == len(ALIEXPRESS)
for cells, cell_ in zip(_cells, ALIEXPRESS):
    cells.append(cell_)
MARKETS.append("AliExpress")

# Temu column (Partner API), same row order.
TEMU = [("B", "push (own product)"), ("B", "push --resend"), ("B", "--offline-sold-out"),
        ("B", "status"),
        ("A", "orders API"), ("N", "paid at checkout"), ("A", "shipment API"),
        ("A", "cancel API"), ("A", "refund API"),
        ("M", "Seller Center"), ("N", "no such feature"), ("M", "Seller Center"),
        ("P", "order amounts only")]
for cells, cell_ in zip(_cells, TEMU):
    cells.append(cell_)
MARKETS.append("Temu")

# TikTok Shop column (Partner API), same row order.
TIKTOK = [("B", "push (own product)"), ("B", "push --resend"), ("B", "--offline-sold-out"), ("B", "status"),
          ("A", "orders API"), ("N", "paid at checkout"), ("A", "fulfillment API"),
          ("A", "cancel API"), ("A", "returns API"),
          ("A", "customer service API"), ("N", "no such feature"), ("M", "Seller Center"),
          ("A", "finance API")]
for cells, cell_ in zip(_cells, TIKTOK):
    cells.append(cell_)
MARKETS.append("TikTok Shop")

# bol.com column (Retailer API), same row order; inserted after Kaufland.
BOL = [("B", "push"), ("B", "push --resend"), ("B", "--zero-sold-out"), ("B", "status + reasons"),
       ("A", "orders API"), ("N", "paid at checkout"), ("A", "shipments API"),
       ("A", "cancel API"), ("A", "returns API"),
       ("M", "Seller Dashboard"), ("N", "no such feature"), ("P", "read ratings"),
       ("A", "invoices API")]
_k = MARKETS.index("Kaufland") + 1
for cells, cell_ in zip(_cells, BOL):
    cells.insert(_k, cell_)
MARKETS.insert(_k, "bol.com")

# Cdiscount column (Octopia Seller API), same row order; inserted after bol.com.
CDISCOUNT = [("B", "push"), ("B", "push --resend"), ("B", "--zero-sold-out"), ("B", "status"),
             ("A", "orders API"), ("A", "order validation API"), ("A", "shipping API"),
             ("A", "cancel API"), ("P", "via orders API"),
             ("A", "discussions API"), ("N", "no such feature"), ("M", "seller back office"),
             ("A", "financial API")]
_k = MARKETS.index("bol.com") + 1
for cells, cell_ in zip(_cells, CDISCOUNT):
    cells.insert(_k, cell_)
MARKETS.insert(_k, "Cdiscount")

# Allegro column (REST API), same row order; inserted after Cdiscount.
ALLEGRO = [("B", "push"), ("B", "push --resend"), ("B", "--end-sold-out"), ("B", "status"),
           ("A", "order events API"), ("N", "paid at checkout"), ("A", "shipment API"),
           ("A", "refund API"), ("A", "returns API"),
           ("A", "messaging API"), ("N", "no such feature"), ("A", "ratings API"),
           ("A", "billing API")]
_k = MARKETS.index("Cdiscount") + 1
for cells, cell_ in zip(_cells, ALLEGRO):
    cells.insert(_k, cell_)
MARKETS.insert(_k, "Allegro")

# booklooker column (REST API 2.0), same row order; inserted after AbeBooks.
BOOKLOOKER = [("B", "push"), ("B", "push --resend"), ("B", "--delete-sold-out"), ("B", "status"),
              ("A", "order API"), ("A", "order status API"), ("A", "order status API"),
              ("A", "order cancel API"), ("M", "booklooker.de"),
              ("A", "order message API"), ("N", "no such feature"), ("A", "order rating API"),
              ("M", "booklooker.de")]
_k = MARKETS.index("AbeBooks / IberLibro") + 1
for cells, cell_ in zip(_cells, BOOKLOOKER):
    cells.insert(_k, cell_)
MARKETS.insert(_k, "booklooker")

# Galaxus column (data feed + XML documents over SFTP), same row order; appended at the end.
GALAXUS = [("B", "push (feed by ISBN)"), ("B", "next feed"), ("B", "stock 0 / --drop-sold-out"),
           ("P", "file only; errors in portal"),
           ("A", "ORDP files (SFTP)"), ("A", "ORDR file"), ("A", "DELR file + tracking"),
           ("A", "CANP / CANR files"), ("A", "RETP file"),
           ("M", "Partner Portal"), ("N", "no such feature"), ("M", "Partner Portal"),
           ("M", "Partner Portal")]
for cells, cell_ in zip(_cells, GALAXUS):
    cells.append(cell_)
MARKETS.append("Galaxus")

# OTTO Market column (Products v5 / Availability / Orders APIs), same row order; appended at the end.
OTTO = [("B", "push (own product)"), ("B", "push --resend"), ("B", "--zero / --offline-sold-out"),
        ("B", "status + errors"),
        ("A", "orders API"), ("N", "paid at checkout"), ("A", "shipments API"),
        ("A", "cancellation API"), ("A", "returns API"),
        ("M", "Partner Connect"), ("N", "no such feature"), ("M", "Partner Connect"),
        ("A", "receipts API")]
for cells, cell_ in zip(_cells, OTTO):
    cells.append(cell_)
MARKETS.append("OTTO")

# eMAG column (Marketplace API v4.5), same row order; appended at the end.
EMAG = [("B", "push (offer by EAN)"), ("B", "push --resend"), ("B", "--zero / --offline-sold-out"),
        ("B", "status"),
        ("A", "order API + callback"), ("A", "acknowledge (required)"), ("A", "AWB API"),
        ("A", "order API"), ("A", "RMA API"),
        ("M", "Marketplace interface"), ("N", "no such feature"), ("M", "Marketplace interface"),
        ("A", "invoice API")]
for cells, cell_ in zip(_cells, EMAG):
    cells.append(cell_)
MARKETS.append("eMAG")

# Fruugo column (CSV product feed at a static URL + Order API), same row order; appended at the end.
FRUUGO = [("B", "push (feed, own product)"), ("B", "next feed"), ("B", "OUTOFSTOCK / NOTAVAILABLE"),
          ("P", "feed URL only; errors in portal"),
          ("A", "Order API (download)"), ("A", "confirm API"), ("A", "ship API + tracking"),
          ("A", "cancel API"), ("A", "return API"),
          ("P", "message with order updates only"), ("N", "no such feature"), ("M", "Retailer Portal"),
          ("M", "Retailer Portal")]
for cells, cell_ in zip(_cells, FRUUGO):
    cells.append(cell_)
MARKETS.append("Fruugo")

# CDON column (merchant API, new platform), same row order; appended at the end.
CDON = [("B", "push (own article)"), ("B", "push --resend"), ("B", "--zero / --pause / --delete"),
        ("B", "status + errors"),
        ("A", "orders API"), ("A", "accept order API"), ("A", "fulfill order API"),
        ("A", "cancel order API"), ("M", "Merchant Center"),
        ("M", "Merchant Center"), ("N", "no such feature"), ("M", "Merchant Center"),
        ("A", "payment reports API")]
for cells, cell_ in zip(_cells, CDON):
    cells.append(cell_)
MARKETS.append("CDON")

# OnBuy column (OnBuy / OnCommerce API v2), same row order; appended at the end.
ONBUY = [("B", "push (offer by EAN)"), ("B", "push --resend"), ("B", "--zero / --delete"),
         ("B", "status + buy box"),
         ("A", "orders API"), ("N", "paid at checkout"), ("A", "dispatch + tracking API"),
         ("A", "cancel API"), ("A", "refund API"),
         ("M", "Seller Control Panel"), ("N", "no such feature"), ("M", "Seller Control Panel"),
         ("M", "Seller Control Panel")]
for cells, cell_ in zip(_cells, ONBUY):
    cells.append(cell_)
MARKETS.append("OnBuy")

# What stock_sync.py does with orders today (overrides the cells above).
_READ = ["Vinted", "eBay", "Amazon EU", "Mirakl shops¹", "booklooker", "Kaufland", "bol.com", "Cdiscount",
         "Allegro", "Temu", "TikTok Shop", "OTTO", "eMAG", "Fruugo", "CDON", "OnBuy"]
_OVERRIDES = {
    "See new sales / orders": {m: ("B", "stock sync + email") for m in _READ},
    "Accept / confirm an order": {"Mirakl shops¹": ("B", "auto-accept if in stock"),
                                  "Cdiscount": ("B", "auto-accept if in stock"),
                                  "eMAG": ("B", "auto-acknowledge if in stock")},
}
for _g, _rows in GROUPS:
    for _act, _cells_row in _rows:
        for _i, _m in enumerate(MARKETS):
            if _m in _OVERRIDES.get(_act, {}):
                _cells_row[_i] = _OVERRIDES[_act][_m]

# ------------------------------------------------------------------ markdown
def build_md():
    out = ["# What can be automated on each marketplace", "",
           "Every seller activity, per marketplace: what our scripts already do automatically, what "
           "the marketplace API lets us automate next, and what can only be done by hand.", "",
           "**Legend:** " + " · ".join(f"{MD_ICON[k]} {v[1]}" for k, v in LEGEND.items()), ""]
    out.append("| Activity | " + " | ".join(MARKETS) + " |")
    out.append("|---|" + "---|" * len(MARKETS))
    for gname, rows in GROUPS:
        out.append(f"| **{gname}** |" + " |" * len(MARKETS))
        for act, cells in rows:
            out.append(f"| {act} | " + " | ".join(f"{MD_ICON[c]} {n}" for c, n in cells) + " |")
    out += ["", f"{FOOTNOTES[0]}  ", f"{FOOTNOTES[1]}  ", FOOTNOTES[2]]
    out += ["", "## Summary", "",
            "- **Listing books is automatic today** on all 26 marketplaces (add; price/stock updates on the "
            "Mirakl shops, Fnac, AbeBooks, Kaufland, bol.com, Cdiscount, Allegro, AliExpress, Temu, TikTok Shop, Galaxus, OTTO, eMAG, Fruugo, CDON and OnBuy).",
            "- **Sales can be managed automatically** almost everywhere: seeing orders, accepting them, "
            "tracking numbers and cancellations all have APIs, but they aren't built yet.",
            "- **Vinted is the most manual:** its API covers listings and orders only. Messages, offers, returns "
            "and payouts are app-only.",
            "- **Leads** (sending offers to interested buyers) exist only on eBay. On the other marketplaces, "
            "buyer interest arrives as messages.", "",
            "## Suggested next steps to manage sales", ""]
    for t, d in PLAN:
        out.append(f"- **{t[3:]}.** " + d.replace("<b>", "**").replace("</b>", "**"))
    out += ["", "## What stays manual", ""] + [f"- {s}" for s in STAYS_MANUAL] + [""]
    (REPO / "docs" / "capabilities.md").write_text("\n".join(out), encoding="utf-8")


# ------------------------------------------------------------------ pdf
def register_fonts():
    """Segoe UI on Windows (how the committed PDFs were made); DejaVu / Liberation Sans elsewhere."""
    candidates = [
        (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ]
    for regular, bold in candidates:
        if Path(regular).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("UI", regular))
            pdfmetrics.registerFont(TTFont("UIB", bold))
            pdfmetrics.registerFontFamily("UI", normal="UI", bold="UIB", italic="UI", boldItalic="UIB")
            return
    sys.exit("No usable font found (Segoe UI, DejaVu Sans or Liberation Sans). "
             "On Linux: sudo apt install fonts-dejavu-core")


register_fonts()
NAVY = colors.HexColor("#1F2A44"); INK = colors.HexColor("#1E293B"); MUTED = colors.HexColor("#64748B")
LINE = colors.HexColor("#CBD5E1"); SOFT = colors.HexColor("#F1F5F9")
STYLE = {  # code -> (text colour, background)
    "B": (colors.HexColor("#15803D"), colors.HexColor("#DCFCE7")),
    "A": (colors.HexColor("#1D4ED8"), colors.HexColor("#DBEAFE")),
    "C": (colors.HexColor("#1D4ED8"), colors.HexColor("#EFF6FF")),
    "P": (colors.HexColor("#B45309"), colors.HexColor("#FEF3C7")),
    "M": (colors.HexColor("#B91C1C"), colors.HexColor("#FEE2E2")),
    "N": (colors.HexColor("#64748B"), colors.HexColor("#F1F5F9")),
}
PAGE = landscape(A4); W, H = PAGE; M = 13 * mm; FW = W - 2 * M
sH1 = ParagraphStyle("h1", fontName="UIB", fontSize=17, leading=20, textColor=NAVY, spaceAfter=4)
sSub = ParagraphStyle("sub", fontName="UI", fontSize=9, leading=12, textColor=MUTED, spaceAfter=5)
sH2 = ParagraphStyle("h2", fontName="UIB", fontSize=12, leading=15, textColor=NAVY, spaceBefore=4, spaceAfter=4)
sBody = ParagraphStyle("b", fontName="UI", fontSize=9.2, leading=12.5, textColor=INK)
sAct = ParagraphStyle("a", fontName="UIB", fontSize=7.8, leading=9.4, textColor=INK)
sHead = ParagraphStyle("hd", fontName="UIB", fontSize=6.9, leading=8.0, textColor=colors.white, alignment=1)
sGroup = ParagraphStyle("g", fontName="UIB", fontSize=8, leading=9.5, textColor=NAVY)
sSmall = ParagraphStyle("s", fontName="UI", fontSize=7, leading=9, textColor=MUTED)


def cell(code, note):
    fg, _ = STYLE[code]
    return Paragraph(f'<font name="UIB" size="4.2" color="#{fg.hexval()[2:]}">{LEGEND[code][0]}</font><br/>'
                     f'<font size="4.6" color="#334155">{note}</font>',
                     ParagraphStyle("c", fontName="UI", fontSize=4.6, leading=5.4, alignment=1))


def matrix():
    data = [[Paragraph("Activity", ParagraphStyle("x", parent=sHead, alignment=0))] +
            [Paragraph(m, sHead) for m in MARKETS]]
    st = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("GRID", (0, 1), (-1, -1), 0.6, colors.white),
          ("LEFTPADDING", (0, 0), (-1, -1), 0.8), ("RIGHTPADDING", (0, 0), (-1, -1), 0.8),
          ("TOPPADDING", (0, 0), (-1, -1), 0.7), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.1)]
    for gname, rows in GROUPS:
        r = len(data)
        data.append([Paragraph(gname.upper(), sGroup)] + [""] * len(MARKETS))
        st += [("SPAN", (0, r), (-1, r)), ("BACKGROUND", (0, r), (-1, r), SOFT),
               ("LINEBELOW", (0, r), (-1, r), 0.8, NAVY)]
        for act, cells in rows:
            r = len(data)
            data.append([Paragraph(act, sAct)] + [cell(c, n) for c, n in cells])
            for j, (c, _) in enumerate(cells, 1):
                st.append(("BACKGROUND", (j, r), (j, r), STYLE[c][1]))
    aw = 57
    t = Table(data, colWidths=[aw] + [(FW - aw) / len(MARKETS)] * len(MARKETS), repeatRows=1)
    t.setStyle(TableStyle(st))
    return t


def legend():
    items = []
    for k, (lab, desc) in LEGEND.items():
        fg, bg = STYLE[k]
        items.append(Paragraph(f'<font name="UIB" color="#{fg.hexval()[2:]}">{lab}</font> '
                               f'<font color="#475569">{desc}</font>',
                               ParagraphStyle("l", fontName="UI", fontSize=6.8, leading=8.4)))
    t = Table([items], colWidths=[FW / 6] * 6)
    t.setStyle(TableStyle([("BACKGROUND", (i, 0), (i, 0), STYLE[k][1]) for i, k in enumerate(LEGEND)] +
                          [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("GRID", (0, 0), (-1, -1), 1.5, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def on_page(c, doc):
    c.saveState()
    c.setFillColor(NAVY); c.rect(0, H - 6, W, 6, stroke=0, fill=1)
    c.setFont("UI", 7.5); c.setFillColor(MUTED)
    c.drawString(M, 7 * mm, "Product pushers · What can be automated")
    c.drawRightString(W - M, 7 * mm, f"November 2026  ·  page {doc.page}")
    c.restoreState()


def build_pdf():
    out = REPO / "docs" / "marketplace_capabilities.pdf"
    doc = BaseDocTemplate(str(out), pagesize=PAGE, leftMargin=M, rightMargin=M, topMargin=10 * mm,
                          bottomMargin=11 * mm, title="Marketplaces – what can be automated", author="Davis")
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(M, 11 * mm, FW, H - 21 * mm, id="f", leftPadding=0,
                                                             rightPadding=0, topPadding=0, bottomPadding=0)],
                                       onPage=on_page)])
    s = [Paragraph("What can be automated on each marketplace", sH1),
         Paragraph("Every seller activity, per marketplace: what our scripts already do, what the marketplace's "
                   "API lets us automate next, and what can only be done by hand.", sSub),
         legend(), Spacer(1, 3), matrix(), Spacer(1, 2)]
    s += [Paragraph(f, sSmall) for f in FOOTNOTES]
    s.append(PageBreak())

    s += [Paragraph("Managing sales automatically: what it would take", sH1),
          Paragraph("Listing is done. Below is what we can add so that sales are handled automatically too, "
                    "in order of importance, and what will stay manual.", sSub)]
    plan_rows = [[Paragraph(f"<b>{t}</b>", sBody), Paragraph(d, sBody)] for t, d in PLAN]
    pt = Table(plan_rows, colWidths=[130, FW * 0.62 - 18 - 130])
    pt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FEF3C7"))]))
    left = [Paragraph("Next steps for sales (all possible through the APIs)", sH2), pt]
    right = [Paragraph("At a glance", sH2),
             Paragraph("• <b>Listing books:</b> automatic today on all 26 marketplaces.<br/>"
                       "• <b>Sales:</b> can be automated almost everywhere (orders, accept, tracking, cancel).<br/>"
                       "• <b>Vinted</b> is the most manual: its API covers listings and orders only.<br/>"
                       "• <b>Leads</b> (offers to interested buyers) exist only on eBay. Elsewhere, buyer "
                       "interest arrives as messages.", sBody),
             Spacer(1, 6), Paragraph("Stays manual", sH2),
             Paragraph("<br/>".join("• " + x for x in STAYS_MANUAL), sBody)]
    two = Table([[left, right]], colWidths=[FW * 0.62, FW * 0.38])
    two.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (0, 0), 18), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    s.append(two)
    doc.build(s)
    print("written", out)


build_md()
build_pdf()
