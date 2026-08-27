"""One outlet's month of supplier invoices, exported as three CSVs.

Backs ``/invoices_export <outlet> <YYYY-MM>``. The director periodically
needs a month of one shop's bills in a spreadsheet — to hand to the
accountant, to argue a supplier's price, to check a shop against its own
history — and the chat commands only ever answer one question at a time.
This dumps the raw rows instead: suppliers rolled up, one row per bill,
and one row per line item.

**Line items still live in ``receipts.items`` (jsonb).** There is no
``receipt_items`` table in this repo, and the bot talks to Supabase over
PostgREST only (no psycopg, no raw SQL) — so the ``jsonb_array_elements``
unnest this export was specified with is done here in Python instead, key
for key:

* name  -> ``coalesce(it->>'item', it->>'name')``
* qty   -> ``coalesce(it->>'qty', it->>'quantity')``

The keys really are inconsistent across rows: today's OCR normaliser
(``items_utils.normalize_items``) writes ``{"name", "qty", "price"}``, but
older rows carry ``item``/``quantity``. Coalesce is on *presence*, exactly
like SQL's — a row with ``qty: "2 pcs"`` uses that and comes out blank; it
does NOT fall through to ``quantity``.

qty and price are only cast when they are a plain unsigned decimal
(``^[0-9]+(\\.[0-9]+)?$``); anything else — "2 pcs", "RM19.80", "-3" —
is emitted blank rather than guessed at, and a receipt with garbage lines
still gets its row in the bills CSV.

The items CSV carries a ``line_sum_vs_total`` column per receipt —
``sum(qty x price) / receipts.total`` — because the OCR is known to
over-count lines (``ocr_quality.total_conflicts_with_item_sum`` fires on
the same mismatch). A column of 1.8s means that month's item detail is
inflated and only the bill totals can be trusted.

Bad *data* never raises — a garbage line is blanked, a garbage row is
dropped. A failed *read* does propagate, unlike the scheduled reports:
this is an interactive command whose handler already answers with "failed
to read", and silently reporting an empty month during an outage would be
worse than saying so.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date
from typing import Any, Iterator

from db_pagination import iter_pages
from money_utils import normalize_total

logger = logging.getLogger(__name__)

RECEIPTS_TABLE = "receipts"

# A month of one outlet's receipts is small; the page size only bounds how
# much is held at once while walking it.
PAGE_SIZE = 500

USAGE = (
    "Usage: /invoices_export <outlet> <YYYY-MM>\n"
    "Example: /invoices_export bistro 2026-07\n"
    "The outlet is a partial, case-insensitive match on the receipts' "
    "outlet name."
)

# Strictly YYYY-MM — this command exports a named month, so "2026-7" and
# "last" are rejected rather than interpreted.
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

# The gate on casting a qty/price cell. Deliberately narrow: no sign, no
# separators, no currency. Anything else is unusable line data, not a
# number to be rescued.
_NUMERIC_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")

# How close sum(qty x price) must land to the bill total to count as
# reconciled, as a fraction of the total.
RECONCILE_TOLERANCE = 0.05

# Slack for binary float representation at the tolerance boundary only —
# far too small to move a real bill in or out of the band.
_TOLERANCE_EPSILON = 1e-9

_UNKNOWN_MERCHANT = "UNKNOWN"

SUPPLIERS_HEADER = ("merchant", "bill_count", "total_amount")
BILLS_HEADER = ("receipt_date", "merchant", "total", "receipt_id")
ITEMS_HEADER = (
    "receipt_date", "merchant", "item", "qty", "price",
    "bill_total", "receipt_id", "line_sum_vs_total",
)

_FILENAME_SAFE_RE = re.compile(r"[^a-z0-9]+")


# --- argument parsing --------------------------------------------------------

def parse_month(value: Any) -> tuple[int, int] | None:
    """``"2026-07"`` -> ``(2026, 7)``. ``None`` on anything else."""
    match = _MONTH_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    # Year 0 has no ``datetime.date``; reject it here so an absurd argument
    # is a usage error rather than a failed export.
    if not 1 <= month <= 12 or year < 1:
        return None
    return year, month


def month_window(year: int, month: int) -> tuple[str, str]:
    """Half-open ISO window ``[first day, first day of next month)``.

    Half-open on purpose: an inclusive upper bound has to know whether the
    month has 28/29/30/31 days, and a ``receipt_date`` that ever becomes a
    timestamp would silently drop the last day. ``>= start`` / ``< end``
    is correct for both.
    """
    first = date(year, month, 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    # The upper bound is formatted, not constructed: December 9999 would roll
    # past ``date``'s MAXYEAR, and this is only ever a comparison string.
    return first.isoformat(), f"{next_year:04d}-{next_month:02d}-01"


def month_label(year: int, month: int) -> str:
    """``(2026, 7)`` -> ``"2026-07"`` — what the user typed, normalised."""
    return f"{year:04d}-{month:02d}"


# --- outlet resolution -------------------------------------------------------

def distinct_outlets(client) -> list[str]:
    """Every distinct non-blank ``receipts.outlet``, case-insensitively sorted.

    PostgREST has no DISTINCT, so the column is walked a page at a time and
    deduped here — only the handful of names is ever held, not the rows.
    Every value is offered, ``"UNKNOWN"`` included: it is a real bucket of
    receipts whose chat didn't map to a shop, and hiding it would make it
    unexportable.
    """
    def _build():
        return (
            client.table(RECEIPTS_TABLE)
            .select("outlet")
            .order("id", desc=False)
        )

    found: set[str] = set()
    for page in iter_pages(_build):
        for row in page:
            if not isinstance(row, dict):
                continue
            name = str(row.get("outlet") or "").strip()
            if name:
                found.add(name)
    return sorted(found, key=str.lower)


def match_outlets(outlets, term: Any) -> list[str]:
    """Outlets containing ``term``, case-insensitively.

    A plain substring match, and deliberately no tie-breaking: when a term
    hits several shops the caller lists them and stops, because picking one
    is exactly the guess this export must not make. Note that a term equal
    to a whole name still counts as ambiguous when a longer name contains
    it ("bistro" vs "One Bistro") — narrow the term instead.
    """
    needle = str(term or "").strip().lower()
    if not needle:
        return []
    return [name for name in outlets if needle in name.lower()]


# --- items jsonb -------------------------------------------------------------

def _coalesce(entry: dict, *keys: str):
    """First key whose value is not null — SQL ``coalesce`` over ``->>``.

    Presence, not truthiness: an empty string stored under ``item`` wins
    over a name under ``name``, exactly as ``coalesce(it->>'item', ...)``
    would.
    """
    for key in keys:
        value = entry.get(key)
        if value is not None:
            return value
    return None


def to_number(value: Any) -> float | None:
    """A qty/price cell as a float, or ``None`` when it isn't usable.

    Real JSON numbers (what the OCR writes today) are taken as they are.
    Strings are cast only when they match ``^[0-9]+(\\.[0-9]+)?$`` after
    trimming, so "2 pcs", "RM19.80", "1,5" and "-3" all come out blank
    instead of being half-parsed into a wrong number. ``bool`` is rejected
    up front because it is an ``int`` subclass.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _NUMERIC_RE.fullmatch(text):
        return None
    try:
        return float(text)
    except ValueError:  # pragma: no cover - regex already guarantees this
        return None


def receipt_lines(items: Any) -> list[tuple[str, float | None, float | None]]:
    """``(name, qty, price)`` per entry of one receipt's ``items`` jsonb.

    A non-list ``items`` (null, a bare string, a dict) yields ``[]``. A
    string entry — glm-4.6v-flash still emits those on terse receipts —
    keeps its text as the name with no numbers, so a line is never dropped
    silently. The embedded "Ayam x30 RM19.80" rescue that
    ``items_utils.normalize_items`` does at ingest is NOT applied: this
    export shows what is stored, not what could be inferred from it.
    """
    if not isinstance(items, list):
        return []
    lines: list[tuple[str, float | None, float | None]] = []
    for entry in items:
        if isinstance(entry, dict):
            raw_name = _coalesce(entry, "item", "name")
            name = "" if raw_name is None else str(raw_name).strip()
            qty = to_number(_coalesce(entry, "qty", "quantity"))
            price = to_number(entry.get("price"))
        elif isinstance(entry, str):
            name, qty, price = entry.strip(), None, None
        else:
            continue
        lines.append((name, qty, price))
    return lines


def line_sum(lines) -> tuple[float | None, int]:
    """``(sum of qty x price, usable line count)`` for one receipt.

    ``price`` is the UNIT price throughout this codebase (the convention
    ``analytics.compute_line`` and ``price_aggregation`` share), so a line
    is worth qty x price. Lines missing either number contribute nothing;
    the sum is ``None`` when no line had both.
    """
    total = 0.0
    usable = 0
    for _name, qty, price in lines:
        if qty is None or price is None:
            continue
        total += qty * price
        usable += 1
    return (total if usable else None), usable


def reconciliation_ratio(summed: float | None, total: float | None) -> float | None:
    """How much of the bill its line items account for, or ``None``.

    ``None`` — the "unusable line data" case — covers both a receipt whose
    lines carry no usable numbers and one whose own total is missing or
    zero, since neither can be checked against the other. Returned
    unrounded; the CSV rounds it to 2dp for display while the +/-5% count
    is taken on the exact value, so a bill 5.4% out is never reported as
    reconciled just because it prints as 1.05.
    """
    if summed is None or total is None:
        return None
    try:
        if float(total) == 0.0:
            return None
        return float(summed) / float(total)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def reconciles(ratio: float | None) -> bool:
    """True when the line items land within ``RECONCILE_TOLERANCE`` of the
    bill total. ``None`` (unusable) is not a reconciliation.

    The bound is inclusive, and the epsilon is what makes it actually so:
    ``1.0 + 0.05`` is ``1.05000000000000004`` in binary, so a bill exactly
    5% over would otherwise be reported as failing to reconcile.
    """
    if ratio is None:
        return False
    return abs(ratio - 1.0) <= RECONCILE_TOLERANCE + _TOLERANCE_EPSILON


# --- CSV cells ---------------------------------------------------------------

def _date_cell(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _money_cell(value: float | None) -> str:
    """Money to a fixed 2dp, blank for a missing value — so a spreadsheet
    sums the column instead of choking on "None"."""
    return "" if value is None else f"{value:.2f}"


def _qty_cell(value: float | None) -> str:
    """Quantities without trailing zeros (30, not 30.0000) — they are counts
    and weights, not money, and 7.2 kg must survive the round trip."""
    if value is None:
        return ""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _ratio_cell(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _sort_key(bill) -> tuple:
    """Bills in date order, dateless rows last.

    No id in the key on purpose: the rows arrive ordered by ``id`` and
    ``sorted`` is stable, so same-day bills keep the order Postgres gave
    them. Deriving a tiebreak from the id instead would have to guess its
    type — ``str(10) < str(2)`` would put a later bill first.
    """
    when = bill[0]
    return (when == "", when)


def _slug(value: str) -> str:
    """``"Klang B.Emas"`` -> ``"klang_b_emas"`` — a filename, not a name."""
    slug = _FILENAME_SAFE_RE.sub("_", str(value or "").lower()).strip("_")
    return slug or "outlet"


def _csv_bytes(header, rows) -> bytes:
    """One CSV as bytes, BOM included so Excel opens Malay item names in
    UTF-8 instead of mojibake."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# --- the export --------------------------------------------------------------

def _iter_month_receipts(client, outlet: str, start_iso: str, end_iso: str) -> Iterator[list]:
    """Pages of one outlet's receipts for the month, oldest id first.

    Half-open on ``receipt_date``. Ordered by ``id`` — the deterministic
    key ``iter_pages`` needs — rather than by date, which has ties and
    would let pages shear; the bills CSV is sorted by date afterwards.
    """
    def _build():
        return (
            client.table(RECEIPTS_TABLE)
            .select("id, receipt_date, merchant, total, items")
            .eq("outlet", outlet)
            .gte("receipt_date", start_iso)
            .lt("receipt_date", end_iso)
            .order("id", desc=False)
        )

    return iter_pages(_build, page_size=PAGE_SIZE)


def build_export(client, outlet: str, year: int, month: int) -> dict:
    """Three CSVs plus the headline numbers for one outlet-month.

    Returns ``{"outlet", "month", "files", "stats"}`` where ``files`` is a
    list of ``(filename, bytes)`` — empty when the month holds no receipts
    at all, so the caller can say so instead of posting three empty files.

    Receipts are consumed a page at a time: item rows are written straight
    into the CSV buffer as they arrive and supplier totals accumulate in a
    dict keyed by merchant, so the month is never materialised as rows.
    (The bills CSV keeps one small tuple per receipt because it has to come
    out in date order and the read is by id.)
    """
    start_iso, end_iso = month_window(year, month)
    label = month_label(year, month)

    suppliers: dict[str, dict] = {}
    bills: list[tuple] = []
    items_buf = io.StringIO(newline="")
    items_writer = csv.writer(items_buf, lineterminator="\n")
    items_writer.writerow(ITEMS_HEADER)

    bill_count = 0
    total_spend = 0.0
    reconciled_count = 0
    unusable_count = 0

    for page in _iter_month_receipts(client, outlet, start_iso, end_iso):
        for row in page:
            if not isinstance(row, dict):
                continue
            receipt_id = row.get("id")
            when = _date_cell(row.get("receipt_date"))
            merchant = str(row.get("merchant") or "").strip() or _UNKNOWN_MERCHANT
            total = normalize_total(row.get("total"))

            bill_count += 1
            if total is not None:
                total_spend += total

            agg = suppliers.setdefault(merchant, {"bills": 0, "amount": 0.0})
            agg["bills"] += 1
            agg["amount"] += total or 0.0

            bills.append((when, merchant, total, receipt_id))

            lines = receipt_lines(row.get("items"))
            summed, _usable = line_sum(lines)
            ratio = reconciliation_ratio(summed, total)
            if ratio is None:
                unusable_count += 1
            elif reconciles(ratio):
                reconciled_count += 1

            ratio_cell = _ratio_cell(ratio)
            bill_total_cell = _money_cell(total)
            for name, qty, price in lines:
                items_writer.writerow([
                    when, merchant, name, _qty_cell(qty), _money_cell(price),
                    bill_total_cell, receipt_id, ratio_cell,
                ])

    logger.info(
        "invoices export: %s %s — %d bill(s), %d supplier(s), %d unusable",
        outlet, label, bill_count, len(suppliers), unusable_count,
    )
    stats = {
        "bill_count": bill_count,
        "total_spend": round(total_spend, 2),
        "reconciled_count": reconciled_count,
        "unusable_count": unusable_count,
    }
    if bill_count == 0:
        return {"outlet": outlet, "month": label, "files": [], "stats": stats}

    prefix = f"{_slug(outlet)}_{label}"
    supplier_rows = [
        (name, agg["bills"], _money_cell(agg["amount"]))
        for name, agg in sorted(
            suppliers.items(), key=lambda kv: (-kv[1]["amount"], kv[0].lower())
        )
    ]
    bill_rows = [
        (when, merchant, _money_cell(total), receipt_id)
        for when, merchant, total, receipt_id in sorted(bills, key=_sort_key)
    ]

    files = [
        (f"{prefix}_suppliers.csv", _csv_bytes(SUPPLIERS_HEADER, supplier_rows)),
        (f"{prefix}_bills.csv", _csv_bytes(BILLS_HEADER, bill_rows)),
        (f"{prefix}_items.csv", items_buf.getvalue().encode("utf-8-sig")),
    ]
    return {"outlet": outlet, "month": label, "files": files, "stats": stats}


# --- replies -----------------------------------------------------------------

def format_outlet_choice(term: Any, matches, outlets) -> str:
    """The reply when the outlet argument did NOT resolve to exactly one shop.

    Zero matches lists every outlet on record (there is nothing else useful
    to show); several lists the ones that matched. Either way the command
    stops — the user retries with a narrower term.
    """
    typed = str(term or "").strip()
    if matches:
        lines = [f'"{typed}" matches {len(matches)} outlets — narrow it down:']
        lines += [f"• {name}" for name in matches]
        return "\n".join(lines)
    lines = [f'No outlet matches "{typed}".']
    if outlets:
        lines.append("Outlets on record:")
        lines += [f"• {name}" for name in outlets]
    return "\n".join(lines)


def format_empty(outlet: str, year: int, month: int) -> str:
    return f"No receipts for {outlet} in {month_label(year, month)}."


def format_summary(outlet: str, year: int, month: int, stats: dict) -> str:
    """The message that follows the files — what the numbers are, and how
    much of the item detail is trustworthy."""
    bills = stats.get("bill_count") or 0
    spend = stats.get("total_spend") or 0.0
    reconciled = stats.get("reconciled_count") or 0
    unusable = stats.get("unusable_count") or 0
    pct = int(RECONCILE_TOLERANCE * 100)
    return "\n".join([
        f"📄 {outlet} • {month_label(year, month)}",
        f"Bills: {bills}",
        f"Total spend: RM{spend:,.2f}",
        f"Line items reconcile to bill total (±{pct}%): {reconciled} of {bills}",
        f"No usable line data: {unusable} bill(s)",
    ])
