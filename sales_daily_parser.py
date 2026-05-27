"""POS daily-summary (D-file) parser (PR #60).

The POS sends two email types per outlet per day:
  * ``S-`` shift-close detail (parsed by sales_parser.py -> sales_daily) — at
    ~19:00 / ~00:00.
  * ``D-`` daily summary (THIS module -> sales_daily_summary) — at ~07:00.

D-files are pre-aggregated (1 row per outlet per day) and carry data the S-files
don't: customer counts, average spend, takeaway/dine-in split, consolidated
vendor payouts, a deleted-item audit trail with staff names, TOP-N rankings, and
itemwise sales by category.

Layout (verified across the 7 real 26-May-2026 fixtures; tolerant of the 3
not-yet-uploaded outlets):

    D-OUTLET   ON DD/Month/YYYY HH:MM:SS
               <BUSINESS NAME>
               <ADDRESS>
           (TOTAL SHIFTS :N)            N varies (SEK6=3, others=2)
    DAY SALES / TAX 6% / ROUNDED / INACTIVE/CR SALE / NET SALES /
    CASH PAYMENT / CASH IN DRAW / DISCOUNT          (daily aggregate)
    ======= NO. OF CUSTOMERS ===
    TODAYS CUSTOMERS / AVERAGE SPENT / TAKE AWAY / DINE IN / DELETED ITEMS
    SHIFT :N (SHIFTNO) ON ...           (repeated per shift, same fields)
    PAYOUT DETAILS                      (SHIFTNO DESCRIPTION AMOUNT; "PAY TO X")
    ==== DELETED ITEM BY ADMIN ====     (item line + staff/time/reason line)
    TOP 30 FOOD ITEM SALES / TOP 30 DRINKS SALES / TOP 20 ITEM SALES
    ITEMWISE SALES                      (category headers + items)

Identity (outlet) comes from the email SUBJECT, not the header — see
sales_email_fetcher.detect_email_type.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sales_parser import (
    _columns,
    _find_banner,
    _is_separator,
    _kv_split,
    _norm_label,
    normalize_content,
    parse_datetime,
    parse_int,
)


def _money(tok):
    """Parse a money token, accepting leading-dot decimals (".50", ".00") the
    POS prints. (Self-contained so this PR doesn't touch the S-file parser.)"""
    if tok is None:
        return None
    s = re.sub(r"(?i)rm", "", str(tok)).replace(",", "").replace(" ", "")
    m = re.search(r"-?(?:\d+(?:\.\d+)?|\.\d+)", s)
    return float(m.group(0)) if m else None


def _money_tok(tok) -> bool:
    t = tok.strip()
    return bool(re.fullmatch(r"-?[\d,]*\.\d{2}", t)) and any(c.isdigit() for c in t)


def _int_tok(tok) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+", tok.strip()))

# Daily aggregate label (normalised) -> (field, parser).
_DAILY_LABELS = {
    "DAY SALES": ("day_sales", _money),
    "NET SALES": ("net_sales", _money),
    "CASH PAYMENT": ("cash_payment", _money),
    "CASH IN DRAW": ("cash_in_draw", _money),
    "DISCOUNT": ("discount", _money),
    "ROUNDED": ("rounded", _money),
    "TAX": ("tax", _money),
    "TODAYS CUSTOMERS": ("customers", parse_int),
    "AVERAGE SPENT": ("average_spent", _money),
    "TAKE AWAY": ("take_away", _money),
    "DINE IN": ("dine_in", _money),
    "DELETED ITEMS": ("deleted_items_total", _money),
}

_SHIFT_HEADER_RE = re.compile(r"^\s*SHIFT\s*:\s*(\d+)\s*\((\d+)\)\s*ON\s+(.+?)\s*$", re.IGNORECASE)
_D_HEADER_RE = re.compile(r"^\s*(D-[\w\s]+?)\s+ON\s+(.+?)\s*$", re.IGNORECASE)
_TOTAL_SHIFTS_RE = re.compile(r"TOTAL\s+SHIFTS\s*:\s*(\d+)", re.IGNORECASE)
# An item/ranking row: NAME QTY AMOUNT. Anchored on the trailing amount so it
# splits correctly even when a long name leaves only ONE space before the qty
# (column-split on 2+ spaces fails there) and when the name itself ends in a
# number (e.g. "Kacang 2.00  9  18.00").
_ITEM_ROW_RE = re.compile(r"^(.*?\S)\s+(\d+)\s+(-?[\d,]*\.\d{2})\s*$")
# A deleted-item line: NAME QTY RATE TOTAL (rate/total may be single-space split).
_DELETED_ITEM_RE = re.compile(r"^(.*?\S)\s+(\d+)\s+(-?[\d,]*\.\d{2})\s+(-?[\d,]*\.\d{2})\s*$")
# The staff/time/reason line that follows a deleted item.
_DELETED_STAFF_RE = re.compile(r"^\s*(\S.*?)\s+(\d{1,2}:\d{2}:\d{2})\s*(.*)$")

# Trailing-section banners (used to bound the per-shift region).
_TRAILING_BANNERS = (r"PAYOUT DETAILS", r"DELETED ITEM BY ADMIN", r"TOP \d+ ", r"ITEMWISE")


def _shift_indices(lines):
    return [i for i, l in enumerate(lines) if _SHIFT_HEADER_RE.match(l)]


def _first_trailing_idx(lines, after=0):
    idxs = []
    for pat in _TRAILING_BANNERS:
        for i, l in enumerate(lines):
            if i >= after and re.search(pat, l, re.IGNORECASE):
                idxs.append(i)
                break
    return min(idxs) if idxs else len(lines)


# --- header + label/value blocks --------------------------------------------

# D-files are emitted at the end of the business day. A normal day shift closes
# ~19:00 and prints same-day; the overnight shift closes ~00:00-07:00 and prints
# AFTER midnight, so its print date is one day AHEAD of the business day it
# summarises. Split on 17:00 (5pm): >=17:00 -> header date; earlier -> day before.
_BUSINESS_DATE_HOUR_CUTOFF = 17


def business_date_for_printed(printed_at):
    """The business day a D-file covers, from its print timestamp (PR #61/#62).

    >=17:00 (evening close) -> the header date; <17:00 (post-midnight overnight
    close) -> the previous day. Returns ``None`` if ``printed_at`` is missing.

    ``printed_at`` is the POS wall-clock time parsed straight from the header and
    is ALREADY Asia/Kuala_Lumpur local (not UTC). The hour is read directly — we
    must NOT apply any timezone conversion, which would double-shift the time and
    mis-date the post-midnight overnight files. As a safety net, an
    accidentally-tz-aware value has its tzinfo dropped (no conversion) so the
    wall-clock hour is preserved.
    """
    if printed_at is None:
        return None
    if printed_at.tzinfo is not None:
        printed_at = printed_at.replace(tzinfo=None)
    day = printed_at.date()
    if printed_at.hour >= _BUSINESS_DATE_HOUR_CUTOFF:
        return day
    return day - timedelta(days=1)


def _parse_header(lines):
    outlet_code = printed_at = business_date = None
    business_name = address = None
    total_shifts = None
    for i, l in enumerate(lines[:12]):
        m = _D_HEADER_RE.match(l)
        if m and outlet_code is None:
            outlet_code = re.sub(r"\s+", " ", m.group(1)).strip().upper()
            printed_at = parse_datetime(m.group(2))
            business_date = business_date_for_printed(printed_at)
            # next two non-empty, non-(TOTAL SHIFTS) lines are name + address
            extras = [
                x.strip() for x in lines[i + 1:i + 5]
                if x.strip() and "TOTAL SHIFTS" not in x.upper()
            ]
            business_name = extras[0] if extras else None
            address = extras[1] if len(extras) > 1 else None
        ms = _TOTAL_SHIFTS_RE.search(l)
        if ms:
            total_shifts = int(ms.group(1))
    return {
        "outlet_code": outlet_code,
        "printed_at": printed_at,
        "business_date": business_date,
        "business_name": business_name,
        "address": address,
        "total_shifts": total_shifts,
    }


def _parse_label_block(lines):
    """Map the daily/shift LABEL:value lines to fields (first occurrence wins)."""
    out = {}
    for line in lines:
        kv = _kv_split(line)
        if not kv:
            continue
        norm = _norm_label(kv[0])
        if "INACTIVE" in norm and "inactive_cr_sale" not in out:
            out["inactive_cr_sale"] = _money(kv[1])
            continue
        # Shift blocks use "TODAY SALES" where the daily block uses "DAY SALES";
        # fold the shift label into day_sales so both populate one field.
        if norm == "TODAY SALES" and "day_sales" not in out:
            out["day_sales"] = _money(kv[1])
            continue
        spec = _DAILY_LABELS.get(norm)
        if spec and spec[0] not in out:
            field, fn = spec
            out[field] = fn(kv[1])
    return out


def _parse_shifts(lines):
    """One dict per ``SHIFT :N (id) ON ...`` block (supports 2-3 shifts)."""
    idxs = _shift_indices(lines)
    if not idxs:
        return []
    trailing = _first_trailing_idx(lines, after=idxs[-1])
    bounds = idxs + [trailing]
    shifts = []
    for k, start in enumerate(idxs):
        end = bounds[k + 1]
        m = _SHIFT_HEADER_RE.match(lines[start])
        block = _parse_label_block(lines[start + 1:end])
        shifts.append({
            "shift_index": int(m.group(1)),
            "shift_id": m.group(2),
            "printed_at": parse_datetime(m.group(3)),
            "sales": block.get("day_sales"),
            "net_sales": block.get("net_sales"),
            "cash_payment": block.get("cash_payment"),
            "cash_in_draw": block.get("cash_in_draw"),
            "customers": block.get("customers"),
            "average_spent": block.get("average_spent"),
            "take_away": block.get("take_away"),
            "dine_in": block.get("dine_in"),
            "deleted_items_total": block.get("deleted_items_total"),
        })
    return shifts


# --- payouts / deleted / rankings / itemwise --------------------------------

def _parse_payouts(lines):
    idx = _find_banner(lines, r"PAYOUT DETAILS")
    if idx is None:
        return []
    out = []
    for l in lines[idx + 1:]:
        if re.search(r"TOTAL\s+PAYOUTS", l, re.IGNORECASE):
            break
        cols = _columns(l)
        if len(cols) >= 3 and _int_tok(cols[0]) and _money_tok(cols[-1]):
            desc = " ".join(cols[1:-1]).strip()
            vendor = re.sub(r"(?i)^PAY\s+TO\s+", "", desc).strip()
            out.append({
                "shiftno": cols[0],
                "description": desc,
                "vendor_name": vendor,
                "amount": _money(cols[-1]),
            })
    return out


def _parse_deleted_audit(lines):
    idx = _find_banner(lines, r"DELETED ITEM BY ADMIN")
    if idx is None:
        return []
    out = []
    i = idx + 1
    n = len(lines)
    while i < n:
        line = lines[i]
        if re.match(r"\s*END\b", line):
            break
        m = _DELETED_ITEM_RE.match(line)
        if m:
            entry = {
                "item_name": m.group(1).strip(),
                "qty": parse_int(m.group(2)),
                "rate": _money(m.group(3)),
                "amount": _money(m.group(4)),
                "staff": None,
                "time": None,
                "reason": None,
            }
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                sm = _DELETED_STAFF_RE.match(lines[j])
                if sm and not _DELETED_ITEM_RE.match(lines[j]):
                    entry["staff"] = sm.group(1).strip()
                    entry["time"] = sm.group(2)
                    entry["reason"] = sm.group(3).strip() or None
                    i = j
            out.append(entry)
        i += 1
    return out


def _skip_section_header(lines, banner_idx):
    """From a banner, skip the rulers / blank / ITEMNAME header and return the
    index of the first data line."""
    i = banner_idx + 1
    n = len(lines)
    while i < n and (
        _is_separator(lines[i])
        or not lines[i].strip()
        or re.search(r"ITEMNAME", lines[i], re.IGNORECASE)
    ):
        i += 1
    return i


def _parse_ranking(lines, banner_re):
    """A TOP-N block: ITEMNAME QTY AMOUNT rows, ending at the next ruler."""
    idx = _find_banner(lines, banner_re)
    if idx is None:
        return []
    rows = []
    i = _skip_section_header(lines, idx)
    while i < len(lines) and not _is_separator(lines[i]):
        m = _ITEM_ROW_RE.match(lines[i])
        if m:
            rows.append({
                "name": m.group(1).strip(),
                "qty": parse_int(m.group(2)),
                "amount": _money(m.group(3)),
            })
        i += 1
    return rows


def _parse_itemwise(lines):
    """ITEMWISE SALES grouped by category -> {category: [{name, qty, amount}]}.

    Bounded to the section (ends at the next ruler, so the following TABLE WISE /
    MACHINE SALES blocks don't bleed in). Bare non-item lines are category heads.
    """
    idx = _find_banner(lines, r"ITEMWISE\s+SALES")
    if idx is None:
        return {}
    result: dict = {}
    current = None
    i = _skip_section_header(lines, idx)
    while i < len(lines) and not _is_separator(lines[i]):
        line = lines[i]
        if line.strip():
            m = _ITEM_ROW_RE.match(line)
            if m:
                if current is not None:
                    result[current].append({
                        "name": m.group(1).strip(),
                        "qty": parse_int(m.group(2)),
                        "amount": _money(m.group(3)),
                    })
            else:
                current = line.strip()
                result.setdefault(current, [])
        i += 1
    return {k: v for k, v in result.items() if v}


# --- top-level ---------------------------------------------------------------

def parse_daily_summary(content: str) -> dict:
    """Parse one D-file (daily summary) into a structured dict (see module docs)."""
    content = normalize_content(content or "")
    lines = content.split("\n")

    header = _parse_header(lines)

    first_shift = next((i for i, l in enumerate(lines) if _SHIFT_HEADER_RE.match(l)), None)
    daily_end = first_shift if first_shift is not None else _first_trailing_idx(lines)
    daily_aggregate = _parse_label_block(lines[:daily_end])

    return {
        "header": header,
        "daily_aggregate": daily_aggregate,
        "shifts": _parse_shifts(lines),
        "payouts": _parse_payouts(lines),
        "deleted_items": _parse_deleted_audit(lines),
        "top_30_food": _parse_ranking(lines, r"TOP\s+30\s+FOOD"),
        "top_30_drinks": _parse_ranking(lines, r"TOP\s+30\s+DRINKS"),
        "top_20_combined": _parse_ranking(lines, r"TOP\s+20\s+ITEM"),
        "itemwise_sales": _parse_itemwise(lines),
    }
