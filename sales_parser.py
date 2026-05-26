"""POS shift-close TXT parser (PR #35).

Pure functions that turn one POS "shift close" report (a UTF-16, CRLF text file
emailed as an attachment from the master inbox) into a structured dict the
ingestion layer writes to ``sales_daily`` + child tables.

Two hard rules from the production variance analysis (these OVERRIDE any
assumption a header might suggest):

  1. Outlet identity comes from the EMAIL SUBJECT only — never from the report
     header (which names the franchise, e.g. "NASI KANDAR HAJI SHARFUDDIN", and
     is ambiguous/inconsistent across outlets). The header is kept as
     ``header_outlet_raw`` for debugging only.
  2. Encoding is UTF-16 with a BOM and CRLF line endings, but some files slip
     through as UTF-8; decode defensively and normalise newlines.

24/7 operation means two shifts per outlet per day (close ~07:00 and ~19:00 MY).
``determine_shift_type_and_business_date`` assigns each close to a
``day``/``overnight``/``unknown`` shift and the business date it belongs to.

Report layout (validated against the 10 real 25-May-2026 files):
  * A flat ``LABEL : value`` summary block at the top (TODAY SALES, TAX,
    NET SALES, DISCOUNT, SHIFTNO, DATE, CASHIER, ...). ``DATE`` is US-style
    ``M/D/YYYY h:mm:ss AM/PM``.
  * ``GROUP WISE ITEM SALES`` (category totals: SHIFTNO ITEMNAME AMOUNT).
  * ``GROUP WISE ITEM SALES (<shiftno>)`` (item totals: ITEMNAME QTY AMOUNT).
  * Optional ``STOCK ON ...`` (ITEM NAME STOCK AMOUNT — STOCK may be negative).
  * Optional ``==== DELETED ITEM BY ADMIN ====`` (ITEMNAME QTY RATE TOTALAMT).
  * Optional ``===== CASHDRAWER OPEN =====`` (drawer-open event log; absent for
    SEK14 / SEK20).
  * A cash-denomination pipe grid (TOTALCASH / QR PAY tender split).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# --- outlet identification (from SUBJECT only) ------------------------------

# Subject code -> canonical outlet name. The canonical names match how the
# owners refer to the shops and are what gets stored in sales_daily.
OUTLET_CANONICAL_BY_CODE: dict[str, str] = {
    "S-BISTRO7": "Bistro",
    "S-DAMANSARA": "D.U",
    "S-JAKEL": "Jakel",
    "S-KLANG": "Klang B.Emas",
    "S-SBESI": "SBESI",
    "S-SEK14": "Signature",
    "S-SEK15": "One Bistro",
    "S-SEK20": "SEK-20",
    "S-SEK6": "SEK-6",
    "S-VISTA": "Vista",
    # Not yet observed in production — handled with an info log if it appears.
    "S-RAZAK": "K.L Razak",
}

# Receipts-side outlet codes (no S- prefix; from outlet_mapping.py) -> the same
# canonical names, so /food_cost can join supplier purchases (receipts.outlet)
# to sales (sales_daily.outlet_canonical).
RECEIPTS_CODE_TO_CANONICAL: dict[str, str] = {
    "BISTRO7": "Bistro",
    "D": "D.U",
    "JAKEL": "Jakel",
    "KLANG": "Klang B.Emas",
    "SBESI": "SBESI",
    "SEK14": "Signature",
    "SEK15": "One Bistro",
    "SEK20": "SEK-20",
    "SEK6": "SEK-6",
    "VISTA": "Vista",
}

# Codes whose canonical mapping is not yet confirmed against a real email.
UNCONFIRMED_CODES = frozenset({"S-SBESI"})
# Codes we have never received yet (log info, don't warn) — see variance #7.
FUTURE_CODES = frozenset({"S-RAZAK"})

# Sections that may legitimately be absent for some outlets (variance #5):
#   stock         : only D.U, KLANG, SEK20, SEK6
#   deleted_items : only KLANG, SEK14, SEK20, VISTA
#   cashdrawer    : everyone EXCEPT SEK14, SEK20
OPTIONAL_SECTIONS = frozenset({"deleted_items", "stock", "cashdrawer"})

_SUBJECT_RE = re.compile(r"^\s*(S-\w+)\s+SHIFTCLOSE", re.IGNORECASE)


def extract_outlet_from_subject(subject) -> str | None:
    """Pull the ``S-XXX`` code out of a subject like ``S-KLANG SHIFTCLOSE (1499)``.

    Returns the upper-cased code (``S-KLANG``) or ``None`` if the subject does
    not match the expected shape.
    """
    if not isinstance(subject, str):
        return None
    m = _SUBJECT_RE.match(subject)
    return m.group(1).upper() if m else None


def extract_shift_no_from_subject(subject) -> str | None:
    """Pull the trailing ``(1499)`` shift number out of a subject, if present."""
    if not isinstance(subject, str):
        return None
    m = re.search(r"\((\d+)\)", subject)
    return m.group(1) if m else None


def canonical_outlet_for_code(code) -> str | None:
    """Map a subject code to its canonical outlet name, logging the documented
    edge cases (unconfirmed / never-seen-yet / unknown). Returns ``None`` for an
    unknown code so the caller can decide whether to skip or store raw."""
    if not code:
        logger.warning("No outlet code supplied (could not parse subject) — skipping outlet id")
        return None
    code = code.upper()
    canonical = OUTLET_CANONICAL_BY_CODE.get(code)
    if canonical is None:
        logger.warning("Unknown outlet subject code %r — ingesting without canonical outlet", code)
        return None
    if code in FUTURE_CODES:
        logger.info("First sighting of outlet code %r — using canonical %r", code, canonical)
    elif code in UNCONFIRMED_CODES:
        logger.warning(
            "Outlet code %r canonical mapping (%r) is UNCONFIRMED — verify against a real email",
            code, canonical,
        )
    return canonical


# --- file / bytes reading + normalisation -----------------------------------

def normalize_content(content: str) -> str:
    """Strip a leading BOM and normalise CRLF/CR to LF."""
    return content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def decode_shift_close_bytes(raw_bytes: bytes) -> str:
    """Decode raw attachment bytes, then normalise the BOM/newlines.

    Encoding is sniffed rather than assumed: production attachments are UTF-16
    (the variance analysis says with a BOM), but a file can also arrive as UTF-8
    (e.g. transcoded in transit). A blind ``decode('utf-16')`` would silently
    turn an even-length UTF-8/ASCII file into garbage, so:
      * UTF-16 BOM present              -> UTF-16
      * NUL bytes present (UTF-16 sans BOM) -> UTF-16 (replace)
      * otherwise                       -> UTF-8 (replace)
    """
    if not raw_bytes:
        return ""
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        content = raw_bytes.decode("utf-16", errors="replace")
    elif b"\x00" in raw_bytes[:4096]:
        content = raw_bytes.decode("utf-16", errors="replace")
    else:
        content = raw_bytes.decode("utf-8", errors="replace")
    return normalize_content(content)


def read_shift_close_file(filepath) -> str:
    """Read a shift-close TXT from disk and decode it (see
    ``decode_shift_close_bytes`` for the encoding sniffing)."""
    with open(filepath, "rb") as f:
        return decode_shift_close_bytes(f.read())


# --- shift classification ----------------------------------------------------

def determine_shift_type_and_business_date(shift_close_datetime: datetime):
    """Classify a shift close into (shift_type, business_date).

    24/7 outlets close around 19:00 (the "day" shift) and 07:00 (the "overnight"
    shift that started the previous evening). Anything outside those windows is
    ``unknown`` and keeps its own date so it is never silently merged.
    """
    hour = shift_close_datetime.hour
    if 17 <= hour <= 22:
        return "day", shift_close_datetime.date()
    if 5 <= hour <= 10:
        return "overnight", (shift_close_datetime - timedelta(days=1)).date()
    return "unknown", shift_close_datetime.date()


# --- low-level value parsing -------------------------------------------------

def parse_money(value) -> float | None:
    """Parse a money/number token: strips ``RM``, thousands commas and spaces.
    Returns ``None`` when there is no parseable number (so callers can default
    explicitly rather than guess zero)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"(?i)rm", "", s)
    s = s.replace(",", "").replace(" ", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_int(value) -> int | None:
    """Parse an integer token (e.g. a stock quantity), keeping the sign."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


_DATETIME_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",   # 5/25/2026 7:00:04 PM  (POS DATE field)
    "%m/%d/%Y %I:%M %p",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%b/%Y %H:%M:%S",      # 25/May/2026 19:00:04  (report header line)
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def parse_datetime(value) -> datetime | None:
    """Parse a POS timestamp. The summary ``DATE`` field is US-style
    ``M/D/YYYY h:mm:ss AM/PM``; a few other shapes are tolerated. Returns a naive
    datetime (Malaysia local time) or ``None``."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --- line helpers ------------------------------------------------------------

def _is_separator(line: str) -> bool:
    """True for a ruler line made only of =, -, _ or * (no letters/digits)."""
    s = line.strip()
    return bool(s) and set(s) <= set("=-_* ") and not any(c.isalnum() for c in s)


def _kv_split(line: str):
    """Split ``Label : value`` on the FIRST colon. Returns (label, value) or
    ``None`` (the value may itself contain colons, e.g. a time)."""
    if ":" not in line:
        return None
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _norm_label(key: str) -> str:
    """Normalise a summary label: drop non-letters (so ``CASH PAYMENT (-)`` ->
    ``CASH PAYMENT``), upper-case, collapse spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", "", key)).strip().upper()


def _columns(line: str):
    """Split a data row on runs of 2+ spaces into trimmed columns."""
    return [c for c in re.split(r"\s{2,}", line.strip()) if c != ""]


def _looks_money(tok: str) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+(?:\.\d+)?", tok.strip()))


def _looks_int(tok: str) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+", tok.strip()))


# --- summary + meta ----------------------------------------------------------

_SUMMARY_FIELD_BY_LABEL = {
    "TODAY SALES": "total_sales",
    "NET SALES": "net_sales",
    "TAX": "tax",
    "ROKOK TAX": "rokok_tax",
    "DISCOUNT": "discount",
}


def _extract_summary(lines):
    """Scan ``LABEL : value`` lines for the known money fields (first wins)."""
    out: dict = {}
    for line in lines:
        kv = _kv_split(line)
        if not kv:
            continue
        field = _SUMMARY_FIELD_BY_LABEL.get(_norm_label(kv[0]))
        if field and field not in out:
            out[field] = parse_money(kv[1])
    return out


def _extract_meta(lines):
    info = {
        "header_outlet_raw": None,
        "shift_no": None,
        "cashier": None,
        "close_time": None,
    }
    for line in lines:
        kv = _kv_split(line)
        if not kv:
            continue
        label = _norm_label(kv[0])
        if label in ("SHIFTNO", "SHIFT NO", "SHIFT") and not info["shift_no"]:
            info["shift_no"] = (re.sub(r"\D", "", kv[1]) or None)
        elif label == "DATE" and not info["close_time"]:
            info["close_time"] = parse_datetime(kv[1])
        elif label == "CASHIER" and info["cashier"] is None:
            v = kv[1].strip()
            info["cashier"] = None if v.lower() in ("", "null") else v
    for line in lines:
        s = line.strip()
        if s and not _is_separator(s):
            info["header_outlet_raw"] = s
            break
    return info


# --- section location + row parsing -----------------------------------------

def _section_rows(lines, banner_idx):
    """Rows of the block that starts at ``banner_idx``: skip to the first ruler
    after the banner (past the column header), then collect non-blank,
    non-ruler lines until the next ruler."""
    n = len(lines)
    i = banner_idx + 1
    while i < n and not _is_separator(lines[i]):
        i += 1
    i += 1  # past the first ruler
    rows = []
    while i < n and not _is_separator(lines[i]):
        s = lines[i].strip()
        if s:
            rows.append(s)
        i += 1
    return rows


def _find_item_banner(lines):
    for i, l in enumerate(lines):
        if re.search(r"GROUP WISE ITEM SALES\s*\(", l):
            return i
    return None


def _find_category_banner(lines):
    for i, l in enumerate(lines):
        if "GROUP WISE ITEM SALES" in l and "(" not in l:
            return i
    return None


def _find_banner(lines, pattern):
    for i, l in enumerate(lines):
        if re.search(pattern, l):
            return i
    return None


def _parse_items(rows):
    """ITEMNAME QTY AMOUNT rows -> [{qty, name, amount}]."""
    items = []
    for row in rows:
        cols = _columns(row)
        if len(cols) < 3:
            continue
        qty_tok, amt_tok = cols[-2], cols[-1]
        if not (_looks_int(qty_tok) and _looks_money(amt_tok)):
            continue
        items.append({
            "qty": parse_money(qty_tok),
            "name": " ".join(cols[:-2]).strip(),
            "amount": parse_money(amt_tok),
        })
    return items


def _parse_categories(rows):
    """SHIFTNO ITEMNAME AMOUNT rows -> [{label, amount}] (drops the shiftno)."""
    out = []
    for row in rows:
        cols = _columns(row)
        if len(cols) < 3:
            continue
        if not (_looks_int(cols[0]) and _looks_money(cols[-1])):
            continue
        out.append({"label": " ".join(cols[1:-1]).strip(), "amount": parse_money(cols[-1])})
    return out


def _parse_stock(rows):
    """ITEM NAME  STOCK  AMOUNT rows -> [{item, qty}]. STOCK is a signed integer
    (e.g. KLANG ``Kacang 2.00  -1218  -2436.00`` -> qty -1218)."""
    stock = []
    for row in rows:
        cols = _columns(row)
        if len(cols) < 3:
            continue
        stock_tok, amt_tok = cols[-2], cols[-1]
        if not (_looks_int(stock_tok) and _looks_money(amt_tok)):
            continue
        stock.append({"item": " ".join(cols[:-2]).strip(), "qty": parse_int(stock_tok)})
    return stock


def _parse_deleted(rows):
    """ITEMNAME QTY RATE TOTALAMT rows -> [{qty, name, amount}]. The interleaved
    operator/time lines (2 columns) are skipped."""
    out = []
    for row in rows:
        cols = _columns(row)
        if len(cols) < 4:
            continue
        qty_tok, total_tok = cols[-3], cols[-1]
        if not (_looks_int(qty_tok) and _looks_money(total_tok)):
            continue
        out.append({
            "qty": parse_money(qty_tok),
            "name": " ".join(cols[:-3]).strip(),
            "amount": parse_money(total_tok),
        })
    return out


def _parse_cashdrawer(lines, banner_idx):
    """CASHDRAWER OPEN log -> [{label, amount}]: the open count plus one entry
    per drawer-open event (``<datetime> <operator>``, no amount)."""
    out = []
    # "TOTAL TIMES OPEN :2" sits between the banner and the first ruler.
    for j in range(banner_idx, min(banner_idx + 5, len(lines))):
        kv = _kv_split(lines[j])
        if kv and _norm_label(kv[0]) == "TOTAL TIMES OPEN":
            out.append({"label": "TOTAL TIMES OPEN", "amount": parse_money(kv[1])})
            break
    for row in _section_rows(lines, banner_idx):
        cols = _columns(row)
        if len(cols) >= 2:
            out.append({"label": " ".join(cols[1:]).strip(), "amount": None})
    return out


def _parse_tender_grid(lines):
    """Pull the tender split (CASH / QR PAY) out of the denomination pipe grid:
    rows like ``|   TOTALCASH | 1010.00 |`` and ``|     QR PAY| 2907.40 |``."""
    found: dict = {}
    for line in lines:
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 2 or not _looks_money(cells[-1]):
            continue
        label = _norm_label(cells[0])
        if label in ("TOTALCASH", "QR PAY") and label not in found:
            found[label] = parse_money(cells[-1])
    payments = []
    if "TOTALCASH" in found:
        payments.append({"label": "CASH", "amount": found["TOTALCASH"]})
    if "QR PAY" in found:
        payments.append({"label": "QR PAY", "amount": found["QR PAY"]})
    return payments


# --- top-level parse ---------------------------------------------------------

def parse_shift_close(content: str) -> dict:
    """Parse one shift-close report body into a structured dict.

    Identity (outlet) is intentionally NOT derived here — the caller supplies it
    from the email subject. ``shift_type`` / ``shift_business_date`` are computed
    from the close time when available.

    Optional sections (stock, deleted items, cashdrawer-open) are simply absent
    from ``sections_present`` when the report omits them — never an error.
    """
    content = normalize_content(content or "")
    lines = content.split("\n")

    meta = _extract_meta(lines)
    summary = _extract_summary(lines)

    sections_present = []

    item_idx = _find_item_banner(lines)
    items = _parse_items(_section_rows(lines, item_idx)) if item_idx is not None else []
    if items:
        sections_present.append("items")

    cat_idx = _find_category_banner(lines)
    categories = _parse_categories(_section_rows(lines, cat_idx)) if cat_idx is not None else []
    if categories:
        sections_present.append("categories")

    stock_idx = _find_banner(lines, r"^\s*STOCK ON\b")
    stock = _parse_stock(_section_rows(lines, stock_idx)) if stock_idx is not None else []
    if stock:
        sections_present.append("stock")

    del_idx = _find_banner(lines, r"DELETED ITEM BY ADMIN")
    deleted_items = _parse_deleted(_section_rows(lines, del_idx)) if del_idx is not None else []
    if deleted_items:
        sections_present.append("deleted_items")

    cash_idx = _find_banner(lines, r"CASHDRAWER OPEN")
    cashdrawer = _parse_cashdrawer(lines, cash_idx) if cash_idx is not None else []
    if cashdrawer:
        sections_present.append("cashdrawer")

    payments = _parse_tender_grid(lines)
    if payments:
        sections_present.append("payments")

    tax = summary.get("tax")
    tax_breakdown = []
    if tax:
        tax_breakdown.append({"label": "TAX", "amount": tax})
    if summary.get("rokok_tax"):
        tax_breakdown.append({"label": "ROKOK TAX", "amount": summary["rokok_tax"]})
    if tax_breakdown:
        sections_present.append("tax")

    discount = summary.get("discount")
    discounts = []
    if discount:
        discounts.append({"label": "DISCOUNT", "amount": discount})
        sections_present.append("discounts")

    close_time = meta.get("close_time")
    if close_time is not None:
        shift_type, business_date = determine_shift_type_and_business_date(close_time)
    else:
        shift_type, business_date = "unknown", None

    return {
        "header_outlet_raw": meta.get("header_outlet_raw"),
        "terminal": None,
        "shift_no": meta.get("shift_no"),
        "cashier": meta.get("cashier"),
        "open_time": None,
        "close_time": close_time,
        "shift_type": shift_type,
        "shift_business_date": business_date,
        "gross_sales": None,
        "discount": discount,
        "service_charge": None,
        "tax": tax if tax is not None else (0.0 if summary else None),
        "net_sales": summary.get("net_sales"),
        "total_sales": summary.get("total_sales"),
        "categories": categories,
        "tax_breakdown": tax_breakdown,
        "discounts": discounts,
        "payments": payments,
        "items": items,
        "deleted_items": deleted_items,
        "stock": stock,
        "cashdrawer": cashdrawer,
        "sections_present": sorted(set(sections_present)),
    }
