"""Overbuying watch — sales dropped, ordering didn't.

The bot already holds both trends: every shop's POS sales arrive by email
(reconciled per outlet per day into ``purchase_reconciliation``) and every
purchase arrives as a receipt (per-line in ``item_prices``). This module
crosses them: when an outlet's sales are clearly down but it keeps buying
the same quantities, the manager gets asked — in Tamil — why the orders
haven't come down with the sales.

Windows: the last 7 FULL days (ending yesterday, so a partial today never
skews the numbers) against the 21 days before that as a weekly baseline.

An outlet is flagged when ALL of these hold:
  * both windows have enough data days (``_MIN_WEEK_DAYS`` /
    ``_MIN_BASE_DAYS``) — a missing POS email must never masquerade as a
    sales crash;
  * sales are down at least ``_MIN_SALES_DROP_PCT`` vs the baseline
    weekly average;
  * purchases fell by less than ``_ADJUST_FACTOR`` of the sales drop
    (i.e. the buying didn't follow the sales down — including buying
    MORE).

Item detail rides on ``monthly_consumption``'s kg estimation for the
tracked protein categories: the message names the categories still being
bought at the old volume, with this week's kg beside the usual kg.

Hard rule (same as the rest of the reporting layer): nothing here ever
raises — every entry point swallows exceptions and returns a safe
default. A watch failure must never crash the bot.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_RECON_TABLE = "purchase_reconciliation"

_BASE_WEEKS = 3
_MIN_WEEK_DAYS = 6    # of 7 — one missing day tolerated
_MIN_BASE_DAYS = 18   # of 21
_MIN_SALES_DROP_PCT = 10.0
_ADJUST_FACTOR = 0.5  # purchases must fall at least half as much as sales
_MIN_BASE_KG = 5.0    # ignore categories too small to matter (kg / week)
_MIN_BASE_UNITS = 10.0  # same floor for unit-counted lines (ekor)
_MAX_ITEMS = 4        # name at most this many categories in the message


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def watch_windows(today: date | None = None) -> dict:
    """The comparison windows as ISO strings.

    ``week``: the last 7 full days (yesterday inclusive, going back).
    ``base``: the 21 days immediately before that.
    """
    base_today = today or date.today()
    week_end = base_today - timedelta(days=1)
    week_start = base_today - timedelta(days=7)
    base_end = base_today - timedelta(days=8)
    base_start = base_today - timedelta(days=7 * (_BASE_WEEKS + 1))
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "base_start": base_start.isoformat(),
        "base_end": base_end.isoformat(),
    }


def load_recon_rows(supabase, start_iso: str, end_iso: str) -> list[dict]:
    """``purchase_reconciliation`` rows for a date range. ``[]`` on failure,
    never raises."""

    def _build():
        return (
            supabase.table(_RECON_TABLE)
            .select(
                "outlet_canonical, business_date, sales_total, "
                "total_food_purchases"
            )
            .gte("business_date", start_iso)
            .lte("business_date", end_iso)
        )

    try:
        from db_pagination import fetch_all_pages

        rows = fetch_all_pages(lambda: _build().order("id", desc=False))
    except Exception:
        try:
            rows = getattr(_build().execute(), "data", None) or []
        except Exception:
            logger.exception(
                "overbuy watch: reconciliation read failed (%s..%s)",
                start_iso, end_iso,
            )
            return []
    return [r for r in rows if isinstance(r, dict)]


def _tally(rows: list[dict]) -> dict:
    """Per-outlet sums over one window: sales RM, purchases RM, and the
    distinct days that actually carried sales data."""
    out: dict[str, dict] = {}
    for row in rows:
        outlet = str(row.get("outlet_canonical") or "").strip()
        if not outlet:
            continue
        bucket = out.setdefault(
            outlet, {"sales": 0.0, "purchases": 0.0, "days": set()}
        )
        sales = _to_float(row.get("sales_total"))
        if sales is not None:
            bucket["sales"] += sales
            bucket["days"].add(row.get("business_date"))
        purchases = _to_float(row.get("total_food_purchases"))
        if purchases is not None:
            bucket["purchases"] += purchases
    return out


def outlet_trends(week_rows: list[dict], base_rows: list[dict]) -> list[dict]:
    """Sales/purchase trend per outlet: this week vs the baseline weekly
    average. One dict per outlet seen in the BASELINE (an outlet with no
    history can't have a trend). Never raises."""
    try:
        week = _tally([r for r in week_rows or [] if isinstance(r, dict)])
        base = _tally([r for r in base_rows or [] if isinstance(r, dict)])
        trends = []
        for outlet, b in base.items():
            w = week.get(outlet, {"sales": 0.0, "purchases": 0.0, "days": set()})
            base_weekly_sales = b["sales"] / _BASE_WEEKS
            base_weekly_purchases = b["purchases"] / _BASE_WEEKS
            sales_drop = (
                (base_weekly_sales - w["sales"]) / base_weekly_sales * 100.0
                if base_weekly_sales > 0 else None
            )
            purchase_drop = (
                (base_weekly_purchases - w["purchases"])
                / base_weekly_purchases * 100.0
                if base_weekly_purchases > 0 else None
            )
            trends.append({
                "outlet": outlet,
                "week_sales": w["sales"],
                "base_weekly_sales": base_weekly_sales,
                "week_purchases": w["purchases"],
                "base_weekly_purchases": base_weekly_purchases,
                "week_days": len(w["days"]),
                "base_days": len(b["days"]),
                "sales_drop_pct": sales_drop,
                "purchase_drop_pct": purchase_drop,
            })
        return trends
    except Exception:
        logger.exception("overbuy watch: trend computation failed")
        return []


def find_overbuying(trends: list[dict]) -> list[dict]:
    """The outlets whose buying didn't follow their sales down. Sorted by
    sales-drop severity, worst first. Never raises."""
    try:
        flagged = []
        for t in trends or []:
            if not isinstance(t, dict):
                continue
            sales_drop = t.get("sales_drop_pct")
            purchase_drop = t.get("purchase_drop_pct")
            if sales_drop is None or purchase_drop is None:
                continue
            if t.get("week_days", 0) < _MIN_WEEK_DAYS:
                continue
            if t.get("base_days", 0) < _MIN_BASE_DAYS:
                continue
            if sales_drop < _MIN_SALES_DROP_PCT:
                continue
            if purchase_drop >= sales_drop * _ADJUST_FACTOR:
                continue
            flagged.append(t)
        flagged.sort(key=lambda t: -t["sales_drop_pct"])
        return flagged
    except Exception:
        logger.exception("overbuy watch: flagging failed")
        return []


def load_item_rows(supabase, start_iso: str, end_iso: str) -> list[dict]:
    """Tracked-category ``item_prices`` rows WITH outlet_code for a range,
    purchases only (non-purchase receipts and own-outlet transfers dropped
    the same way the monthly report does it). ``[]`` on failure."""
    try:
        from monthly_consumption import (
            TRACKED_CATEGORIES,
            _excluded_receipt_ids,
            _is_own_outlet,
        )

        def _build():
            return (
                supabase.table("item_prices")
                .select(
                    "outlet_code, canonical_item, raw_item_name, qty, "
                    "line_total, receipt_id, receipt_date, merchant"
                )
                .in_("canonical_item", list(TRACKED_CATEGORIES))
                .gte("receipt_date", start_iso)
                .lte("receipt_date", end_iso)
            )

        try:
            from db_pagination import fetch_all_pages

            rows = fetch_all_pages(lambda: _build().order("id", desc=False))
        except Exception:
            rows = getattr(_build().execute(), "data", None) or []

        rows = [r for r in rows if isinstance(r, dict)]
        excluded = _excluded_receipt_ids(
            supabase, [r.get("receipt_id") for r in rows]
        )
        return [
            r for r in rows
            if r.get("receipt_id") not in excluded
            and not _is_own_outlet(r.get("merchant"))
        ]
    except Exception:
        logger.exception(
            "overbuy watch: item_prices read failed (%s..%s)",
            start_iso, end_iso,
        )
        return []


def category_totals(rows: list[dict], outlet_canonical: str) -> dict:
    """``{category: {'kg', 'units'}}`` for one outlet's rows, matching
    ``item_prices.outlet_code`` against the reconciliation's canonical
    outlet name via the kitchen bridge (``outlets_match``). Never raises."""
    try:
        from kitchen_usage import outlets_match
        from monthly_consumption import TRACKED_CATEGORIES, estimate_line_kg

        totals: dict[str, dict] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if row.get("canonical_item") not in TRACKED_CATEGORIES:
                continue
            if not outlets_match(row.get("outlet_code"), outlet_canonical):
                continue
            kg, units = estimate_line_kg(row.get("raw_item_name"), row.get("qty"))
            bucket = totals.setdefault(
                row["canonical_item"], {"kg": 0.0, "units": 0.0}
            )
            if kg is not None:
                bucket["kg"] += kg
            if units is not None:
                bucket["units"] += units
        return totals
    except Exception:
        logger.exception("overbuy watch: category totals failed")
        return {}


def sticky_items(week_totals: dict, base_totals: dict, sales_drop_pct) -> list[dict]:
    """Categories still bought at the old volume while sales dropped.

    A category is sticky when its baseline weekly volume clears the floor
    (kg or units) and its drop is less than ``_ADJUST_FACTOR`` of the
    sales drop. Sorted by baseline kg (the biggest money first), capped at
    ``_MAX_ITEMS``. Never raises."""
    try:
        from monthly_consumption import TRACKED_CATEGORIES

        drop_needed = float(sales_drop_pct) * _ADJUST_FACTOR
        out = []
        for category, base in (base_totals or {}).items():
            if not isinstance(base, dict):
                continue
            week = (week_totals or {}).get(category, {"kg": 0.0, "units": 0.0})
            base_kg = float(base.get("kg") or 0.0) / _BASE_WEEKS
            base_units = float(base.get("units") or 0.0) / _BASE_WEEKS
            if base_kg >= _MIN_BASE_KG:
                measure, week_qty, base_qty = "kg", float(week.get("kg") or 0.0), base_kg
            elif base_units >= _MIN_BASE_UNITS:
                measure, week_qty, base_qty = "ekor", float(week.get("units") or 0.0), base_units
            else:
                continue
            drop_pct = (base_qty - week_qty) / base_qty * 100.0
            if drop_pct >= drop_needed:
                continue  # this category WAS adjusted
            meta = TRACKED_CATEGORIES.get(category, {})
            out.append({
                "category": category,
                "label": meta.get("label", category),
                "emoji": meta.get("emoji", ""),
                "unit": measure,
                "week_qty": week_qty,
                "base_qty": base_qty,
            })
        out.sort(key=lambda i: -i["base_qty"])
        return out[:_MAX_ITEMS]
    except Exception:
        logger.exception("overbuy watch: sticky item selection failed")
        return []


def _fmt_qty(value: float) -> str:
    rounded = round(float(value), 1)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.1f}"


def format_manager_overbuy(entry: dict) -> str:
    """The Tamil question to the outlet manager: sales down X%, buying
    barely moved, these items still at the old volume — why not less?
    Returns ``""`` on malformed input. Never raises."""
    try:
        if not isinstance(entry, dict):
            return ""
        outlet = str(entry.get("outlet") or "").strip()
        sales_drop = float(entry["sales_drop_pct"])
        week_sales = float(entry["week_sales"])
        base_sales = float(entry["base_weekly_sales"])
        week_purch = float(entry["week_purchases"])
        base_purch = float(entry["base_weekly_purchases"])
        purchase_drop = float(entry["purchase_drop_pct"])

        lines = [
            f"📉 Sales இறங்குது, order அப்படியே — {outlet}",
            "",
            f"போன 7 நாள் sales: RM{week_sales:,.0f} "
            f"(வழக்கமா RM{base_sales:,.0f} — {sales_drop:.0f}% கம்மி)",
        ]
        if purchase_drop <= 0:
            lines.append(
                f"ஆனா purchase RM{week_purch:,.0f} "
                f"(வழக்கமா RM{base_purch:,.0f}) — குறையவே இல்ல."
            )
        else:
            lines.append(
                f"ஆனா purchase RM{week_purch:,.0f} "
                f"(வழக்கமா RM{base_purch:,.0f}) — "
                f"{purchase_drop:.0f}% தான் குறைஞ்சிருக்கு."
            )

        items = entry.get("items") or []
        if items:
            lines += ["", "இதெல்லாம் இன்னும் பழைய அளவுலயே வாங்குறீங்க:"]
            for item in items:
                emoji = f"{item['emoji']} " if item.get("emoji") else ""
                lines.append(
                    f"• {emoji}{item['label']}: {_fmt_qty(item['week_qty'])} "
                    f"{item['unit']} (வழக்கமா {_fmt_qty(item['base_qty'])} "
                    f"{item['unit']})"
                )

        lines += [
            "",
            "Sales கம்மி ஆச்சுன்னா order-உம் அதுக்கு ஏத்த மாதிரி குறையணும் — "
            "இல்லன்னா மிச்ச stock wastage ஆகும், cash-um lock ஆகும்.",
            "ஏன் இந்த items குறைக்கலன்னு சொல்லுங்க. "
            "Sales பாத்து supplier order-அ adjust பண்ணுங்க. 🙏",
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("overbuy watch: manager message failed")
        return ""


def format_owner_summary(entries: list[dict]) -> str:
    """Compact English overview for the alert group. ``""`` when nothing is
    flagged. Never raises."""
    try:
        if not entries:
            return ""
        lines = [
            "📉 Sales down but buying not adjusted "
            "(last 7 days vs prior 3-week avg):",
        ]
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                items = ", ".join(
                    f"{i['label']} {_fmt_qty(i['week_qty'])}{i['unit']} "
                    f"vs {_fmt_qty(i['base_qty'])}{i['unit']}"
                    for i in (e.get("items") or [])
                )
                item_part = f" — {items}" if items else ""
                lines.append(
                    f"• {e['outlet']}: sales RM{float(e['week_sales']):,.0f} "
                    f"(−{float(e['sales_drop_pct']):.0f}%), purchases "
                    f"RM{float(e['week_purchases']):,.0f} "
                    f"({float(e['purchase_drop_pct']):+.0f}% vs usual)"
                    f"{item_part}"
                )
            except Exception:
                continue
        if len(lines) == 1:
            return ""
        lines.append("Managers asked in Tamil to adjust orders to sales.")
        return "\n".join(lines)
    except Exception:
        logger.exception("overbuy watch: owner summary failed")
        return ""


def gather_overbuy_entries(supabase, today: date | None = None) -> list[dict]:
    """End-to-end: windows -> trends -> flagged outlets -> item detail.
    Returns the flagged entries with their ``items`` attached. Never
    raises; ``[]`` when nothing qualifies or on failure."""
    try:
        windows = watch_windows(today)
        week_rows = load_recon_rows(
            supabase, windows["week_start"], windows["week_end"]
        )
        base_rows = load_recon_rows(
            supabase, windows["base_start"], windows["base_end"]
        )
        flagged = find_overbuying(outlet_trends(week_rows, base_rows))
        if not flagged:
            return []
        week_items = load_item_rows(
            supabase, windows["week_start"], windows["week_end"]
        )
        base_items = load_item_rows(
            supabase, windows["base_start"], windows["base_end"]
        )
        for entry in flagged:
            entry["items"] = sticky_items(
                category_totals(week_items, entry["outlet"]),
                category_totals(base_items, entry["outlet"]),
                entry["sales_drop_pct"],
            )
        return flagged
    except Exception:
        logger.exception("overbuy watch: gather failed")
        return []
