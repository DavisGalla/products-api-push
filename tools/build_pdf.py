"""
Builds docs/product_pushers_architecture.pdf (4 pages, A4 landscape): the overview
diagram, components, marketplaces and run flow, security and status.
The texts and numbers are in this file; update them when the project changes.

    pip install -r tools/requirements.txt
    python tools/build_pdf.py                 # writes docs/product_pushers_architecture.pdf
    python tools/build_pdf.py other.pdf       # or another path
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
                                TableStyle, Flowable, PageBreak, KeepTogether)

ROOT = Path(__file__).resolve().parent.parent
OUT = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "docs" / "product_pushers_architecture.pdf")
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

NAVY = colors.HexColor("#1F2A44")
INK = colors.HexColor("#1E293B")
MUTED = colors.HexColor("#64748B")
LINE = colors.HexColor("#CBD5E1")
SOFT = colors.HexColor("#F1F5F9")
BLUE = colors.HexColor("#2563EB")
BLUE_BG = colors.HexColor("#DBEAFE")
TEAL = colors.HexColor("#0F766E")
TEAL_BG = colors.HexColor("#CCFBF1")
AMBER = colors.HexColor("#B45309")
AMBER_BG = colors.HexColor("#FEF3C7")
RED = colors.HexColor("#B91C1C")
RED_BG = colors.HexColor("#FEE2E2")
GREEN = colors.HexColor("#15803D")
GREEN_BG = colors.HexColor("#DCFCE7")
PURPLE = colors.HexColor("#6D28D9")
PURPLE_BG = colors.HexColor("#EDE9FE")
GREY_BG = colors.HexColor("#E2E8F0")

PAGE = landscape(A4)
W, H = PAGE
M = 16 * mm

sH1 = ParagraphStyle("h1", fontName="UIB", fontSize=20, leading=24, textColor=NAVY, spaceAfter=2)
sSub = ParagraphStyle("sub", fontName="UI", fontSize=10.5, leading=14, textColor=MUTED, spaceAfter=10)
sH2 = ParagraphStyle("h2", fontName="UIB", fontSize=12.5, leading=16, textColor=NAVY, spaceBefore=8, spaceAfter=5)
sBody = ParagraphStyle("b", fontName="UI", fontSize=9.5, leading=13, textColor=INK)
sCell = ParagraphStyle("c", fontName="UI", fontSize=8.6, leading=11.2, textColor=INK)
sCellB = ParagraphStyle("cb", parent=sCell, fontName="UIB")
sHead = ParagraphStyle("hd", parent=sCell, fontName="UIB", textColor=colors.white)
sSmall = ParagraphStyle("s", fontName="UI", fontSize=8, leading=10.5, textColor=MUTED)


def P(t, s=sCell):
    return Paragraph(t, s)


def table(rows, widths, head=True, zebra=True, pad=4):
    data = [[P(c, sHead) for c in rows[0]]] + [[c if isinstance(c, Flowable) else P(c) for c in r] for r in rows[1:]]
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("BACKGROUND", (0, 0), (-1, 0), NAVY),
          ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
          ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
          ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad)]
    if zebra:
        for i in range(2, len(data), 2):
            st.append(("BACKGROUND", (0, i), (-1, i), SOFT))
    t.setStyle(TableStyle(st))
    return t


def pill(text, fg, bg):
    t = Table([[P(f"<font color='#{fg.hexval()[2:]}'><b>{text}</b></font>", sCell)]])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LEFTPADDING", (0, 0), (-1, -1), 5),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 1),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


# ------------------------------------------------------------ drawing helpers

def box(c, x, y, w, h, fill, stroke=None, r=6, lw=1):
    c.setFillColor(fill)
    c.setStrokeColor(stroke or fill)
    c.setLineWidth(lw)
    c.roundRect(x, y, w, h, r, stroke=1 if stroke else 0, fill=1)


def text(c, x, y, s, font="UI", size=9, color=INK, anchor="left"):
    c.setFont(font, size)
    c.setFillColor(color)
    {"left": c.drawString, "center": c.drawCentredString, "right": c.drawRightString}[anchor](x, y, s)


def arrow(c, x1, y1, x2, y2, color=MUTED, lw=1.4, head=6):
    import math
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(lw)
    ang = math.atan2(y2 - y1, x2 - x1)
    bx, by = x2 - head * math.cos(ang), y2 - head * math.sin(ang)
    c.line(x1, y1, bx, by)
    p = c.beginPath()
    p.moveTo(x2, y2)
    p.lineTo(bx - head * 0.55 * math.sin(ang), by + head * 0.55 * math.cos(ang))
    p.lineTo(bx + head * 0.55 * math.sin(ang), by - head * 0.55 * math.cos(ang))
    p.close()
    c.drawPath(p, stroke=0, fill=1)


def badge(c, x, y, n, color):
    c.setFillColor(color)
    c.circle(x, y, 9, stroke=0, fill=1)
    text(c, x, y - 3.4, str(n), "UIB", 10, colors.white, "center")


class Infographic(Flowable):
    """Page-1 diagram: scraper -> runner (+ Bitwarden) -> 26 marketplaces -> logs + Gmail report."""

    def __init__(self, w, h):
        super().__init__()
        self.width, self.height = w, h

    def draw(self):
        c = self.canv
        W_, H_ = self.width, self.height

        # ---- 1 scraper / input (left)
        x1, w1 = 0, 150
        ym = H_ * 0.55
        box(c, x1, ym - 52, w1, 104, SOFT, LINE)
        badge(c, x1 + 14, ym + 38, 1, NAVY)
        text(c, x1 + 28, ym + 34, "Scraper", "UIB", 11, NAVY)
        text(c, x1 + 12, ym + 14, "Collects books from the", size=8.3, color=MUTED)
        text(c, x1 + 12, ym + 3, "source shop", size=8.3, color=MUTED)
        # document icon
        dx, dy = x1 + 14, ym - 42
        box(c, dx, dy, 122, 34, colors.white, LINE, r=4)
        text(c, dx + 8, dy + 20, "books.json", "UIB", 9, INK)
        text(c, dx + 8, dy + 8, "title · price · ISBN · stock", size=7.3, color=MUTED)

        # ---- 3 runner (centre)
        rx, rw, rh = 228, 196, 128
        ry = ym - rh / 2
        box(c, rx, ry, rw, rh, BLUE_BG, BLUE, lw=1.2)
        badge(c, rx + 14, ry + rh - 14, 3, BLUE)
        text(c, rx + 28, ry + rh - 18, "push_all.py", "UIB", 12, BLUE)
        lines = ["Runs every enabled pusher,", "one after another",
                 "Counts pushed / skipped /", "rejected / failed",
                 "Lock: no double runs", "Timeout per pusher"]
        yy = ry + rh - 38
        for i, l in enumerate(lines):
            bullet = i in (0, 2, 4, 5)
            text(c, rx + 14, yy, ("•  " if bullet else "    ") + l, size=8.4, color=INK)
            yy -= 12.5
        arrow(c, x1 + w1 + 4, ym, rx - 4, ym, NAVY, 1.8)
        text(c, (x1 + w1 + rx) / 2, ym + 6, "products", size=7.5, color=MUTED, anchor="center")

        # ---- 2 Bitwarden (above runner)
        bw_w, bw_h = 236, 56
        bx, by = rx + rw / 2 - bw_w / 2, H_ - bw_h - 2
        box(c, bx, by, bw_w, bw_h, PURPLE_BG, PURPLE, lw=1.2)
        badge(c, bx + 14, by + bw_h - 14, 2, PURPLE)
        text(c, bx + 28, by + bw_h - 18, "Bitwarden Secrets Manager", "UIB", 10.5, PURPLE)
        text(c, bx + 14, by + 22, "All credentials: Gmail + marketplace keys", size=8.2, color=INK)
        text(c, bx + 14, by + 10, "Injected at start by  bws run  (nothing in files)", size=8.2, color=INK)
        arrow(c, rx + rw / 2, by - 3, rx + rw / 2, ry + rh + 3, PURPLE, 1.8)
        text(c, rx + rw / 2 + 6, (by + ry + rh) / 2 - 3, "credentials", size=7.5, color=PURPLE)

        # ---- marketplaces (right)
        mk = [("Vinted", "Vinted Pro API"),
              ("eBay", "Sell Inventory API"),
              ("Amazon EU (9 stores)", "Selling Partner API"),
              ("Casa del Libro", "Mirakl API"),
              ("Fnac", "Fnac Marketplace API"),
              ("AbeBooks / IberLibro / ZVAB", "Inventory Update API"),
              ("booklooker.de", "REST API 2.0"),
              ("El Corte Inglés", "Mirakl API"),
              ("Carrefour", "Mirakl API"),
              ("Worten (PT + ES)", "Mirakl API"),
              ("IBS.it (Italy)", "Mirakl API"),
              ("Empik (Poland)", "Mirakl API"),
              ("E.Leclerc (France)", "Mirakl API"),
              ("Kaufland.es", "Kaufland Seller API v2"),
              ("bol.com (NL + BE)", "Retailer API"),
              ("Cdiscount (France)", "Octopia Seller API"),
              ("Allegro (PL + CZ / SK / HU)", "Allegro REST API"),
              ("AliExpress", "Open Platform API"),
              ("Temu", "Partner API"),
              ("TikTok Shop", "Partner API"),
              ("Galaxus (CH / DE)", "data feed over SFTP"),
              ("OTTO Market (DE)", "Products API v5"),
              ("eMAG (RO / BG / HU)", "Marketplace API v4.5"),
              ("Fruugo (~40 countries)", "CSV feed at a static URL"),
              ("CDON (SE / DK / FI / NO)", "merchant API"),
              ("OnBuy (UK + EU)", "API v2")]
        mx, mw, mh, gap = 520, W_ - 520, 10.6, 1.0
        total = len(mk) * mh + (len(mk) - 1) * gap
        top = H_ - 22
        text(c, mx, top + 10, "26 MARKETPLACES  ·  official seller APIs / feeds", "UIB", 8.2, MUTED)
        for i, (name, api) in enumerate(mk):
            y = top - (i + 1) * mh - i * gap
            box(c, mx, y, mw, mh, colors.white, LINE)
            c.setFillColor(TEAL)
            c.rect(mx, y, 4, mh, stroke=0, fill=1)
            text(c, mx + 14, y + 5.8, name, "UIB", 5.5, INK)
            text(c, mx + 14, y + 1.3, api, size=4.2, color=MUTED)
            text(c, mx + mw - 10, y + 4.0, f"{['vinted_api','ebay','amazon','casadellibro','fnac','abebooks','booklooker','elcorteingles','carrefour','worten','ibs','empik','leclerc','kaufland','bol','cdiscount','allegro','aliexpress','temu','tiktok','galaxus','otto','emag','fruugo','cdon','onbuy'][i]}_push.py",
                 size=5.2, color=TEAL, anchor="right")
            arrow(c, rx + rw + 4, ym + (12.5 - i) * 2.3, mx - 4, y + mh / 2, TEAL, 1.1, 5)
        text(c, (rx + rw + mx) / 2 - 4, ym + 40, "4", "UIB", 1, colors.white)  # spacer
        badge(c, (rx + rw + mx) / 2, top + 14, 4, TEAL)
        text(c, (rx + rw + mx) / 2 + 13, top + 10, "push", "UIB", 9, TEAL)

        # ---- 5 report (below runner)
        ex, ew, eh = 150, 352, 118
        ey = 0
        arrow(c, rx + rw / 2, ry - 3, rx + rw / 2, ey + eh + 3, NAVY, 1.8)
        text(c, rx + rw / 2 + 6, (ry + ey + eh) / 2 - 3, "results", size=7.5, color=MUTED)
        box(c, ex, ey, ew, eh, colors.white, LINE)
        badge(c, ex + 14, ey + eh - 14, 5, GREEN)
        text(c, ex + 28, ey + eh - 18, "Email report via Gmail", "UIB", 10.5, GREEN)
        text(c, ex + ew - 10, ey + eh - 18, "+ full log attached", size=7.6, color=MUTED, anchor="right")
        text(c, ex + 14, ey + eh - 36, "[Product push] PROBLEMS – 8 pushed, 1 rejected", "UIB", 8.2, INK)
        rows = [("Vinted", "OK", GREEN, GREEN_BG, "5 pushed, 1 skipped"),
                ("Amazon EU", "PROBLEMS", AMBER, AMBER_BG, "3 pushed, 1 rejected: price too high"),
                ("Fnac", "FAILED", RED, RED_BG, "login failed: bad key"),
                ("Casa del Libro", "NOT CONFIGURED", MUTED, GREY_BG, "no credentials yet")]
        yy = ey + eh - 54
        for name, st, fg, bg, det in rows:
            text(c, ex + 14, yy, name, size=8, color=INK)
            pw = c.stringWidth(st, "UIB", 7) + 10
            box(c, ex + 92, yy - 3.5, pw, 12, bg, r=3)
            text(c, ex + 97, yy, st, "UIB", 7, fg)
            text(c, ex + 92 + pw + 8, yy, det, size=7.8, color=MUTED)
            yy -= 15
        # logs
        lx, lw_ = 0, 132
        box(c, lx, ey + 30, lw_, 58, SOFT, LINE)
        text(c, lx + 10, ey + 70, "logs/<date_time>/", "UIB", 8.5, INK)
        text(c, lx + 10, ey + 56, "Full output per pusher", size=7.8, color=MUTED)
        text(c, lx + 10, ey + 44, "+ report.txt", size=7.8, color=MUTED)
        arrow(c, ex - 3, ey + 59, lx + lw_ + 3, ey + 59, MUTED, 1.2, 5)

        # ---- schedule note (bottom right)
        sx, sw = 520, W_ - 520
        box(c, sx, 0, sw, 54, NAVY)
        text(c, sx + 12, 34, "Runs on a server, once a day", "UIB", 9.5, colors.white)
        text(c, sx + 12, 19, "cron / Task Scheduler:  bws run -- python push_all.py", size=7.9,
             color=colors.HexColor("#CBD5E1"))
        text(c, sx + 12, 7, "Exit code 0 OK · 1 a pusher failed · 2 email failed", size=7.6,
             color=colors.HexColor("#94A3B8"))


class Layers(Flowable):
    """Three-layer architecture strip."""

    def __init__(self, w, h=92):
        super().__init__()
        self.width, self.height = w, h

    def draw(self):
        c = self.canv
        cols = [("1  INPUT", "Scraper output (books.json)|+ push_all_config.json", SOFT, NAVY),
                ("2  ORCHESTRATION & REPORT", "push_all.py runs the pushers,|counts results, logs, emails", BLUE_BG, BLUE),
                ("3  PUSHERS", "26 scripts, one per marketplace,|calling the official APIs.|Each also runs on its own.", colors.HexColor("#F0FDFA"), TEAL),
                ("SECRETS (feeds 2 and 3)", "Bitwarden Secrets Manager,|injected by bws at start", PURPLE_BG, PURPLE)]
        gap = 14
        w = (self.width - gap * (len(cols) - 1)) / len(cols)
        for i, (t, body, bg, fg) in enumerate(cols):
            x = i * (w + gap)
            box(c, x, 0, w, self.height, bg, fg, lw=1)
            text(c, x + 12, self.height - 20, t, "UIB", 9, fg)
            yy = self.height - 40
            for l in body.split("|"):
                text(c, x + 12, yy, l, size=8.6, color=INK)
                yy -= 12.5
            if i < len(cols) - 2:
                arrow(c, x + w + 1, self.height / 2, x + w + gap - 1, self.height / 2, MUTED, 1.4, 5)


class Steps(Flowable):
    """Numbered run-flow timeline."""

    def __init__(self, w, steps, h=88):
        super().__init__()
        self.width, self.height, self.steps = w, h, steps

    def draw(self):
        c = self.canv
        n = len(self.steps)
        w = self.width / n
        y = self.height - 14
        c.setStrokeColor(LINE)
        c.setLineWidth(2)
        c.line(w / 2, y, self.width - w / 2, y)
        for i, (title, body) in enumerate(self.steps):
            cx = i * w + w / 2
            badge(c, cx, y, i + 1, BLUE if i not in (n - 1,) else GREEN)
            text(c, cx, y - 26, title, "UIB", 8.8, INK, "center")
            yy = y - 40
            for l in body.split("\n"):
                text(c, cx, yy, l, size=7.8, color=MUTED, anchor="center")
                yy -= 10.5


# ------------------------------------------------------------ page decoration

def on_page(c, doc):
    c.saveState()
    c.setFillColor(NAVY)
    c.rect(0, H - 6, W, 6, stroke=0, fill=1)
    text(c, M, 9 * mm, "Product pushers · Architecture & how it works", size=7.5, color=MUTED)
    text(c, W - M, 9 * mm, f"November 2026  ·  page {doc.page}", size=7.5, color=MUTED, anchor="right")
    c.restoreState()


doc = BaseDocTemplate(OUT, pagesize=PAGE, leftMargin=M, rightMargin=M, topMargin=13 * mm, bottomMargin=15 * mm,
                      title="Product pushers – Architecture & how it works", author="Davis",
                      subject="Architecture of the marketplace product pushers")
frame = Frame(M, 15 * mm, W - 2 * M, H - 28 * mm, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=on_page)])
FW = W - 2 * M

s = []
# ---------------- page 1: infographic
s.append(Paragraph("Product pushers: how it works", sH1))
s.append(Paragraph("Scraped books are listed on 26 marketplaces through their official seller APIs and feeds. "
                   "One run a day; one email tells you what happened everywhere.", sSub))
s.append(Infographic(FW, 425))
s.append(PageBreak())

# ---------------- page 2: architecture
s.append(Paragraph("Architecture", sH1))
s.append(Paragraph("Four parts, each replaceable on its own. No marketplace logic sits in the runner, "
                   "and no credentials are stored in code, configuration or git.", sSub))
s.append(Layers(FW))
s.append(Spacer(1, 12))
s.append(Paragraph("Components", sH2))
s.append(table([
    ["Component", "Responsibility", "Technology"],
    ["<b>Scraper</b> (separate project)", "Produces books.json: id, title, price, ISBN, condition, images, stock", "Python"],
    ["<b>push_all_config.json</b>", "Which pushers run and their options, products file, report recipients, timeouts", "JSON (local, not in git)"],
    ["<b>push_all.py</b> (runner)", "Runs the pushers one by one, counts results, writes logs, emails the report, prevents overlapping runs", "Python standard library"],
    ["<b>stock_sync.py</b>", "Stops double sales: reads new orders, keeps one stock, takes sold copies down on every marketplace", "Python + the pushers' API clients"],
    ["<b>26 pushers</b> (*_push.py)", "Validate products, match the marketplace catalog, create or update listings, record what was pushed", "Python + requests (Galaxus, Fruugo SFTP: paramiko)"],
    ["<b>State files</b> (*_posted.json)", "What is already listed on each marketplace, so re-runs never create duplicates", "JSON (local)"],
    ["<b>Bitwarden Secrets Manager</b> + bws", "Stores all credentials; injects them as environment variables when a run starts", "Bitwarden cloud, bws 2.1.0"],
    ["<b>Gmail</b>", "Sends the report: SMTP over TLS, App Password on a dedicated account", "Gmail"],
    ["<b>logs/</b>", "Full output of every pusher plus report.txt, one folder per run", "Text files (local)"],
], [150, 430, FW - 580]))
s.append(PageBreak())

# ---------------- page 3: marketplaces + run flow
s.append(Paragraph("Marketplaces and run flow", sH1))
s.append(Paragraph("Same product format and rules everywhere: skip sold-out or incomplete products, "
                   "never list a product twice.", sSub))
s.append(table([
    ["Marketplace", "API", "Login", "How a book is listed"],
    ["<b>Vinted</b>", "Vinted Pro Integrations API", "Access + signing key (HMAC-signed requests)", "Creates a new item with own photos, text and price"],
    ["<b>eBay</b> (EU)", "Sell Inventory API", "OAuth refresh token", "Inventory item, then offer, then publish"],
    ["<b>Amazon</b> ES DE FR IT NL BE PL SE IE", "Selling Partner API", "Login with Amazon refresh token", "Finds the Amazon page by ISBN, adds an offer"],
    ["<b>Casa del Libro</b>", "Mirakl seller API", "API key", "Matches the catalog by EAN; one CSV offer import"],
    ["<b>Fnac</b>", "Fnac Marketplace API v2.6 (XML)", "Partner ID + shop ID + key", "Sends offers by EAN in batches; checks the batch status"],
    ["<b>AbeBooks / IberLibro</b> (+ ZVAB)", "Inventory Update API (XML)", "Username + API key", "Describes its own copy of a book: no catalog match needed"],
    ["<b>booklooker.de</b> (Germany)", "booklooker REST API 2.0", "API key", "Own copy of a book; all offers in one article file"],
    ["<b>El Corte Inglés</b>", "Mirakl seller API", "API key", "Same as Casa del Libro (no public docs)"],
    ["<b>Carrefour</b>", "Mirakl seller API", "API key", "Same as Casa del Libro (ES and FR are separate)"],
    ["<b>Worten</b> (Portugal + Spain)", "Mirakl seller API", "API key", "Same as Casa del Libro; one price for PT + ES"],
    ["<b>IBS.it</b> (Italy)", "Mirakl seller API", "API key", "Same as Casa del Libro"],
    ["<b>Empik</b> (Poland)", "Mirakl seller API", "API key", "Same as Casa del Libro; prices converted to PLN"],
    ["<b>E.Leclerc</b> (France)", "Mirakl seller API", "API key", "Same as Casa del Libro"],
    ["<b>Kaufland.es</b>", "Kaufland Seller API v2", "Client + secret key (HMAC-signed)", "Checks the catalog by EAN, then adds an offer"],
    ["<b>bol.com</b> (NL + BE)", "Retailer API (Offers v11)", "Client ID + secret", "Catalog check by EAN, then an offer (new or used)"],
    ["<b>Cdiscount</b> (France)", "Octopia Seller API", "Client ID + secret + seller ID", "Catalog check by EAN, offers sent in Upsert packages"],
    ["<b>Allegro</b> (PL, CZ, SK, HU)", "Allegro REST API", "Client ID + secret + device authorization", "Offer on the catalog product by EAN"],
    ["<b>AliExpress</b> (local seller)", "AliExpress Open Platform API", "App key + secret + OAuth seller token", "Own product per book (category from EAN data)"],
    ["<b>Temu</b> (local seller)", "Temu Partner API", "App key + secret + Seller Center token", "Own product per book; Temu reviews it and its price"],
    ["<b>TikTok Shop</b> (Spain, EU)", "TikTok Shop Partner API", "App key + secret + OAuth seller token", "Own product per book; TikTok reviews it"],
    ["<b>Galaxus</b> (CH + DE)", "Data feed (CSV) over SFTP", "SFTP user + password (pinned host key)", "One feed file with all offers, matched by ISBN"],
    ["<b>OTTO Market</b> (Germany)", "OTTO Market API (Products v5)", "Client ID + secret (self-app)", "Own product per book; stock via Availability API"],
    ["<b>eMAG</b> (RO, BG, HU)", "eMAG Marketplace API v4.5", "API user + password (whitelisted IP)", "Finds the eMAG product by EAN, attaches the offer"],
    ["<b>Fruugo</b> (~40 countries)", "CSV product feed at a static URL", "Web folder or SFTP to own host", "One feed with all books (ISBN), images copied next to it"],
    ["<b>CDON</b> (SE, DK, FI, NO)", "CDON merchant API", "Merchant ID + API token", "Own article per book; price per market; async status"],
    ["<b>OnBuy</b> (UK + EU)", "OnBuy API v2", "Consumer key + secret key", "Finds the OnBuy product by ISBN, attaches the listing"],
], [150, 165, 190, FW - 505], pad=0.55))
s.append(Spacer(1, 6))
s.append(Paragraph("One run, step by step", sH2))
s.append(Steps(FW, [
    ("Start", "Scheduler runs\nbws run -- python push_all.py"),
    ("Credentials", "bws fetches the secrets\nfrom Bitwarden"),
    ("Lock", "A second run can't\nstart in parallel"),
    ("Push", "Each pusher in turn,\nwith --yes and a timeout"),
    ("Count", "State file before/after,\nSKIP and ! lines, exit code"),
    ("Report", "Logs saved, email sent,\nlock released"),
]))
s.append(PageBreak())

# ---------------- page 4: security, ops, status
s.append(Paragraph("Security, operations and status", sH1))
s.append(Paragraph("What protects the credentials, how failures are handled, and where the project stands today.", sSub))
left = [Paragraph("Credentials", sH2), table([
    ["Decision", "Why"],
    ["All keys in <b>Bitwarden Secrets Manager</b>", "One managed place, team access, access log; change a key without touching the server"],
    ["Server holds only a <b>read-only access token</b>", "Smallest secret on disk; each machine can be revoked separately"],
    ["Keys reach scripts only as <b>environment variables</b>", "Nothing secret in code, git or config files"],
    ["Gmail <b>App Password</b> on a dedicated account", "Revocable on its own; no personal mailbox exposed"],
    ["Email only over <b>TLS</b>; local files in .gitignore", "Passwords never sent in clear text; nothing local reaches GitHub"],
], [150, 222]),
    Paragraph("When something goes wrong", sH2), table([
    ["Situation", "What happens"],
    ["Product invalid", "Skipped with its reason; the rest continue"],
    ["Offer rejected", "Shown in the email with the marketplace's message; retried next run"],
    ["Bad key / crash / hang", "That marketplace is FAILED; the others still run"],
    ["Email can't be sent", "Exit code 2; the report is still in logs/"],
], [150, 222])]
right = [Paragraph("Status (5 Nov 2026)", sH2), table([
    ["Part", "Status", "Details"],
    ["<b>26 pushers</b>", pill("TESTED", AMBER, AMBER_BG), "Pass all automated tests against fake APIs. Not yet run on the real marketplaces: no seller accounts yet."],
    ["<b>Runner + email</b>", pill("VERIFIED", GREEN, GREEN_BG), "Real test email and report delivered through Gmail, with credentials from Bitwarden."],
    ["<b>Bitwarden</b>", pill("VERIFIED", GREEN, GREEN_BG), "Project, machine account, token and bws working."],
    ["<b>Server</b>", pill("TO DO", MUTED, GREY_BG), "Steps and schedule commands are documented."],
], [84, 66, 212]),
    Spacer(1, 4),
    Paragraph("Automated tests: <b>697 checks, all passing</b> (30 test suites, no real API contacted).", sBody),
    Paragraph("Next steps", sH2),
    Paragraph("1. Seller accounts and API access for each marketplace<br/>"
              "2. Add their keys to Bitwarden<br/>"
              "3. Safe first run per marketplace: dry run, then 1 product<br/>"
              "4. Deploy to the server and schedule daily", sBody),
    Spacer(1, 6),
    Paragraph("Note: new books in Spain, Portugal, Italy, France, Germany and the Netherlands have legally fixed prices. Full documentation: "
              "docs/architecture.md and docs/push_all.md in the GitHub repository.", sSmall)]
two = Table([[left, right]], colWidths=[FW / 2 + 4, FW / 2 - 4])
two.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                         ("RIGHTPADDING", (0, 0), (0, 0), 18), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
s.append(two)

doc.build(s)
print("written", OUT)
