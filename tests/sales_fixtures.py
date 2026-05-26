"""Fixture data + renderer for PR #35 shift-close parsing tests.

The 10 ``.TXT`` files in ``tests/fixtures/sales/`` are GENERATED from the specs
below and written exactly as the production POS emits them: UTF-16 with a BOM
and CRLF line endings. Regenerate them with::

    python -m tests.sales_fixtures

IMPORTANT (format provenance): the real shift-close files were not available
when this PR was written (see the PR description). The layout here is modelled
on the documented variance-analysis findings — outlet identity from the
subject, optional deleted/stock/cashdrawer sections, BISTRO7 tax, KLANG's
negative ``Kacang -1218`` stock line, the exact KLANG/BISTRO7 totals. Validate
``sales_parser._SECTION_TITLES`` + the line regexes against the real files
before production; the schema/ingestion/analytics do not depend on the layout.
"""

from __future__ import annotations

import os

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "sales")

# Section-presence rules straight from the variance analysis (#5):
#   deleted_items : KLANG, SEK14, SEK20, VISTA
#   stock_report  : Damansara (D.U), KLANG, SEK20, SEK6
#   cashdrawer    : everyone EXCEPT SEK14, SEK20

FIXTURES = [
    {
        "code": "S-BISTRO7", "canonical": "Bistro", "filename": "S-BISTRO7_SHIFTCLOSE.TXT",
        "header_outlet": "BISTRO 7", "shift_no": "2207", "cashier": "FARID",
        "open_time": "26/05/2026 11:02:10", "close_time": "26/05/2026 19:25:41",
        "gross": 6181.13, "discount": 0.00, "service": 0.00, "tax": 382.62,
        "net": 6181.13, "total": 6563.75,
        "categories": [("Food", 5000.00), ("Beverage", 1181.13)],
        "tax_lines": [("SST 6%", 382.62)],
        "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 4000.00), ("Card", 2563.75), ("E-Wallet", 0.00)],
        "items": [(40, "Nasi Briyani Ayam", 480.00), (35, "Teh Tarik", 175.00),
                  (20, "Roti Canai", 60.00), (15, "Mee Goreng Mamak", 120.00)],
        "deleted": None, "stock": None,
        "cashdrawer": [("Opening Float", 300.00), ("Cash In", 0.00), ("Cash Out", 100.00)],
    },
    {
        "code": "S-DAMANSARA", "canonical": "D.U", "filename": "S-DAMANSARA_SHIFTCLOSE.TXT",
        "header_outlet": "DAMANSARA UPTOWN", "shift_no": "884", "cashier": "SITI",
        "open_time": "25/05/2026 19:01:55", "close_time": "26/05/2026 07:05:12",
        "gross": 5200.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 5200.00, "total": 5200.00,
        "categories": [("Food", 4200.00), ("Beverage", 1000.00)],
        "tax_lines": None, "discounts": [("Staff Discount", 0.00)],
        "payments": [("Cash", 3000.00), ("Card", 2200.00), ("E-Wallet", 0.00)],
        "items": [(30, "Nasi Lemak Ayam", 390.00), (25, "Kopi O", 100.00),
                  (18, "Maggi Goreng", 144.00)],
        "deleted": None,
        "stock": [("Beras 10kg", 42), ("Ayam (kg)", 88), ("Telur (tray)", 60)],
        "cashdrawer": [("Opening Float", 200.00), ("Cash In", 0.00), ("Cash Out", 50.00)],
    },
    {
        "code": "S-JAKEL", "canonical": "Jakel", "filename": "S-JAKEL_SHIFTCLOSE.TXT",
        "header_outlet": "JAKEL MALL", "shift_no": "1322", "cashier": "RAJ",
        "open_time": "26/05/2026 10:58:00", "close_time": "26/05/2026 19:10:33",
        "gross": 3800.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 3800.00, "total": 3800.00,
        "categories": [("Food", 3000.00), ("Beverage", 800.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 2500.00), ("Card", 1300.00), ("E-Wallet", 0.00)],
        "items": [(28, "Nasi Kandar", 420.00), (22, "Teh Ais", 110.00),
                  (12, "Ayam Goreng", 96.00)],
        "deleted": None, "stock": None,
        "cashdrawer": [("Opening Float", 200.00), ("Cash In", 0.00), ("Cash Out", 0.00)],
    },
    {
        "code": "S-KLANG", "canonical": "Klang B.Emas", "filename": "S-KLANG_SHIFTCLOSE.TXT",
        "header_outlet": "KLANG BANDAR ENMAS", "shift_no": "1499", "cashier": "AISYAH",
        "open_time": "25/05/2026 19:02:11", "close_time": "26/05/2026 07:01:45",
        "gross": 4758.20, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 4758.20, "total": 4758.20,
        "categories": [("Food", 3800.20), ("Beverage", 958.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 3200.00), ("Card", 1558.20), ("E-Wallet", 0.00)],
        "items": [(45, "Nasi Campur", 540.00), (33, "Teh Tarik", 165.00),
                  (20, "Roti Telur", 80.00), (10, "Air Bandung", 40.00)],
        "deleted": [(2, "Teh Tarik", 10.00), (1, "Roti Canai", 3.00)],
        "stock": [("Kacang", -1218), ("Beras 10kg", 45), ("Minyak Masak (btl)", -12)],
        "cashdrawer": [("Opening Float", 250.00), ("Cash In", 0.00), ("Cash Out", 80.00)],
    },
    {
        "code": "S-SBESI", "canonical": "SBESI", "filename": "S-SBESI_SHIFTCLOSE.TXT",
        "header_outlet": "SUNGAI BESI", "shift_no": "540", "cashier": "KUMAR",
        "open_time": "26/05/2026 11:05:00", "close_time": "26/05/2026 19:45:09",
        "gross": 2900.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 2900.00, "total": 2900.00,
        "categories": [("Food", 2300.00), ("Beverage", 600.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 1900.00), ("Card", 1000.00), ("E-Wallet", 0.00)],
        "items": [(20, "Nasi Goreng", 240.00), (18, "Teh O Ais", 72.00),
                  (9, "Murtabak", 90.00)],
        "deleted": None, "stock": None,
        "cashdrawer": [("Opening Float", 150.00), ("Cash In", 0.00), ("Cash Out", 0.00)],
    },
    {
        "code": "S-SEK14", "canonical": "Signature", "filename": "S-SEK14_SHIFTCLOSE.TXT",
        "header_outlet": "SEKSYEN 14", "shift_no": "1777", "cashier": "DINA",
        "open_time": "26/05/2026 10:50:00", "close_time": "26/05/2026 19:30:22",
        "gross": 5100.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 5100.00, "total": 5100.00,
        "categories": [("Food", 4100.00), ("Beverage", 1000.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 3300.00), ("Card", 1800.00), ("E-Wallet", 0.00)],
        "items": [(38, "Nasi Ayam", 456.00), (30, "Teh Tarik", 150.00),
                  (16, "Cendol", 96.00)],
        "deleted": [(1, "Cendol", 6.00)],
        "stock": None,    # SEK14 has no stock report
        "cashdrawer": None,  # SEK14 has no cash drawer section
    },
    {
        "code": "S-SEK15", "canonical": "One Bistro", "filename": "S-SEK15_SHIFTCLOSE.TXT",
        "header_outlet": "SEKSYEN 15", "shift_no": "1610", "cashier": "HAKIM",
        "open_time": "25/05/2026 19:00:40", "close_time": "26/05/2026 07:08:55",
        "gross": 4600.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 4600.00, "total": 4600.00,
        "categories": [("Food", 3700.00), ("Beverage", 900.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 2900.00), ("Card", 1700.00), ("E-Wallet", 0.00)],
        "items": [(34, "Nasi Lemak Special", 510.00), (27, "Kopi Ais", 135.00),
                  (14, "Roti Bakar", 56.00)],
        "deleted": None, "stock": None,
        "cashdrawer": [("Opening Float", 200.00), ("Cash In", 0.00), ("Cash Out", 30.00)],
    },
    {
        "code": "S-SEK20", "canonical": "SEK-20", "filename": "S-SEK20_SHIFTCLOSE.TXT",
        "header_outlet": "SEKSYEN 20", "shift_no": "1955", "cashier": "NABISA",
        "open_time": "26/05/2026 10:45:00", "close_time": "26/05/2026 19:20:18",
        "gross": 4300.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 4300.00, "total": 4300.00,
        "categories": [("Food", 3500.00), ("Beverage", 800.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 2800.00), ("Card", 1500.00), ("E-Wallet", 0.00)],
        "items": [(31, "Nasi Briyani", 465.00), (24, "Teh Tarik", 120.00),
                  (13, "Mee Rebus", 91.00)],
        "deleted": [(3, "Teh Tarik", 15.00)],
        "stock": [("Beras 10kg", 38), ("Gula (kg)", -22)],
        "cashdrawer": None,  # SEK20 has no cash drawer section
    },
    {
        "code": "S-SEK6", "canonical": "SEK-6", "filename": "S-SEK6_SHIFTCLOSE.TXT",
        "header_outlet": "JALAN MURAI", "shift_no": "1201", "cashier": "ZARA",
        "open_time": "25/05/2026 19:03:30", "close_time": "26/05/2026 07:03:50",
        "gross": 3950.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 3950.00, "total": 3950.00,
        "categories": [("Food", 3150.00), ("Beverage", 800.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 2600.00), ("Card", 1350.00), ("E-Wallet", 0.00)],
        "items": [(29, "Nasi Dagang", 435.00), (21, "Teh Halia", 105.00),
                  (11, "Keropok Lekor", 55.00)],
        "deleted": None,
        "stock": [("Beras 10kg", 50), ("Ikan (kg)", 33), ("Santan (pek)", -8)],
        "cashdrawer": [("Opening Float", 180.00), ("Cash In", 0.00), ("Cash Out", 40.00)],
    },
    {
        "code": "S-VISTA", "canonical": "Vista", "filename": "S-VISTA_SHIFTCLOSE.TXT",
        "header_outlet": "VISTA ALAM", "shift_no": "770", "cashier": "IMRAN",
        "open_time": "26/05/2026 10:40:00", "close_time": "26/05/2026 19:15:05",
        "gross": 4100.00, "discount": 0.00, "service": 0.00, "tax": 0.00,
        "net": 4100.00, "total": 4100.00,
        "categories": [("Food", 3300.00), ("Beverage", 800.00)],
        "tax_lines": None, "discounts": [("Member Discount", 0.00)],
        "payments": [("Cash", 2700.00), ("Card", 1400.00), ("E-Wallet", 0.00)],
        "items": [(26, "Nasi Kerabu", 390.00), (23, "Sirap Bandung", 92.00),
                  (10, "Ayam Percik", 120.00)],
        "deleted": [(2, "Sirap Bandung", 8.00)],
        "stock": None,    # Vista has no stock report
        "cashdrawer": [("Opening Float", 200.00), ("Cash In", 0.00), ("Cash Out", 60.00)],
    },
]

# Cross-check totals: the aggregate the rollout step 8 expects (RM44,000+).
EXPECTED_GRAND_TOTAL = sum(f["total"] for f in FIXTURES)

SEP = "=" * 48
SUBSEP = "-" * 48


def _kv(label, value):
    if isinstance(value, float):
        value = f"{value:.2f}"
    return f"{str(label).ljust(18)}: {value}"


def _item_line(qty, name, amount):
    # >=2 spaces between columns so the parser's 2+-space split yields 3 fields.
    return f"{str(qty).ljust(5)}  {name.ljust(28)}  {amount:.2f}"


def _stock_line(name, qty):
    return f"{name.ljust(28)}  {qty}"


def render(fixture) -> str:
    """Render one fixture spec to shift-close report text (LF newlines)."""
    lines = [
        SEP, "          KHULAFA RESTAURANT", "          SHIFT CLOSE REPORT", SEP,
        _kv("Outlet", fixture["header_outlet"]),
        _kv("Terminal", "POS-01"),
        _kv("Shift No", fixture["shift_no"]),
        _kv("Cashier", fixture["cashier"]),
        _kv("Open Time", fixture["open_time"]),
        _kv("Close Time", fixture["close_time"]),
        SUBSEP, "SALES SUMMARY", SUBSEP,
        _kv("Gross Sales", fixture["gross"]),
        _kv("Discount", fixture["discount"]),
        _kv("Service Charge", fixture["service"]),
        _kv("Tax", fixture["tax"]),
        _kv("Net Sales", fixture["net"]),
        _kv("Total Collected", fixture["total"]),
        SUBSEP, "SALES BY CATEGORY", SUBSEP,
    ]
    for name, amount in fixture["categories"]:
        lines.append(_kv(name, amount))
    if fixture.get("tax_lines"):
        lines += [SUBSEP, "TAX", SUBSEP]
        for name, amount in fixture["tax_lines"]:
            lines.append(_kv(name, amount))
    if fixture.get("discounts"):
        lines += [SUBSEP, "DISCOUNTS", SUBSEP]
        for name, amount in fixture["discounts"]:
            lines.append(_kv(name, amount))
    lines += [SUBSEP, "PAYMENT BREAKDOWN", SUBSEP]
    for name, amount in fixture["payments"]:
        lines.append(_kv(name, amount))
    lines += [SUBSEP, "ITEMS SOLD", SUBSEP, f"{'Qty'.ljust(5)}  {'Item'.ljust(28)}  Amount"]
    for qty, name, amount in fixture["items"]:
        lines.append(_item_line(qty, name, amount))
    if fixture.get("deleted"):
        lines += [SUBSEP, "DELETED ITEMS", SUBSEP, f"{'Qty'.ljust(5)}  {'Item'.ljust(28)}  Amount"]
        for qty, name, amount in fixture["deleted"]:
            lines.append(_item_line(qty, name, amount))
    if fixture.get("stock"):
        lines += [SUBSEP, "STOCK REPORT", SUBSEP, f"{'Item'.ljust(28)}  Qty"]
        for name, qty in fixture["stock"]:
            lines.append(_stock_line(name, qty))
    if fixture.get("cashdrawer"):
        lines += [SUBSEP, "CASH DRAWER", SUBSEP]
        for name, amount in fixture["cashdrawer"]:
            lines.append(_kv(name, amount))
    lines += [SUBSEP, "END OF REPORT", SEP]
    return "\n".join(lines) + "\n"


def by_code(code):
    for f in FIXTURES:
        if f["code"] == code:
            return f
    raise KeyError(code)


def write_all(directory=FIXTURE_DIR) -> list:
    """Write all fixtures as UTF-16 (BOM) + CRLF, exactly like the POS emails."""
    os.makedirs(directory, exist_ok=True)
    written = []
    for f in FIXTURES:
        path = os.path.join(directory, f["filename"])
        crlf = render(f).replace("\n", "\r\n")
        with open(path, "w", encoding="utf-16", newline="") as fh:
            fh.write(crlf)
        written.append(path)
    return written


if __name__ == "__main__":
    paths = write_all()
    print(f"Wrote {len(paths)} fixtures to {FIXTURE_DIR}")
    print(f"Grand total across fixtures: RM{EXPECTED_GRAND_TOTAL:,.2f}")
