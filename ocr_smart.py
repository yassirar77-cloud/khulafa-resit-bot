"""Self-checking bill reading.

The first OCR pass reads the photo and a second pass audits it. Until now the
bot believed whatever the audit said and stopped there, even when the bill's
own numbers still did not add up. This module makes the reading check itself
against the one thing on a bill that cannot lie: its arithmetic
(qty x unit price = line total, and the lines add up to the bill total).

Four steps, each of which only ever changes a value when the arithmetic or
the calendar backs the change:

* ``pre_check`` — fixes that need no model call: a dropped decimal point on
  one line (qty 2 x 1150 against a printed line total of 23.00), a missing
  qty or price that the printed line total gives away, a bill total that is
  exactly 10x / 100x off its lines, and a date that cannot be right (future,
  or months old) when swapping day and month or the year lands it within the
  last two weeks.
* ``arbitrate_corrections`` — the audit's corrections are applied only when
  they do not break a bill that already added up, and the combination that
  makes the bill add up wins. A "corrected" item list that drops lines
  without fixing the sum is refused.
* ``build_reread_prompt`` / ``accept_reread`` — when the bill still does not
  add up, the model is shown exactly where (which lines, by how much) and
  asked to read those digits again. The answer is used only if it makes the
  arithmetic close AND agrees with the first reading on one side (the same
  bill total, or the same line sum), so two readings and the arithmetic all
  have to line up before a number changes. Tax / discount / rounding lines
  are accepted as the explanation of a gap only when the first reading's
  own transcription mentions them.
* ``self_check`` — runs the above in order around an injected ``reread``
  coroutine and reports what it did.

Pure apart from the injected coroutine: no Telegram, no Supabase, no network.
``OCR_SELF_CHECK=off`` switches the whole module off; ``OCR_REREAD=off`` keeps
the free fixes but never makes the extra model call.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from date_utils import normalize_date
from items_utils import normalize_items, resolve_weighed_lines
from money_utils import normalize_total
from ocr_quality import (
    _DATE_ANY_RE,
    _is_foc_item,
    _parse_date_candidate,
    correct_total_with_items,
)

logger = logging.getLogger(__name__)

_MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

# Bill level: the lines add up to the total within 1% (the same tolerance as
# ocr_quality.items_sum_matches_total) or five sen, whichever is larger.
BILL_TOLERANCE = 0.01
BILL_TOLERANCE_ABS = 0.05
# Line level: qty x price against the printed line total. Weighed lines are
# rounded to the sen on the bill, so allow a few sen or 0.5%.
LINE_TOLERANCE = 0.005
LINE_TOLERANCE_ABS = 0.06

# A bill is photographed the day it arrives, or a few days after. A date
# outside this window is suspect; a candidate inside it is believable.
DATE_RECENT_DAYS = 14
DATE_FUTURE_DAYS = 1
# A date older than this is suspect enough to look for a day/month swap.
DATE_STALE_DAYS = 45

# Extra lines that legitimately sit between the item sum and the bill total.
# An adjustment from the re-read is believed only when one of its family's
# words is in the FIRST reading's transcription.
_ADJUSTMENT_FAMILIES = (
    ("sst", "gst", "tax", "cukai", "service charge", "svc"),
    ("disc", "diskaun", "rebate", "less", "tolak"),
    ("round", "bundar", "rnd", "adj"),
    ("deposit", "cagaran", "botol", "bottle", "tong", "crate", "bakul"),
    ("deliver", "transport", "penghantaran", "lori", "freight"),
    ("balance", "baki", "tunggakan", "previous", "b/f"),
)
ROUNDING_MAX = 0.05
# An "adjustment" bigger than this share of the bill is not an adjustment.
ADJUSTMENT_MAX_SHARE = 0.30


def enabled() -> bool:
    return os.environ.get("OCR_SELF_CHECK", "on").strip().lower() not in (
        "off", "0", "false", "no")


def reread_enabled() -> bool:
    return enabled() and os.environ.get("OCR_REREAD", "on").strip().lower() not in (
        "off", "0", "false", "no")


def today_my() -> date:
    return datetime.now(_MY_TZ).date()


# --- arithmetic ---------------------------------------------------------------

def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _money(value: Any) -> float | None:
    """A bill total as a float, whatever shape it arrived in."""
    number = _num(value)
    if number is not None:
        return number
    if isinstance(value, str):
        return _num(normalize_total(value))
    return None


def item_sum(items: Any) -> float | None:
    """Sum of qty x price over the lines that have a price (qty defaults to 1,
    free-of-charge lines count as nothing). ``None`` when no line is usable."""
    if not isinstance(items, list):
        return None
    total = 0.0
    seen = False
    for it in items:
        if not isinstance(it, dict) or _is_foc_item(it):
            continue
        price = _num(it.get("price"))
        if price is None:
            continue
        qty = _num(it.get("qty"))
        total += (qty if qty and qty > 0 else 1.0) * price
        seen = True
    return total if seen and total > 0 else None


def _close(a: float, b: float, tol: float = BILL_TOLERANCE,
           tol_abs: float = BILL_TOLERANCE_ABS) -> bool:
    return abs(a - b) <= max(tol_abs, tol * abs(b))


def adjustments_sum(parsed: dict) -> float:
    total = 0.0
    for adj in parsed.get("adjustments") or []:
        amount = _num(adj.get("amount")) if isinstance(adj, dict) else None
        if amount is not None:
            total += amount
    return total


def math_total(parsed: dict) -> float | None:
    """The bill total the item lines should add up to: the printed total less
    any accepted tax / discount / rounding lines."""
    total = _money(parsed.get("total"))
    if total is None:
        return None
    return round(total - adjustments_sum(parsed), 2)


def state(items: Any, total: Any, adjustments: float = 0.0) -> str:
    """``match`` (the lines add up to the total), ``conflict`` (they do not)
    or ``unknown`` (nothing to check: no total, or no priced line)."""
    t = _money(total)
    s = item_sum(items)
    if t is None or t <= 0 or s is None:
        return "unknown"
    return "match" if _close(s + adjustments, t) else "conflict"


def parsed_state(parsed: dict) -> str:
    return state(parsed.get("items"), parsed.get("total"), adjustments_sum(parsed))


# --- line repairs (no model call) ---------------------------------------------

def _line_ok(qty: float, price: float, line_total: float) -> bool:
    return _close(qty * price, line_total, LINE_TOLERANCE, LINE_TOLERANCE_ABS)


def suspect_lines(items: Any) -> list[int]:
    """Indexes of lines whose qty x price disagrees with their printed line
    total."""
    out = []
    for idx, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        qty, price, lt = _num(it.get("qty")), _num(it.get("price")), _num(it.get("line_total"))
        if qty and price and lt and qty > 0 and price > 0 and lt > 0 and not _line_ok(qty, price, lt):
            out.append(idx)
    return out


def _clean_cents(value: float) -> bool:
    return abs(value * 100 - round(value * 100)) < 1e-6


def repair_lines(items: Any, total: Any = None) -> tuple[list[dict], list[str]]:
    """Fix lines the printed line total gives away. Returns
    ``(items, notes)``; the input is not mutated.

    * qty x price is exactly 10x / 100x above the printed line total: a
      decimal point was lost in the price or the qty — fixed only when just
      one of the two is a believable candidate;
    * price missing, qty and line total there: price = line total / qty
      (only when that comes out to whole sen);
    * qty missing, price and line total there: qty = line total / price.

    The repaired list is kept only if it does not turn a bill that added up
    into one that does not.
    """
    if not isinstance(items, list):
        return [], []
    rows = [dict(it) if isinstance(it, dict) else it for it in items]
    notes: list[str] = []
    for it in rows:
        if not isinstance(it, dict) or _is_foc_item(it):
            continue
        qty, price, lt = _num(it.get("qty")), _num(it.get("price")), _num(it.get("line_total"))
        if lt is None or lt <= 0:
            continue
        name = str(it.get("name") or "?")[:40]
        if qty and qty > 0 and price and price > 0:
            if _line_ok(qty, price, lt):
                continue
            # A lost decimal point makes a whole number 10x / 100x too big.
            # Either the price or the qty could be the one; fix the line only
            # when exactly one of them is a believable candidate.
            options = []
            for flip in (0.1, 0.01):
                if not _line_ok(qty * flip, price, lt):
                    continue
                new_price, new_qty = round(price * flip, 4), round(qty * flip, 4)
                if float(price).is_integer() and _clean_cents(new_price) and new_price >= 0.05:
                    options.append(("price", round(new_price, 2)))
                if float(qty).is_integer() and new_qty >= 0.1:
                    options.append(("qty", new_qty))
            if len(options) == 1:
                field, value = options[0]
                notes.append(f"{name}: {field} {it[field]:g}→{value:g} (line total {lt:g})")
                it[field] = value
        elif qty and qty > 0 and price is None:
            candidate = lt / qty
            if _clean_cents(round(candidate, 6)):
                it["price"] = round(candidate, 2)
                notes.append(f"{name}: price {it['price']:g} from line total {lt:g}")
        elif price and price > 0 and qty is None:
            it["qty"] = round(lt / price, 3)
            notes.append(f"{name}: qty {it['qty']:g} from line total {lt:g}")
    if not notes:
        return rows, []
    if state(items, total) == "match" and state(rows, total) != "match":
        return [dict(it) if isinstance(it, dict) else it for it in items], []
    return rows, notes


# --- date sanity ----------------------------------------------------------------

def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        iso = normalize_date(value)
        if iso:
            try:
                return date.fromisoformat(iso)
            except ValueError:
                return None
    return None


def _dates_in_text(raw_text: Any) -> set[date]:
    """Every date written in the transcription, read day-first as printed."""
    if not isinstance(raw_text, str):
        return set()
    found = set()
    for match in _DATE_ANY_RE.finditer(raw_text):
        parsed = _parse_date_candidate(match.group(1))
        if parsed is not None:
            found.add(parsed)
    return found


def fix_date(value: Any, today: date | None = None,
             raw_text: Any = None) -> tuple[str | None, str | None]:
    """``(iso_date, note)``. The date is kept unless it is suspect — in the
    future, or more than ``DATE_STALE_DAYS`` old — AND exactly one of "day
    and month swapped", "year off by one", or both lands within the last
    ``DATE_RECENT_DAYS`` days. A future date cannot be right, so that is
    enough; an old date can be a genuine late upload, so it is changed only
    when the transcription itself shows the corrected date. ``note`` is
    ``None`` when nothing changed."""
    today = today or today_my()
    current = _to_date(value)
    if current is None:
        return (value if isinstance(value, str) else None), None
    earliest = today - timedelta(days=DATE_RECENT_DAYS)
    latest = today + timedelta(days=DATE_FUTURE_DAYS)
    if today - timedelta(days=DATE_STALE_DAYS) <= current <= latest:
        return current.isoformat(), None
    candidates: set[date] = set()
    shapes = [(current.year, current.month, current.day)]
    if current.day <= 12 and current.day != current.month:
        shapes.append((current.year, current.day, current.month))
    for year, month, day in shapes:
        for y in (year, year - 1, year + 1):
            try:
                cand = date(y, month, day)
            except ValueError:
                continue
            if cand != current and earliest <= cand <= latest:
                candidates.add(cand)
    if len(candidates) != 1:
        return current.isoformat(), None
    fixed = candidates.pop()
    if current <= latest and fixed not in _dates_in_text(raw_text):
        return current.isoformat(), None
    return fixed.isoformat(), f"date {current.isoformat()}→{fixed.isoformat()}"


# --- step 1: fixes that need no model call -------------------------------------

def pre_check(parsed: dict, today: date | None = None) -> list[str]:
    """Mutate ``parsed`` with the free fixes; return what changed."""
    notes: list[str] = []
    items, line_notes = repair_lines(parsed.get("items"), parsed.get("total"))
    if line_notes:
        parsed["items"] = items
        notes.extend(line_notes)

    # Per-kg lines read as the bird / piece count: take the weight, but only
    # when that is what makes the bill add up.
    if parsed_state(parsed) == "conflict":
        weighed = resolve_weighed_lines(
            parsed.get("items"), parsed.get("raw_text"), _money(parsed.get("total")))
        if state(weighed, parsed.get("total"), adjustments_sum(parsed)) == "match":
            parsed["items"] = weighed
            notes.append("per-kg lines: qty set to the weight")

    total = _money(parsed.get("total"))
    if total is not None and parsed_state(parsed) == "conflict":
        fixed, changed = correct_total_with_items(
            total, parsed.get("items"), parsed.get("raw_text"))
        if changed:
            parsed["total"] = fixed
            notes.append(f"total {total:g}→{fixed:g} (decimal point)")

    current = parsed.get("receipt_date") or parsed.get("date")
    fixed_date, date_note = fix_date(current, today, parsed.get("raw_text"))
    if date_note:
        parsed["receipt_date"] = fixed_date
        parsed["date"] = fixed_date
        notes.append(date_note)
    return notes


# --- step 2: the audit's corrections, checked against the arithmetic ------------

def arbitrate_corrections(parsed: dict, corrections: Any) -> tuple[dict, list[str]]:
    """Return ``(corrections_to_apply, refused)``.

    Merchant / date / currency corrections pass through. The total and items
    corrections are judged by what they do to the bill's arithmetic:

    * the bill added up before and would not after -> both refused;
    * the bill did not add up: the smallest change that makes it add up wins
      (total only, items only, then both);
    * nothing makes it add up: applied as before, except an item list that
      loses lines is refused (the audit often returns only the lines it
      changed, which would wipe the rest of the bill).
    """
    if not isinstance(corrections, dict):
        return {}, []
    out = dict(corrections)
    refused: list[str] = []
    has_total = "total" in out and normalize_total(out.get("total")) is not None
    new_items = normalize_items(out.get("items")) if "items" in out else None
    has_items = bool(new_items)
    if "items" in out and not has_items:
        out.pop("items")
        refused.append("items (empty)")
    if "total" in out and not has_total:
        out.pop("total")
    if not has_total and not has_items:
        return out, refused

    old_items, old_total = parsed.get("items"), parsed.get("total")
    adj = adjustments_sum(parsed)
    cand_total = normalize_total(out.get("total")) if has_total else old_total
    cand_items = new_items if has_items else old_items

    before = state(old_items, old_total, adj)
    full = state(cand_items, cand_total, adj)

    if before == "match" and full != "match":
        if has_total:
            out.pop("total")
            refused.append("total (bill already adds up)")
        if has_items:
            out.pop("items")
            refused.append("items (bill already adds up)")
        return out, refused

    if before != "match" and has_total and has_items and full != "match":
        if state(old_items, cand_total, adj) == "match":
            out.pop("items")
            refused.append("items (total alone makes the bill add up)")
            return out, refused
        if state(cand_items, old_total, adj) == "match":
            out.pop("total")
            refused.append("total (items alone make the bill add up)")
            return out, refused

    if has_items and "items" in out and state(cand_items, cand_total, adj) != "match":
        old_count = len(old_items) if isinstance(old_items, list) else 0
        if len(new_items) < old_count:
            out.pop("items")
            refused.append(f"items ({len(new_items)} lines would replace {old_count})")
    return out, refused


# --- step 3: the targeted re-read -----------------------------------------------

def needs_reread(parsed: dict) -> bool:
    return parsed_state(parsed) == "conflict"


def _fmt_line(idx: int, it: dict) -> str:
    def show(value):
        number = _num(value)
        return "?" if number is None else f"{number:g}"
    line = f"{idx + 1}. {it.get('name') or '?'} | qty {show(it.get('qty'))} | unit price {show(it.get('price'))}"
    if _num(it.get("line_total")) is not None:
        line += f" | line total {show(it.get('line_total'))}"
    return line


def build_reread_prompt(parsed: dict) -> str:
    items = [it for it in (parsed.get("items") or []) if isinstance(it, dict)]
    total = _money(parsed.get("total"))
    lines_sum = item_sum(items) or 0.0
    suspects = suspect_lines(items)
    lines = "\n".join(_fmt_line(i, it) for i, it in enumerate(items)) or "(none)"
    where = ""
    if suspects:
        where = ("Lines where qty x unit price does not equal the line total: "
                 + ", ".join(str(i + 1) for i in suspects) + ".\n")
    return (
        "You are re-checking a Malaysian supplier bill. A first reading of this "
        "photo gave numbers that do not add up:\n\n"
        f"{lines}\n\n"
        f"These lines add up to {lines_sum:.2f} but the bill total was read as "
        f"{total:.2f} (difference {total - lines_sum:+.2f}).\n"
        f"{where}\n"
        "Look at the photo again and read every number digit by digit. Common "
        "mistakes: 1/7, 3/8, 5/6, 0/6 confused in handwriting; a decimal point "
        "missed; a line skipped; the bird or piece count used instead of the "
        "weight in kg on per-kg lines; a tax, discount, rounding, deposit, "
        "delivery or previous-balance line between the items and the total.\n\n"
        "Respond ONLY with JSON:\n"
        "{\n"
        "  \"total\": <final amount payable as printed>,\n"
        "  \"items\": [{\"name\": \"...\", \"qty\": <number>, \"price\": <unit price>, "
        "\"total\": <line total as printed or null>}],\n"
        "  \"adjustments\": [{\"label\": \"<as printed, e.g. SST 6%>\", "
        "\"amount\": <signed number: positive adds to the bill, negative reduces it>}],\n"
        "  \"unreadable\": true | false\n"
        "}\n"
        "List EVERY item line on the bill, not only the ones you changed. Do "
        "not invent an adjustment to make the numbers fit: include one only if "
        "it is printed or written on the bill. If a number truly cannot be "
        "read, set \"unreadable\" to true and give your best reading. No "
        "markdown, JSON only."
    )


def _adjustment_grounded(label: Any, amount: float, raw_text: Any) -> bool:
    if abs(amount) <= ROUNDING_MAX:
        return True
    if not isinstance(label, str) or not isinstance(raw_text, str):
        return False
    low_label, low_raw = label.lower(), raw_text.lower()
    for family in _ADJUSTMENT_FAMILIES:
        if any(word in low_label for word in family) and any(word in low_raw for word in family):
            return True
    return False


def _clean_adjustments(raw: Any, raw_text: Any, total: float) -> list[dict] | None:
    """The re-read's adjustments, or ``None`` when one of them is not backed
    by the first transcription (the whole re-read is then not trusted)."""
    out = []
    for adj in raw if isinstance(raw, list) else []:
        if not isinstance(adj, dict):
            continue
        amount = _money(adj.get("amount"))
        if amount is None or amount == 0:
            continue
        if abs(amount) > ADJUSTMENT_MAX_SHARE * total:
            return None
        if not _adjustment_grounded(adj.get("label"), amount, raw_text):
            return None
        out.append({"label": str(adj.get("label") or "").strip()[:60], "amount": round(amount, 2)})
    return out


def accept_reread(parsed: dict, reread: Any) -> tuple[bool, list[str]]:
    """Judge a re-read against the first reading. On acceptance ``parsed`` is
    updated in place and ``(True, notes)`` returned; otherwise ``parsed`` is
    untouched and the note says why.

    Accepted only when all of these hold:
      * the re-read's own lines (plus its adjustments) add up to its total;
      * it agrees with the first reading on one side — the same bill total,
        or the same line sum — so only the other side changed;
      * every adjustment is backed by the first transcription;
      * it does not lose more than one item line.
    """
    if not isinstance(reread, dict):
        return False, ["re-read: no usable answer"]
    old_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    old_total = _money(parsed.get("total"))
    old_sum = item_sum(old_items)
    new_total = _money(reread.get("total"))
    new_items, _ = repair_lines(normalize_items(reread.get("items")), new_total)
    new_sum = item_sum(new_items)
    if new_total is None or new_total <= 0 or new_sum is None or old_total is None or old_sum is None:
        return False, ["re-read: incomplete"]
    adjustments = _clean_adjustments(reread.get("adjustments"), parsed.get("raw_text"), new_total)
    if adjustments is None:
        return False, ["re-read: adjustment not on the bill as first read"]
    adj_sum = sum(a["amount"] for a in adjustments)
    if not _close(new_sum + adj_sum, new_total):
        return False, ["re-read: still does not add up"]
    same_total = _close(new_total, old_total, 0.0, 0.011)
    same_sum = _close(new_sum, old_sum, 0.0, 0.011)
    if not (same_total or same_sum):
        return False, ["re-read: disagrees with the first reading on both the lines and the total"]
    if len(new_items) < len(old_items) - 1:
        return False, ["re-read: lost item lines"]
    if suspect_lines(new_items):
        return False, ["re-read: a line disagrees with its own line total"]

    notes = []
    if not same_total:
        notes.append(f"total {old_total:g}→{new_total:g}")
    if not same_sum:
        notes.append(f"lines {old_sum:.2f}→{new_sum:.2f}")
    for adj in adjustments:
        notes.append(f"{adj['label'] or 'adjustment'} {adj['amount']:+.2f}")
    parsed["items"] = new_items
    parsed["total"] = new_total
    if adjustments:
        parsed["adjustments"] = adjustments
    return True, notes


# --- the whole self-check -------------------------------------------------------

RereadFn = Callable[[str], Awaitable[Any]]


async def post_check(parsed: dict, reread: RereadFn | None = None) -> dict:
    """Run after the audit's corrections were applied. Repairs lines again
    (the audit may have changed them) and, if the bill still does not add up,
    asks for one targeted re-read. Returns
    ``{"notes": [...], "reread": "accepted" | "rejected" | "failed" | None,
    "state": "match" | "conflict" | "unknown"}``. Never raises."""
    notes: list[str] = []
    outcome = None
    try:
        items, line_notes = repair_lines(parsed.get("items"), parsed.get("total"))
        if line_notes:
            parsed["items"] = items
            notes.extend(line_notes)
        if reread is not None and reread_enabled() and needs_reread(parsed):
            try:
                answer = await reread(build_reread_prompt(parsed))
            except Exception as exc:
                logger.warning("ocr_smart: re-read call failed: %s", exc)
                outcome = "failed"
            else:
                accepted, reread_notes = accept_reread(parsed, answer)
                outcome = "accepted" if accepted else "rejected"
                if accepted:
                    notes.extend(reread_notes)
                logger.info("ocr_smart: re-read %s (%s)", outcome, "; ".join(reread_notes))
    except Exception:
        logger.exception("ocr_smart.post_check failed; keeping the reading as it was")
    return {"notes": notes, "reread": outcome, "state": parsed_state(parsed)}


def summarize(notes: list[str], limit: int = 3) -> str:
    """One short line of what the self-check changed, for the reply and the
    stored verification notes."""
    if not notes:
        return ""
    shown = notes[:limit]
    more = len(notes) - len(shown)
    return "; ".join(shown) + (f"; +{more} more" if more > 0 else "")
