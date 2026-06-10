"""Quantity forecast + draft message formatting for the order generator.

Two pure concerns, kept out of the DB layer so they're trivially testable:

1. Quantity — the cadence FORKS the calculation (spec §3.3):
     * DAILY items   -> next-day replacement: trailing average of the per-buy
       quantity, weekend-adjusted (Fri/Sat/Sun run hotter — the multiplier is
       DERIVED from the outlet's own history, never hardcoded).
     * WEEKLY/MONTHLY items -> one buy already covers the whole cycle, so the
       trailing average of the per-buy quantity already IS the cycle quantity.
   Then round UP to the supplier pack size when known; otherwise flag it for the
   manager rather than guessing a sack/carton size.

   NOTE: the spec's §3.3 leans on a "v12 sales forecast". That dish→ingredient
   engine does not exist in this codebase, so the honest Phase-1 signal is the
   purchase history itself — for no-stockpile perishables, what they buy IS what
   they use. The method is documented here so it can be swapped for a true sales
   forecast later without touching the cadence/formatting layers.

2. Formatting — the per-outlet Telegram draft, one line per item, with the
   reasoning shown on every line (so the manager trusts/overrides intelligently)
   and the spec's flags: 💰 cheaper alternate, ⚠️ price spike, ❓ NEEDS_REVIEW.
"""
from __future__ import annotations

import math
import statistics
from datetime import date

import order_cadence as oc
import order_items

# Weekdays that typically run hotter for a mamak (Fri/Sat/Sun). Used only to
# DERIVE a multiplier from the outlet's own data, never to hardcode a number.
_WEEKEND_WEEKDAYS = {4, 5, 6}  # Fri, Sat, Sun
# Clamp the derived weekend multiplier so a couple of noisy buys can't 3x an
# order. 1.0 = no weekend bump; 2.0 = double at most.
_WEEKEND_MULT_MIN = 1.0
_WEEKEND_MULT_MAX = 2.0
# How many recent buys feed the trailing average.
_TRAILING_BUYS = 8


def _per_buy_quantities(records: list[dict]) -> list[tuple[date, float]]:
    """Collapse raw line rows to one (date, total_qty) per purchase day, oldest
    first. ``records`` rows have ``date`` (date/ISO) and ``qty`` (number)."""
    by_day: dict[date, float] = {}
    for r in records or []:
        d = oc._to_date(r.get("date"))
        qty = r.get("qty")
        if d is None or not isinstance(qty, (int, float)) or isinstance(qty, bool):
            continue
        if qty <= 0:
            continue
        by_day[d] = by_day.get(d, 0.0) + float(qty)
    return sorted(by_day.items())


def weekend_multiplier(per_buy: list[tuple[date, float]]) -> float:
    """Derive a Fri/Sat/Sun multiplier from history: mean weekend buy / mean
    weekday buy, clamped. Returns 1.0 when either side lacks data."""
    weekend = [q for d, q in per_buy if d.weekday() in _WEEKEND_WEEKDAYS]
    weekday = [q for d, q in per_buy if d.weekday() not in _WEEKEND_WEEKDAYS]
    if not weekend or not weekday:
        return 1.0
    wknd_mean = statistics.fmean(weekend)
    wkdy_mean = statistics.fmean(weekday)
    if wkdy_mean <= 0:
        return 1.0
    mult = wknd_mean / wkdy_mean
    return max(_WEEKEND_MULT_MIN, min(_WEEKEND_MULT_MAX, mult))


def forecast_qty(records: list[dict], cadence_info: dict, *, target_day: date) -> dict:
    """Forecast the quantity to order for ``target_day`` (tomorrow).

    Returns ``{'qty', 'pack', 'pack_known', 'basis'}`` where ``qty`` is the
    rounded order quantity, ``pack`` the unit noun, ``pack_known`` whether a real
    pack size was applied, and ``basis`` a short human explanation. Returns
    ``qty=None`` when there's no usable history.
    """
    per_buy = _per_buy_quantities(records)
    canonical = cadence_info.get("canonical_item")
    pack_noun = order_items.unit_noun(canonical)
    if not per_buy:
        return {"qty": None, "pack": pack_noun, "pack_known": False,
                "basis": "no usable purchase quantities"}

    trailing = [q for _, q in per_buy[-_TRAILING_BUYS:]]
    base = statistics.fmean(trailing)
    cadence = cadence_info.get("cadence")

    if cadence == oc.DAILY:
        mult = weekend_multiplier(per_buy)
        applied = mult if target_day.weekday() in _WEEKEND_WEEKDAYS else 1.0
        raw_qty = base * applied
        if applied > 1.0:
            basis = ("avg daily buy %.1f × %.2f weekend" % (base, applied))
        else:
            basis = "avg daily buy %.1f" % base
    else:
        # One historical buy already spans the cycle to the next buy.
        raw_qty = base
        basis = "avg per-cycle buy %.1f" % base

    pack_size = order_items.DEFAULT_PACK.get((canonical or "").strip().lower())
    if pack_size and pack_size > 0:
        qty = int(math.ceil(raw_qty / pack_size) * pack_size)
        pack_known = True
    else:
        # No known pack — round up to a whole unit and let the manager confirm.
        qty = int(math.ceil(raw_qty))
        pack_known = False
    qty = max(qty, 1)

    return {"qty": qty, "pack": pack_noun, "pack_known": pack_known, "basis": basis}


# --- formatting --------------------------------------------------------------

def _cadence_tag(cadence_info: dict, due_info: dict) -> str:
    cadence = cadence_info.get("cadence")
    if cadence == oc.DAILY:
        return "cadence: daily"
    if cadence == oc.NEEDS_REVIEW:
        return "cadence: needs review"
    pretty = {oc.TWICE_WEEKLY: "2×/week", oc.WEEKLY: "weekly",
              oc.MONTHLY: "monthly"}.get(cadence, cadence.lower())
    return "cadence: %s / due tomorrow" % pretty


def format_item_line(line: dict) -> str:
    """One draft line for an item.

    ``line`` keys: canonical_item, qty, pack, pack_known, cadence_info,
    due_info, supplier, alternate (dict|None), spike (str|None).
    """
    ci = line["cadence_info"]
    name = order_items.display_name(line["canonical_item"])
    qty = line.get("qty")
    pack = line.get("pack") or "unit"
    qty_txt = ("%d %s" % (qty, pack)) if qty is not None else "qty?"

    head = "• %s — %s   (%s)" % (name, qty_txt, _cadence_tag(ci, line.get("due_info", {})))
    out = [head]

    last = ci.get("last_purchase_date")
    gap = ci.get("median_gap_days")
    if last is not None and gap:
        out.append("   ↳ last bought: %s, usually every %.0f days"
                   % (last.isoformat(), gap))
    elif last is not None:
        out.append("   ↳ last bought: %s" % last.isoformat())

    supplier = line.get("supplier")
    if supplier:
        out.append("   ↳ supplier: %s" % supplier)

    # Flags.
    if not line.get("pack_known", False):
        out.append("   ❓ confirm pack size (sack/carton/tin)")
    if ci.get("needs_review"):
        out.append("   ❓ NEEDS REVIEW — %s" % ci.get("reason", "erratic cadence"))
    alt = line.get("alternate")
    if alt:
        out.append("   💰 %s" % alt.get("note", ""))
    spike = line.get("spike")
    if spike:
        out.append("   ⚠️ %s" % spike)
    return "\n".join(out)


def format_item_line_compact(line: dict) -> str:
    """A single-line draft entry — name, qty, cadence tag and flag emojis only.

    Used when an outlet's full (reasoning-rich) draft would need more than two
    Telegram messages: the manager gets a compact, scannable list and the full
    reasoning stays in ``order_drafts`` / the dashboard. Supplier is shown by the
    group header, so it is omitted from the line itself."""
    ci = line["cadence_info"]
    name = order_items.display_name(line["canonical_item"])
    qty = line.get("qty")
    pack = line.get("pack") or "unit"
    qty_txt = ("%d %s" % (qty, pack)) if qty is not None else "qty?"
    cadence = ci.get("cadence")
    tag = {oc.DAILY: "harian", oc.TWICE_WEEKLY: "2×/mgg", oc.WEEKLY: "mingguan",
           oc.MONTHLY: "bulanan", oc.NEEDS_REVIEW: "semak"}.get(
               cadence, str(cadence).lower())
    marks: list[str] = []
    if not line.get("pack_known", False) or ci.get("needs_review"):
        marks.append("❓")
    if line.get("alternate"):
        marks.append("💰")
    if line.get("spike"):
        marks.append("⚠️")
    flag_txt = ("  " + "".join(marks)) if marks else ""
    return "• %s — %s  (%s)%s" % (name, qty_txt, tag, flag_txt)


def _group_by_supplier(lines: list[dict]) -> dict[str, list[dict]]:
    """Group lines by supplier, stable order (known suppliers sort first)."""
    by_supplier: dict[str, list[dict]] = {}
    for ln in lines:
        key = ln.get("supplier") or "Supplier belum dikenalpasti"
        by_supplier.setdefault(key, []).append(ln)
    return by_supplier


def build_outlet_message(outlet_display: str, target_day: date, lines: list[dict]) -> str:
    """The full per-outlet draft as ONE string (unbounded). Kept for callers /
    tests that want the whole draft; Telegram delivery uses
    ``build_outlet_messages`` which splits it under the 4096-char cap."""
    header = "🧾 Senarai order — %s\nUntuk %s (semak & edit sebelum hantar)" % (
        outlet_display, target_day.isoformat())
    if not lines:
        return header + "\n\nTiada item due esok. ✅"

    by_supplier = _group_by_supplier(lines)
    blocks = [header]
    for supplier in sorted(by_supplier):
        blocks.append("\n— %s —" % supplier)
        for ln in by_supplier[supplier]:
            blocks.append(format_item_line(ln))
    blocks.append("\nManager boleh edit kuantiti / buang item sebelum office "
                  "boy hantar ke supplier.")
    return "\n".join(blocks)


# --- Telegram-safe chunking (the 4096-char cap) ------------------------------
# A per-outlet draft with ~50 reasoning-rich items blows past Telegram's 4096
# limit, the send throws, and the message is lost. We split on WHOLE-ITEM
# boundaries (an item's multi-line block is never broken) and, when even that
# needs more than two messages, fall back to the compact one-line form.

# Target max length per emitted message. Well under Telegram's 4096 so the
# [TEST] routing prefix bot.py prepends to chunk #0 + UTF-16 emoji width still
# fit. ``_pack_messages`` guarantees every returned message is <= this.
_TG_SAFETY_LIMIT = 3800


def build_outlet_messages(outlet_display: str, target_day: date,
                          lines: list[dict], *, limit: int = _TG_SAFETY_LIMIT) -> list[str]:
    """Per-outlet draft as a LIST of Telegram-safe messages (each <= ``limit``).

    Splits only on whole-item boundaries — an item's block is never broken across
    messages. Reasoning-rich by default; if the full form would need more than
    TWO messages, falls back to the compact one-line-per-item form (full
    reasoning stays in ``order_drafts``) so a manager isn't sent a wall of text.
    """
    full = _pack_messages(outlet_display, target_day, lines, limit=limit, compact=False)
    if len(full) > 2:
        return _pack_messages(outlet_display, target_day, lines, limit=limit, compact=True)
    return full


def _pack_messages(outlet_display: str, target_day: date, lines: list[dict],
                   *, limit: int, compact: bool) -> list[str]:
    """Greedily pack item blocks into messages no larger than ``limit`` (the
    whole emitted message, header + body + footer), repeating the supplier
    header at the top of a continuation chunk. An item's block is never split."""
    header = "🧾 Senarai order — %s\nUntuk %s (semak & edit sebelum hantar)" % (
        outlet_display, target_day.isoformat())
    if not lines:
        return [header + "\n\nTiada item due esok. ✅"]

    if compact:
        footer = ("\nButiran penuh (sebab/harga/supplier) tersimpan dalam sistem — "
                  "edit kuantiti / buang item sebelum hantar.")
    else:
        footer = ("\nManager boleh edit kuantiti / buang item sebelum office "
                  "boy hantar ke supplier.")
    fmt = format_item_line_compact if compact else format_item_line

    # ``limit`` is the whole-message cap; reserve room for the per-chunk header
    # (the continuation form "(sambungan i/N)" is shorter than this 2-line base)
    # and the footer (added to the last chunk) so the assembled message fits.
    reserve = len(header) + 16 + len(footer) + 2
    body_limit = max(200, limit - reserve)

    chunks: list[list[str]] = [[]]
    cur_len = 0
    cur_supplier: str | None = None

    def _add(text: str) -> None:
        nonlocal cur_len
        chunks[-1].append(text)
        cur_len += len(text) + 1  # +1 for the "\n" join

    grouped = _group_by_supplier(lines)
    for supplier in sorted(grouped):
        sup_hdr = "\n— %s —" % supplier
        for ln in grouped[supplier]:
            block = fmt(ln)
            need_header = supplier != cur_supplier
            addition = (len(sup_hdr) + 1 if need_header else 0) + len(block) + 1
            if chunks[-1] and cur_len + addition > body_limit:
                chunks.append([])
                cur_len = 0
                cur_supplier = None
                need_header = True  # always re-show the supplier in a fresh chunk
            if need_header:
                _add(sup_hdr)
                cur_supplier = supplier
            _add(block)

    n = len(chunks)
    out: list[str] = []
    for i, chunk in enumerate(chunks):
        if n == 1:
            head = header
        elif i == 0:
            head = "%s  (1/%d)" % (header, n)
        else:
            head = "🧾 Senarai order — %s (sambungan %d/%d)" % (
                outlet_display, i + 1, n)
        msg = head + "\n" + "\n".join(chunk)
        if i == n - 1:
            msg += "\n" + footer
        out.append(msg)
    return out
