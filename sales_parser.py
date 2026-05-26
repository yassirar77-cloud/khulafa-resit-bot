"""POS shift-close TXT parser (PR #35).

Pure functions that turn one POS "shift close" report (a UTF-16, CRLF text
file emailed as an attachment from the master inbox) into a structured dict the
ingestion layer writes to ``sales_daily`` + child tables.

Two hard rules from the production variance analysis (these OVERRIDE any
assumption a header might suggest):

  1. Outlet identity comes from the EMAIL SUBJECT only — never from the TXT
     "Outlet" header, which is ambiguous/inconsistent across outlets. The
     header value is kept as ``header_outlet_raw`` for debugging only.
  2. Encoding is UTF-16 with a BOM and CRLF line endings, but some files slip
     through as UTF-8; decode defensively and normalise newlines.

24/7 operation means two shifts per outlet per day (close ~07:00 and ~19:00
MY). ``determine_shift_type_and_business_date`` assigns each close to a
``day``/``overnight``/``unknown`` shift and the business date it belongs to.

NOTE (format provenance): the exact section headers / column layout below are
modelled on the variance-analysis findings and the 10 fixture files in
``tests/fixtures/sales/``. Section detection is label-anchored and tolerant, so
adapting to the real POS output is a matter of tuning ``_SECTION_TITLES`` and
the line regexes here — no structural change to the ingestion or schema.
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


def read_shift_close_file(filepath) -> str:
    """Read a shift-close TXT from disk: UTF-16 (with BOM) first, UTF-8 on
    fallback, then normalise the BOM/newlines."""
    try:
        with open(filepath, "r", encoding="utf-16") as f:
            content = f.read()
    except UnicodeError:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    return normalize_content(content)


def decode_shift_close_bytes(raw_bytes: bytes) -> str:
    """Decode raw attachment bytes: UTF-16 first, UTF-8 (replace) on fallback,
    then normalise the BOM/newlines."""
    if raw_bytes is None:
        return ""
    try:
        content = raw_bytes.decode("utf-16")
    except UnicodeError:
        content = raw_bytes.decode("utf-8", errors="replace")
    return normalize_content(content)


# --- shift classification ----------------------------------------------------

def determine_shift_type_and_business_date(shift_close_datetime: datetime):
    """Classify a shift close into (shift_type, business_date).

    24/7 outlets close around 19:00 (the "day" shift) and 07:00 (the
    "overnight" shift that started the previous evening). Anything outside those
    windows is ``unknown`` and keeps its own date so it is never silently merged.
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
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def parse_datetime(value) -> datetime | None:
    """Parse a POS timestamp. Tries DD/MM/YYYY and ISO-ish forms (the POS prints
    day-first). Returns a naive datetime (Malaysia local time) or ``None``."""
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


# --- section detection -------------------------------------------------------

# Canonical section titles -> internal key. Detection is case-insensitive on
# the stripped line. Tune this set (and the per-section parsers) to match the
# real POS output.
_SECTION_TITLES: dict[str, str] = {
    "SALES SUMMARY": "summary",
    "SALES BY CATEGORY": "categories",
    "TAX": "tax",
    "TAX BREAKDOWN": "tax",
    "DISCOUNTS": "discounts",
    "DISCOUNT": "discounts",
    "PAYMENT BREAKDOWN": "payments",
    "PAYMENTS": "payments",
    "ITEMS SOLD": "items",
    "DELETED ITEMS": "deleted_items",
    "STOCK REPORT": "stock",
    "CASH DRAWER": "cashdrawer",
    "CASH DRAWER OPEN": "cashdrawer",
    "END OF REPORT": "_end",
}

# A line that is only separator punctuation (===, ---, ___) carries no data.
_SEPARATOR_RE = re.compile(r"^[\s=\-_*]+$")


def _is_separator(line: str) -> bool:
    return bool(_SEPARATOR_RE.match(line)) and not any(c.isalnum() for c in line)


def _split_sections(content: str):
    """Return ``(meta_lines, {section_key: [lines]})``.

    Everything before the first recognised section header is "meta" (the
    Outlet/Terminal/Shift/Cashier/times block). Lines made only of separator
    characters are dropped. The END OF REPORT marker terminates parsing.
    """
    meta: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in content.split("\n"):
        line = raw.rstrip()
        if not line.strip() or _is_separator(line):
            continue
        key = _SECTION_TITLES.get(line.strip().upper())
        if key is not None:
            if key == "_end":
                break
            current = key
            sections.setdefault(current, [])
            continue
        if current is None:
            meta.append(line)
        else:
            sections[current].append(line)
    return meta, sections


_KV_RE = re.compile(r"^(.*?):\s*(.+)$")


def _parse_kv_lines(lines):
    """Parse ``Key : value`` lines into an ordered list of (key, raw_value)."""
    out = []
    for line in lines:
        m = _KV_RE.match(line.strip())
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def _parse_columnar(lines):
    """Split each line on runs of 2+ spaces into columns. Header rows (no
    numeric trailing column) are returned too; callers filter them."""
    rows = []
    for line in lines:
        parts = re.split(r"\s{2,}", line.strip())
        if parts:
            rows.append(parts)
    return rows


# --- per-section parsers -----------------------------------------------------

def _parse_meta(meta_lines):
    info = {
        "header_outlet_raw": None,
        "terminal": None,
        "shift_no": None,
        "cashier": None,
        "open_time": None,
        "close_time": None,
    }
    for key, value in _parse_kv_lines(meta_lines):
        k = key.upper()
        if k == "OUTLET":
            info["header_outlet_raw"] = value
        elif k == "TERMINAL":
            info["terminal"] = value
        elif k in ("SHIFT NO", "SHIFT", "SHIFT NUMBER", "SHIFT #"):
            info["shift_no"] = value.strip()
        elif k == "CASHIER":
            info["cashier"] = value
        elif k in ("OPEN TIME", "OPEN", "OPENED"):
            info["open_time"] = parse_datetime(value)
        elif k in ("CLOSE TIME", "CLOSE", "CLOSED"):
            info["close_time"] = parse_datetime(value)
    return info


def _parse_summary(lines):
    summary = {
        "gross_sales": None,
        "discount": None,
        "service_charge": None,
        "tax": None,
        "net_sales": None,
        "total_sales": None,
    }
    total_collected = None
    total_generic = None
    for key, value in _parse_kv_lines(lines):
        k = key.upper()
        amount = parse_money(value)
        if "GROSS" in k:
            summary["gross_sales"] = amount
        elif "SERVICE" in k:
            summary["service_charge"] = amount
        elif "DISCOUNT" in k:
            summary["discount"] = amount
        elif "TAX" in k or "SST" in k or "GST" in k:
            summary["tax"] = amount
        elif "NET" in k:
            summary["net_sales"] = amount
        elif "TOTAL COLLECTED" in k or "TOTAL COLLECT" in k:
            total_collected = amount
        elif k.startswith("TOTAL"):
            total_generic = amount
    # total_sales preference: explicit collected total -> net sales -> any total.
    summary["total_sales"] = next(
        (v for v in (total_collected, summary["net_sales"], total_generic) if v is not None),
        None,
    )
    return summary


def _parse_amount_kv_section(lines):
    """Generic ``Label : amount`` section (categories / tax / discounts /
    payments). Returns a list of {label, amount}."""
    out = []
    for key, value in _parse_kv_lines(lines):
        out.append({"label": key.strip(), "amount": parse_money(value)})
    return out


def _looks_like_qty(token) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", token.strip()))


def _looks_like_money(token) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+\.\d{2}", token.strip()))


def _parse_items(lines):
    """Parse ``Qty  Item name  Amount`` columnar rows. Skips the header row and
    anything that does not have a leading qty and trailing money column."""
    items = []
    for parts in _parse_columnar(lines):
        if len(parts) < 3:
            continue
        qty_tok, amount_tok = parts[0], parts[-1]
        if not (_looks_like_qty(qty_tok) and _looks_like_money(amount_tok)):
            continue  # header row ("Qty Item Amount") or malformed
        name = " ".join(parts[1:-1]).strip()
        items.append({
            "qty": parse_money(qty_tok),
            "name": name,
            "amount": parse_money(amount_tok),
        })
    return items


def _parse_stock(lines):
    """Parse ``Item name  qty`` rows. Quantity is an integer and may be negative
    (e.g. KLANG ``Kacang -1218``). Skips the header row."""
    stock = []
    for parts in _parse_columnar(lines):
        if len(parts) < 2:
            continue
        name, qty_tok = " ".join(parts[:-1]).strip(), parts[-1]
        if not re.fullmatch(r"-?\d[\d,]*", qty_tok.strip()):
            continue  # header ("Item Qty") or malformed
        stock.append({"item": name, "qty": parse_int(qty_tok)})
    return stock


def _parse_cashdrawer(lines):
    """Parse the cash-drawer block (``Label : amount`` lines)."""
    return _parse_amount_kv_section(lines)


# --- top-level parse ---------------------------------------------------------

# Sections that may legitimately be absent for some outlets (variance #5).
OPTIONAL_SECTIONS = frozenset({"deleted_items", "stock", "cashdrawer"})


def parse_shift_close(content: str) -> dict:
    """Parse one shift-close report body into a structured dict.

    Identity (outlet) is intentionally NOT derived here — the caller supplies it
    from the email subject. ``shift_type`` / ``shift_business_date`` are computed
    from the close time when available.

    Optional sections (deleted items, stock report, cash drawer) are simply
    absent from ``sections_present`` when the report omits them — never an error.
    """
    content = normalize_content(content or "")
    meta_lines, sections = _split_sections(content)

    meta = _parse_meta(meta_lines)
    summary = _parse_summary(sections.get("summary", []))

    categories = _parse_amount_kv_section(sections.get("categories", []))
    tax_breakdown = _parse_amount_kv_section(sections.get("tax", []))
    discounts = _parse_amount_kv_section(sections.get("discounts", []))
    payments = _parse_amount_kv_section(sections.get("payments", []))
    items = _parse_items(sections.get("items", []))
    deleted_items = _parse_items(sections.get("deleted_items", []))
    stock = _parse_stock(sections.get("stock", []))
    cashdrawer = _parse_cashdrawer(sections.get("cashdrawer", []))

    # Tax: prefer the summary line; fall back to the sum of a tax breakdown.
    tax = summary.get("tax")
    if tax is None and tax_breakdown:
        tax = sum(t["amount"] for t in tax_breakdown if t["amount"] is not None) or 0.0

    close_time = meta.get("close_time")
    if close_time is not None:
        shift_type, business_date = determine_shift_type_and_business_date(close_time)
    else:
        shift_type, business_date = "unknown", None

    sections_present = sorted(sections.keys())

    return {
        "header_outlet_raw": meta.get("header_outlet_raw"),
        "terminal": meta.get("terminal"),
        "shift_no": meta.get("shift_no"),
        "cashier": meta.get("cashier"),
        "open_time": meta.get("open_time"),
        "close_time": close_time,
        "shift_type": shift_type,
        "shift_business_date": business_date,
        "gross_sales": summary.get("gross_sales"),
        "discount": summary.get("discount"),
        "service_charge": summary.get("service_charge"),
        "tax": tax,
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
        "sections_present": sections_present,
    }
