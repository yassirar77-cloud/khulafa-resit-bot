"""Parser for the POS MONTHLY REPORT ``.TXT`` attachment.

Same generator as the S-/D- shift files (see ``sales_parser``), so the same
hazards apply — and the fixtures prove each one:

* zeros print as ``.00``/``.60``, never ``0.00`` — ``parse_money`` handles it
* line endings are CRLF and the file may be UTF-16 — ``decode_shift_close_bytes``
* item names carry LEADING spaces (``" Nasi Goreng Daging"``), underscores
  (``" Tambah_nasi"``) and DOUBLE spaces (``"Barli  Panas"``)

That last one is why this module does not reuse ``sales_parser._columns``, which
splits on runs of 2+ spaces: it would turn ``"Barli  Panas"`` into two columns
and silently invent an item. Rows here are split from the RIGHT instead —
trailing numeric columns are peeled off and everything left of them is the name,
verbatim. Leading whitespace is preserved because it is real: the menu-hygiene
sheet exists precisely to find the SKU that differs from its twin only by a
leading space.

Section detection is banner-driven and tolerant. A banner is matched by keyword,
not by exact text or line number, and an absent section yields an empty list
rather than an exception — a POS that stops printing STAFF WISE SALES must not
take the month's P&L down with it. Every section that WAS found is recorded in
``sections_found`` so callers can tell "empty" from "missing".
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sales_parser import (  # noqa: F401 - decode/normalize re-exported for ingest
    decode_shift_close_bytes,
    normalize_content,
    parse_int,
    parse_money,
)

logger = logging.getLogger(__name__)

#: ``MONTHLY REPORT-Damansara ON Jul 2026`` — outlet may contain spaces.
_HEADER_RE = re.compile(
    r"MONTHLY\s+REPORT\s*-\s*(?P<outlet>.+?)\s+ON\s+(?P<month>[A-Za-z]{3,9})\s*[/,-]?\s*(?P<year>\d{4})",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONEY_TOKEN = r"-?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
_MONEY_RE = re.compile(rf"^{_MONEY_TOKEN}$")

# A date cell in the daily table: 01, 1/7, 01/07/2026, 2026-07-01 all appear
# across POS versions, so accept anything starting with digits and containing
# only digits and separators.
_DATE_CELL_RE = re.compile(r"^\d{1,4}(?:[/\-.]\d{1,4}){0,2}$")


def _is_separator(line: str) -> bool:
    """True for a ruler line made only of =, -, _, * or | (no letters/digits)."""
    s = line.strip()
    return bool(s) and set(s) <= set("=-_*| ") and not any(c.isalnum() for c in s)


def _is_blank(line: str) -> bool:
    return not line.strip()


def split_trailing_numbers(line: str, count: int) -> tuple[str, list[float]] | None:
    """Peel ``count`` numeric columns off the RIGHT of a row.

    Returns ``(label, [values…])`` with the values in printed order, or ``None``
    when the row does not end in that many numbers. ``label`` keeps its LEADING
    whitespace and internal spacing exactly as printed — only trailing space is
    removed — because both are significant (see the module docstring).

    This is the "split on the LAST whitespace run" rule, applied ``count``
    times. Splitting from the left would break on every name containing a space,
    which is most of them.
    """
    if not isinstance(line, str) or not line.strip():
        return None
    rest = line.rstrip()
    values: list[float] = []
    for _ in range(count):
        match = re.search(r"\s(\S+)$", rest)
        if match is None:
            return None
        token = match.group(1)
        if not _MONEY_RE.match(token):
            return None
        parsed = parse_money(token)
        if parsed is None:
            return None
        values.append(parsed)
        rest = rest[: match.start()].rstrip()
    if not rest.strip():
        return None
    values.reverse()
    # rstrip only: a leading space is part of the name.
    return (rest, values)


def _kv(line: str) -> tuple[str, str] | None:
    """Split ``LABEL : value`` on the first colon."""
    if ":" not in line:
        return None
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _norm_label(key: str) -> str:
    """Upper-case, drop non-letters, collapse spaces: ``TOTAL SALES (+)`` ->
    ``TOTAL SALES``."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", "", str(key))).strip().upper()


# --- section banners --------------------------------------------------------
# Each entry is (section_key, banner_regex). Matched against the STRIPPED line,
# case-insensitively. Order matters only in that the first match on a line wins.
_BANNERS: tuple[tuple[str, re.Pattern], ...] = (
    ("daily_sales", re.compile(r"DAY\s*WISE|DAILY\s+SALES|DATEWISE|DATE\s*WISE", re.I)),
    ("payouts", re.compile(r"PURCHASE\s+DETAILS|PAYOUT\s+DETAILS", re.I)),
    ("pinjam", re.compile(r"STAFF\s+PINJAM|PINJAM\s+DETAILS", re.I)),
    ("salary_block", re.compile(r"PAY\s+SALARY\s+DETAILS|SALARY\s+DETAILS", re.I)),
    ("itemwise", re.compile(r"^\s*ITEMWISE\s+SALES|^\s*ITEM\s*WISE\s+SALES", re.I)),
    ("subgroup_itemwise", re.compile(r"SUB\s*GROUP\s+ITEMWISE", re.I)),
    ("staff_sales", re.compile(r"STAFF\s*WISE\s+SALES", re.I)),
    ("staff_advance", re.compile(r"STAFF\s+ADVANCE|ADVANCE\s+DETAILS|SALARY\s+ADVANCE", re.I)),
)

# Column-header lines that sit directly under a banner and must not be parsed
# as data.
_HEADER_HINT_RE = re.compile(
    r"^\s*(TRNO|SHIFTNO|ITEMNAME|STAFFNAME|DATE|SNO|NO)\b", re.I
)

# A row that closes a section: "TOTAL PURCHASE :974.00", "TOTAL :3050.40".
_SECTION_TOTAL_RE = re.compile(r"^\s*(?:GRAND\s+)?TOTAL\b", re.I)


def _section_bounds(lines: list[str]) -> dict[str, list[tuple[int, int]]]:
    """Map each section key to the (start, end) line ranges of its data rows.

    A section runs from just after its banner to the next banner or its own
    TOTAL line. Sections can repeat (the POS prints one block per shift in some
    versions), so every occurrence is kept and callers concatenate.
    """
    banner_hits: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        for key, pattern in _BANNERS:
            if pattern.search(line):
                banner_hits.append((index, key))
                break

    bounds: dict[str, list[tuple[int, int]]] = {}
    for position, (index, key) in enumerate(banner_hits):
        start = index + 1
        next_banner = (
            banner_hits[position + 1][0] if position + 1 < len(banner_hits) else len(lines)
        )
        end = next_banner
        for scan in range(start, next_banner):
            if _SECTION_TOTAL_RE.match(lines[scan]) and not _is_separator(lines[scan]):
                end = scan
                break
        bounds.setdefault(key, []).append((start, end))
    return bounds


def _data_rows(lines: list[str], ranges: list[tuple[int, int]]) -> list[str]:
    """Concatenate a section's rows, dropping separators, blanks and headers."""
    rows: list[str] = []
    for start, end in ranges:
        for line in lines[start:end]:
            if _is_blank(line) or _is_separator(line) or _HEADER_HINT_RE.match(line):
                continue
            rows.append(line)
    return rows


# --- row parsers ------------------------------------------------------------


def _parse_payout_rows(rows: list[str]) -> list[dict]:
    """``14415  PAY TO AYAM BESTARI        764.00`` -> description + amount.

    The leading transaction number is peeled off when present; it is not always
    printed, so its absence is not an error. The description keeps its internal
    spacing — ``PAY [SALARY] TO KALEEL`` must survive intact for the
    reclassification rules to read it.
    """
    out: list[dict] = []
    for line in rows:
        split = split_trailing_numbers(line, 1)
        if split is None:
            logger.debug("monthly parser: unparsed payout row %r", line)
            continue
        label, (amount,) = split
        trno = None
        head = label.lstrip()
        leading = re.match(r"^(\d+)\s+(.*)$", head)
        if leading:
            trno, description = leading.group(1), leading.group(2)
        else:
            description = head
        description = description.strip()
        if not description:
            continue
        out.append({"trno": trno, "description": description, "amount": amount})
    return out


def _parse_itemwise_rows(rows: list[str]) -> list[dict]:
    """``  Barli  Panas              1          2.30`` -> name, qty, amount.

    ``item_name`` is stored VERBATIM including leading spaces and double spaces;
    ``menu_hygiene`` needs the raw string to find near-duplicate SKUs.
    """
    out: list[dict] = []
    for line in rows:
        split = split_trailing_numbers(line, 2)
        if split is None:
            logger.debug("monthly parser: unparsed itemwise row %r", line)
            continue
        name, (qty, amount) = split
        if not name.strip():
            continue
        out.append({"item_name": name, "qty": qty, "amount": amount})
    return out


def _parse_staff_sales_rows(rows: list[str]) -> list[dict]:
    """``2384    RAFAYUDEEN              867.70`` -> staff_name, amount.

    The optional leading shift number is dropped. Rows whose "name" is entirely
    numeric are machine-sales rows that share the block in some POS versions —
    skipped rather than stored as a staff member called "1153".
    """
    out: list[dict] = []
    for line in rows:
        split = split_trailing_numbers(line, 1)
        if split is None:
            continue
        label, (amount,) = split
        name = re.sub(r"^\s*\d+\s+", "", label).strip()
        if not name or re.fullmatch(r"[\d\s/]+", name):
            continue
        out.append({"staff_name": name, "amount": amount})
    return out


def _parse_staff_advance_rows(rows: list[str]) -> list[dict]:
    """``AHMAD          200.00      1800.00`` -> staff_name, advance, netsal.

    Falls back to advance-only when a single number is printed, leaving
    ``netsal`` NULL rather than duplicating the advance into it.
    """
    out: list[dict] = []
    for line in rows:
        split = split_trailing_numbers(line, 2)
        if split is not None:
            label, (advance, netsal) = split
        else:
            single = split_trailing_numbers(line, 1)
            if single is None:
                continue
            label, (advance,) = single
            netsal = None
        name = re.sub(r"^\s*\d+\s+", "", label).strip()
        if not name or re.fullmatch(r"[\d\s/]+", name):
            continue
        out.append({"staff_name": name, "advance": advance, "netsal": netsal})
    return out


def _parse_daily_sales_rows(rows: list[str]) -> list[dict]:
    """``01/07/2026      4,120.50     820.00      .00`` -> date, sale, payout, tax.

    Trailing columns are peeled right-to-left, so a POS printing only
    ``date  sale`` still parses with payout/tax left NULL rather than shifting
    the sale into the wrong field.
    """
    out: list[dict] = []
    for line in rows:
        parsed = None
        for count in (3, 2, 1):
            split = split_trailing_numbers(line, count)
            if split is not None:
                parsed = (split[0], split[1], count)
                break
        if parsed is None:
            continue
        label, values, count = parsed
        day = label.strip().split()[0] if label.strip() else ""
        if not _DATE_CELL_RE.match(day):
            continue
        sale = values[0] if count >= 1 else None
        payout = values[1] if count >= 2 else None
        tax = values[2] if count >= 3 else None
        out.append({"date_cell": day, "sale": sale, "payout": payout, "tax": tax})
    return out


# --- header / totals --------------------------------------------------------

#: Normalised label -> ``monthly_report`` column.
_HEADER_FIELDS = {
    "TOTAL SALES": "total_sales",
    "NET SALES": "net_sales",
    "TOTAL BANK SALES": "total_bank_sales",
    "TOTAL FP SALES": "total_fp_sales",
    "TOTAL PAYOUTS": "total_payouts",
    "TOTAL PAYOUT": "total_payouts",
    "TOTAL PURCHASE": "total_purchase",
    "TOTAL PINJAM": "total_pinjam",
    "PAYOUT PINJAM": "payout_pinjam",
    "TOTAL PAY SALARY": "total_pay_salary",
    "TOTAL PAIKKASU": "total_paikkasu",
    "PAIKKASU": "total_paikkasu",
    "TAX": "tax",
    "DISCOUNT": "discount",
    "ROUNDED": "rounded",
    "TOTAL CUSTOMERS": "total_customers",
    "TODAYS CUSTOMERS": "total_customers",
    "QR PAY": "qr_pay",
    "GRAB PAY": "grab_pay",
    "TOTAL CASH": "total_cash",
    "TOTALCASH": "total_cash",
}

_INT_FIELDS = frozenset({"total_customers"})


def _parse_header_totals(lines: list[str]) -> dict:
    """Collect every ``LABEL : amount`` and pipe-table total into one dict.

    Later occurrences do NOT overwrite earlier ones: the monthly file repeats
    some labels per-shift below the summary, and the first (summary) value is
    the month's. Values that do not parse as money are skipped rather than
    stored as ``None`` over a good earlier read.
    """
    totals: dict[str, Any] = {}
    for raw in lines:
        line = raw.strip().strip("|").strip()
        if not line or _is_separator(raw):
            continue
        pair = _kv(line)
        if pair is None:
            # Pipe-table rows print as ``|   TOTAL SALE|   3086.00 |``.
            if "|" in raw:
                cells = [c.strip() for c in raw.strip().strip("|").split("|")]
                if len(cells) >= 2 and cells[-1]:
                    pair = (cells[-2], cells[-1])
            if pair is None:
                continue
        label, value = pair
        field = _HEADER_FIELDS.get(_norm_label(label))
        if field is None or field in totals:
            continue
        parsed = parse_int(value) if field in _INT_FIELDS else parse_money(value)
        if parsed is not None:
            totals[field] = parsed
    return totals


def parse_header(content: str) -> dict:
    """Extract outlet code and period from the ``MONTHLY REPORT-…`` line.

    Returns ``{"outlet_code", "period", "month_name", "year"}`` with ``None``
    values when the header is absent — the ingest treats that as "not a monthly
    report" rather than guessing the period from the filename.
    """
    empty = {"outlet_code": None, "period": None, "month_name": None, "year": None}
    if not isinstance(content, str):
        return empty
    match = _HEADER_RE.search(content)
    if match is None:
        return empty
    outlet = re.sub(r"\s+", " ", match.group("outlet")).strip()
    month_name = match.group("month")
    year = int(match.group("year"))
    month_no = _MONTHS.get(month_name[:3].lower())
    period = f"{year:04d}-{month_no:02d}" if month_no else None
    return {
        "outlet_code": outlet.upper(),
        "period": period,
        "month_name": month_name,
        "year": year,
    }


def parse_monthly_report(content: str) -> dict:
    """Parse a monthly report into the six section lists plus header totals.

    Returns::

        {
          "outlet_code", "period",            # from the header line
          "totals": {...},                    # monthly_report columns
          "daily_sales": [...],               # date_cell, sale, payout, tax
          "payouts": [...],                   # trno, description, amount
          "itemwise": [...],                  # item_name (verbatim), qty, amount
          "staff_sales": [...],               # staff_name, amount
          "staff_advance": [...],             # staff_name, advance, netsal
          "salary_block": [...],              # the mislabelled supplier block
          "pinjam": [...],                    # ADVANCE TO <staff> rows
          "sections_found": [...],            # banners actually seen
          "raw_line_count": int,
        }

    Never raises on malformed input: an unparseable row is logged and skipped so
    one bad line cannot cost the month its other 400.
    """
    if not isinstance(content, str) or not content.strip():
        return {
            "outlet_code": None, "period": None, "totals": {},
            "daily_sales": [], "payouts": [], "itemwise": [], "staff_sales": [],
            "staff_advance": [], "salary_block": [], "pinjam": [],
            "sections_found": [], "raw_line_count": 0,
        }

    text = normalize_content(content)
    lines = text.split("\n")
    header = parse_header(text)
    bounds = _section_bounds(lines)

    def rows_for(key: str) -> list[str]:
        return _data_rows(lines, bounds.get(key, []))

    result = {
        "outlet_code": header["outlet_code"],
        "period": header["period"],
        "totals": _parse_header_totals(lines),
        "daily_sales": _parse_daily_sales_rows(rows_for("daily_sales")),
        "payouts": _parse_payout_rows(rows_for("payouts")),
        "itemwise": _parse_itemwise_rows(rows_for("itemwise")),
        "staff_sales": _parse_staff_sales_rows(rows_for("staff_sales")),
        "staff_advance": _parse_staff_advance_rows(rows_for("staff_advance")),
        "salary_block": _parse_payout_rows(rows_for("salary_block")),
        "pinjam": _parse_payout_rows(rows_for("pinjam")),
        "sections_found": sorted(bounds.keys()),
        "raw_line_count": len(lines),
    }
    logger.info(
        "monthly parser: outlet=%r period=%r sections=%s daily=%d payouts=%d "
        "items=%d staff=%d advances=%d salary_block=%d pinjam=%d",
        result["outlet_code"], result["period"], result["sections_found"],
        len(result["daily_sales"]), len(result["payouts"]), len(result["itemwise"]),
        len(result["staff_sales"]), len(result["staff_advance"]),
        len(result["salary_block"]), len(result["pinjam"]),
    )
    return result
