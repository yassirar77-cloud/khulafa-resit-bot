"""Tomorrow's order, proposed from what the outlet really bought.

The 20:05 order check-in shows the cashier a proposed order instead of an
open question. Quantities are the MEDIAN of the last ``WEEKS`` (4) orders
on the same weekday for that outlet — receipts plus what cashiers told us
(staff_order_items), merged so nothing counts twice. An item with fewer
than ``MIN_POINTS`` (2) such days is still listed but marked "confirm qty".

The code decides the items and the numbers; the AI provider only phrases
the message, and the fact check makes sure every item and number in the
wording is in the proposal. A reply of "ok" saves the proposal as
confirmed; a reply with changes ("ayam 12 bukan 10") is read by the reply
parser, the edits are applied and the whole order is saved — so next
week's median already knows.

Pure: no database. bot.py fetches the history and saves the rows.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta

import staff_chat
import staff_orders

WEEKS = 4
MIN_POINTS = 2
MAX_LINES = 30
SOURCE_CONFIRMED = "confirmed"
SOURCE_REPLY = "reply"


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def same_weekday_dates(target_day: date, weeks: int = WEEKS) -> list[date]:
    """The last ``weeks`` dates on the same weekday before ``target_day``."""
    return [target_day - timedelta(days=7 * k) for k in range(1, weeks + 1)]


def _unit(item: str, rows: list[dict]) -> str:
    for r in rows:
        if r.get("unit"):
            return str(r["unit"])
    try:
        import order_items
        noun = order_items.unit_noun(item)
        return noun if noun != "unit" else "pcs"
    except Exception:
        return "pcs"


def build(rows: list[dict], *, target_day: date, weeks: int = WEEKS) -> list[dict]:
    """Proposed lines for ``target_day`` from history rows (item_prices shape:
    canonical_item, qty, receipt_date; staff rows may carry ``unit``).
    ``[{item, label, qty, unit, confirm, points, samples}]`` sorted like the
    check-ins (key items first, then by quantity). Items never bought on
    that weekday in the window are not proposed."""
    wanted = set(same_weekday_dates(target_day, weeks))
    per_item_day: dict[str, dict[date, float]] = {}
    unit_rows: dict[str, list[dict]] = {}
    for r in rows or []:
        item = str(r.get("canonical_item") or "").strip().lower()
        d = _day(r.get("receipt_date"))
        if not item or d is None or d not in wanted:
            continue
        try:
            qty = float(r.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        days = per_item_day.setdefault(item, {})
        days[d] = days.get(d, 0.0) + qty
        unit_rows.setdefault(item, []).append(r)
    lines = []
    for item, days in per_item_day.items():
        points = sorted(days.values())
        unit = _unit(item, unit_rows[item])
        median = statistics.median(points)
        lines.append({
            "item": item,
            "label": staff_chat.item_label(item),
            "qty": staff_chat.fmt_qty(median, unit),
            "unit": unit,
            "confirm": len(points) < MIN_POINTS,
            "points": len(points),
            "samples": [round(p, 3) for p in points],
        })
    lines = [ln for ln in lines if ln["qty"] and float(ln["qty"]) > 0]
    lines.sort(key=lambda ln: (staff_chat._priority(ln["item"]), -float(ln["qty"]), ln["item"]))
    return lines[:MAX_LINES]


def draft_lines(lines: list[dict]) -> list[dict]:
    """The proposal in the ``order_drafts`` line shape the sanity gate and
    ``staff_chat.order_facts`` read (item, qty, pack, supplier, confirm)."""
    return [{"item": ln["item"], "qty": float(ln["qty"]), "pack": ln["unit"],
             "supplier": None, "confirm": ln["confirm"]} for ln in lines]


def apply_edits(lines: list[dict], items: list[dict]) -> tuple[list[dict], list[str]]:
    """The proposal with the cashier's changes applied: an item they named
    gets their quantity (and unit when given); a new item is added. Returns
    ``(lines, changed_items)``."""
    out = [dict(ln) for ln in lines]
    changed: list[str] = []
    for it in staff_orders.clean_items(items):
        canon = staff_orders.canonical(it["item"]) or str(it["item"]).strip().lower()
        unit = it.get("unit")
        for ln in out:
            if ln["item"] == canon:
                new_qty = staff_chat.fmt_qty(it["qty"], unit or ln["unit"])
                if new_qty != ln["qty"] or (unit and unit != ln["unit"]):
                    ln.update(qty=new_qty, unit=unit or ln["unit"], confirm=False, edited=True)
                    changed.append(canon)
                break
        else:
            out.append({"item": canon, "label": staff_chat.item_label(canon),
                        "qty": staff_chat.fmt_qty(it["qty"], unit), "unit": unit or "pcs",
                        "confirm": False, "points": 0, "samples": [], "edited": True,
                        "raw_item": it["item"]})
            changed.append(canon)
    return out, changed


def order_rows(thread: dict, lines: list[dict], reply_text: str, *,
               confirmed: bool) -> list[dict]:
    """``staff_order_items`` rows for the whole order the cashier confirmed
    (or corrected), for the day after the shift that was asked. Edited
    lines carry source 'reply', the rest 'confirmed'."""
    shift_day = _day(thread.get("shift_date"))
    if shift_day is None:
        return []
    order_for = (shift_day + timedelta(days=1)).isoformat()
    rows = []
    for ln in lines:
        try:
            qty = float(ln["qty"])
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        rows.append({
            "outlet_code": thread.get("outlet_code"),
            "order_for": order_for,
            "raw_item": ln.get("raw_item") or ln.get("label") or ln["item"],
            "canonical_item": staff_orders.canonical(ln["item"]) or ln["item"],
            "qty": round(qty, 3),
            "unit": ln.get("unit"),
            "cashier": thread.get("cashier"),
            "thread_id": thread.get("id"),
            "reply_text": (reply_text or "")[:1000],
            "source": SOURCE_REPLY if ln.get("edited") else SOURCE_CONFIRMED,
        })
    return rows if (confirmed or any(r["source"] == SOURCE_REPLY for r in rows)) else []


def format_director(outlet_label: str, target_day: date, lines: list[dict]) -> str:
    """``/order <OUTLET>``: the proposal as the director reads it."""
    if not lines:
        return (f"No order proposal for {outlet_label} for {target_day.isoformat()} — "
                f"nothing bought on a {target_day.strftime('%A')} in the last {WEEKS} weeks.")
    out = [f"🧾 Proposed order — {outlet_label} for {target_day.isoformat()} "
           f"({target_day.strftime('%A')}, median of the last {WEEKS} {target_day.strftime('%A')}s)", ""]
    for ln in lines:
        mark = f"  ⚠️ confirm qty ({ln['points']} data point)" if ln["confirm"] else ""
        out.append(f"• {ln['label']} {staff_chat._qty_pack(ln['qty'], ln['unit'])}{mark}")
    confirm = sum(1 for ln in lines if ln["confirm"])
    if confirm:
        out += ["", f"{confirm} line(s) need the cashier to confirm the quantity."]
    return "\n".join(out)
