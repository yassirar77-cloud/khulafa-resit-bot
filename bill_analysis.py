"""Bill analysis — every bill, every item, every shop, once a day.

The real-time spike alert (``price_spike_detection``) looks at ONE receipt
as it lands and only fires past 110% of a trailing average with five prior
samples. That misses the two questions the owners actually ask when they
sit down with the day's bills:

1. **"Which items went up?"** — not just the dramatic jumps: a 6% creep at
   the same supplier is a real cost, and the same-supplier previous price
   is the number a bill reader compares against, not a 90-day average.
2. **"Why is Vista paying more for telur than Bistro?"** — Khulafa's own
   outlets buy the same items from different suppliers at different
   prices. Nobody can see that from inside one shop; the bot can.

This module does both over the cleaned corpus (``shop_price_comparison``
does the cleaning: no internal transfers, no OCR merchant variants, no
future dates, one cut per variant):

* ``find_price_changes`` — for every bill uploaded in the last day, each
  line's unit price against the SAME shop's previous price for the SAME
  cut. Increases past a small threshold become entries, decreases are
  kept separately; each increase carries who else sells it cheaper and
  which outlet pays less.
* ``compare_outlets`` — for every item bought by two or more outlets in
  the window, the latest price each outlet paid (and from whom), cheapest
  first, with the gap to the cheapest.

Delivery (``bot.post_bill_analysis``): the owners' alert group gets the
full English picture; each outlet manager gets a Tamil note about THEIR
bills — the items that went up and the items another branch buys cheaper,
with the supplier to ask. Delivery to managers rides the same
MANAGER_DELIVERY_ENABLED gate as every other manager message.

Hard rules, same as the rest of the reporting layer: nothing here ever
raises — every entry point swallows exceptions and returns a safe default.
A bad bill must never take the analysis down with it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Baseline window for "previous price at the same shop" and the 90-day avg.
DEFAULT_LOOKBACK_DAYS = 90
# Cross-outlet comparison window: what each branch paid recently.
DEFAULT_OUTLET_WINDOW_DAYS = 30
# Bills uploaded within this many hours are "today's bills" to analyse.
DEFAULT_NEW_BILL_HOURS = 24

# An increase must clear BOTH: below this it is rounding / OCR jitter. The
# sen floor is deliberately low — an egg at 42 sen moving 4 sen is a real
# 10% increase — and the percentage carries the weight on dearer items.
MIN_CHANGE_PCT = 5.0
MIN_CHANGE_RM = 0.02
# Past this the number is almost certainly a misread (a tray priced as an
# egg); the sanity gate should have caught it, but never alert on it.
MAX_PLAUSIBLE_CHANGE_PCT = 200.0

# Cross-outlet: a branch is "paying more" once it is this far above the
# cheapest branch.
MIN_OUTLET_GAP_PCT = 5.0
MIN_OUTLET_GAP_RM = 0.02
# Dearest / cheapest beyond this ratio is two different units (kg vs pack),
# not two prices. Shown to the owner as "check unit", never to a manager.
UNIT_MISMATCH_RATIO = 3.0

# Phone-sized lists.
MAX_CHEAPER_SHOPS = 3
MAX_CHEAPER_OUTLETS = 3
MAX_MANAGER_ITEMS = 12
MAX_PRAISE_ITEMS = 6


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_item(canonical: Any) -> str:
    text = str(canonical or "").strip()
    return text.replace("_", " ").title() if text else "Item"


def item_label(canonical_item: Any, variant: Any) -> str:
    """``TELUR GRED A`` -> ``Telur Gred A``; falls back to the canonical."""
    text = str(variant or "").strip()
    return text.title() if text else _display_item(canonical_item)


def outlet_label(code: Any) -> str:
    """Human label for an item_prices outlet code (``D`` -> ``D.U``)."""
    try:
        from outlet_mapping import outlet_display_name

        return outlet_display_name(code)
    except Exception:
        return str(code or "?")


def _fmt_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 10:
        return ""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return value[:10]


def _sort_key(row: dict) -> tuple:
    receipt_id = row.get("receipt_id")
    return (
        str(row.get("receipt_date") or ""),
        receipt_id if isinstance(receipt_id, int) else -1,
    )


def _parse_ts(value: Any) -> datetime | None:
    """A ``created_at`` timestamptz -> aware UTC datetime, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --- which rows are "today's bills" -----------------------------------------

def is_new_row(row: dict, now: datetime, hours: int = DEFAULT_NEW_BILL_HOURS,
               today: date | None = None) -> bool:
    """A row belongs to today's analysis when its bill was uploaded within
    ``hours``. Rows without a usable ``created_at`` (older schema, backfills)
    fall back to a receipt_date of yesterday or today."""
    try:
        since = now - timedelta(hours=int(hours))
        created = _parse_ts(row.get("created_at"))
        if created is not None:
            return created >= since
        base = today or now.date()
        when = str(row.get("receipt_date") or "")[:10]
        return bool(when) and when >= (base - timedelta(days=1)).isoformat()
    except Exception:
        return False


def split_new_rows(rows: list[dict], now: datetime,
                   hours: int = DEFAULT_NEW_BILL_HOURS,
                   today: date | None = None) -> tuple[list[dict], list[dict]]:
    """``(new_rows, baseline_rows)``. Never raises."""
    new: list[dict] = []
    base: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        (new if is_new_row(row, now, hours, today) else base).append(row)
    return new, base


# --- price changes vs the same shop's previous price ------------------------

def _group_key(row: dict) -> tuple:
    return (
        str(row.get("canonical_item") or ""),
        str(row.get("variant") or ""),
        str(row.get("shop_key") or row.get("shop") or ""),
    )


def find_price_changes(
    rows: list[dict],
    now: datetime,
    *,
    hours: int = DEFAULT_NEW_BILL_HOURS,
    today: date | None = None,
    min_pct: float = MIN_CHANGE_PCT,
    min_rm: float = MIN_CHANGE_RM,
    max_pct: float = MAX_PLAUSIBLE_CHANGE_PCT,
) -> dict:
    """Compare each new bill line with the same shop's previous price.

    Returns ``{'increases': [...], 'decreases': [...], 'stats': {...}}``.
    One entry per (item, cut, shop, outlet) — the outlet dimension is what
    lets each manager be told about THEIR bill. Entries carry:
    ``canonical_item, variant, label, shop, shop_key, outlet_code, outlet,
    previous_price, previous_date, previous_outlet, new_price, new_date,
    receipt_id, change_rm, change_pct, avg_price, sample_count``.
    Increases sort by change_pct descending, decreases ascending. Never
    raises.
    """
    stats = {
        "new_rows": 0, "new_bills": 0, "compared": 0, "no_history": 0,
        "unchanged": 0, "implausible": 0, "increases": 0, "decreases": 0,
    }
    try:
        new_rows, base_rows = split_new_rows(rows, now, hours, today)
        stats["new_rows"] = len(new_rows)
        stats["new_bills"] = len({
            r.get("receipt_id") for r in new_rows if r.get("receipt_id") is not None
        })
        if not new_rows:
            return {"increases": [], "decreases": [], "stats": stats}

        baseline: dict[tuple, list[dict]] = {}
        for row in base_rows:
            baseline.setdefault(_group_key(row), []).append(row)
        for group in baseline.values():
            group.sort(key=_sort_key)

        # Latest new row per (group, outlet): a shop delivering twice in a
        # day is judged on the later bill, not alerted twice.
        latest: dict[tuple, dict] = {}
        for row in new_rows:
            key = _group_key(row) + (row.get("outlet_code"),)
            if key not in latest or _sort_key(row) > _sort_key(latest[key]):
                latest[key] = row

        increases: list[dict] = []
        decreases: list[dict] = []
        for key, row in latest.items():
            try:
                history = baseline.get(key[:3]) or []
                row_key = _sort_key(row)
                prior = [h for h in history if _sort_key(h) < row_key]
                if not prior:
                    stats["no_history"] += 1
                    continue
                previous = prior[-1]
                new_price = float(row["unit_price"])
                prev_price = float(previous["unit_price"])
                if prev_price <= 0 or new_price <= 0:
                    continue
                stats["compared"] += 1
                change_rm = new_price - prev_price
                change_pct = change_rm / prev_price * 100.0
                # Round to the sen before applying the floor: 0.50 - 0.45 is
                # 0.04999… in binary and must still count as five sen.
                if abs(change_pct) < min_pct or round(abs(change_rm), 4) < min_rm:
                    stats["unchanged"] += 1
                    continue
                if abs(change_pct) > max_pct:
                    stats["implausible"] += 1
                    continue
                prices = [float(h["unit_price"]) for h in prior]
                entry = {
                    "canonical_item": key[0],
                    "variant": key[1],
                    "label": item_label(key[0], key[1]),
                    "shop": row.get("shop") or "",
                    "shop_key": key[2],
                    "outlet_code": row.get("outlet_code"),
                    "outlet": outlet_label(row.get("outlet_code")) if row.get("outlet_code") else "",
                    "previous_price": prev_price,
                    "previous_date": previous.get("receipt_date") or "",
                    "previous_outlet": (
                        outlet_label(previous.get("outlet_code"))
                        if previous.get("outlet_code") else ""
                    ),
                    "new_price": new_price,
                    "new_date": row.get("receipt_date") or "",
                    "receipt_id": row.get("receipt_id"),
                    "change_rm": change_rm,
                    "change_pct": change_pct,
                    "avg_price": sum(prices) / len(prices),
                    "sample_count": len(prices),
                    "cheaper_shops": [],
                    "cheaper_outlets": [],
                }
                (increases if change_rm > 0 else decreases).append(entry)
            except Exception:
                logger.exception("bill analysis: change entry failed (skipping)")
                continue

        increases.sort(key=lambda e: (-e["change_pct"], e["label"], e["outlet"]))
        decreases.sort(key=lambda e: (e["change_pct"], e["label"], e["outlet"]))
        stats["increases"] = len(increases)
        stats["decreases"] = len(decreases)
        return {"increases": increases, "decreases": decreases, "stats": stats}
    except Exception:
        logger.exception("bill analysis: find_price_changes failed")
        return {"increases": [], "decreases": [], "stats": stats}


# --- cross-outlet comparison -------------------------------------------------

def compare_outlets(
    rows: list[dict],
    *,
    min_gap_pct: float = MIN_OUTLET_GAP_PCT,
    min_gap_rm: float = MIN_OUTLET_GAP_RM,
    mismatch_ratio: float = UNIT_MISMATCH_RATIO,
) -> list[dict]:
    """What each outlet last paid for every item bought by two or more.

    One entry per (item, cut) with ``outlets`` cheapest first — each
    ``{outlet_code, outlet, latest_price, latest_date, shop, avg_price,
    sample_count, gap_rm, gap_pct, pays_more}`` — plus ``cheapest``,
    ``dearest``, ``spread_rm``, ``spread_pct``, ``has_gap`` (someone pays
    more than the threshold) and ``unit_mismatch`` (the spread is too wide
    to be the same unit). Widest spread first. Never raises.
    """
    try:
        groups: dict[tuple, dict[str, list[dict]]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = row.get("outlet_code")
            if not isinstance(code, str) or not code.strip():
                continue
            price = _to_float(row.get("unit_price"))
            if price is None or price <= 0:
                continue
            key = (str(row.get("canonical_item") or ""), str(row.get("variant") or ""))
            groups.setdefault(key, {}).setdefault(code.strip().upper(), []).append(row)

        out: list[dict] = []
        for (canonical, variant), by_outlet in groups.items():
            if len(by_outlet) < 2:
                continue
            outlets: list[dict] = []
            for code, outlet_rows in by_outlet.items():
                newest = max(outlet_rows, key=_sort_key)
                prices = [float(r["unit_price"]) for r in outlet_rows]
                outlets.append({
                    "outlet_code": code,
                    "outlet": outlet_label(code),
                    "latest_price": float(newest["unit_price"]),
                    "latest_date": newest.get("receipt_date") or "",
                    "shop": newest.get("shop") or "",
                    "avg_price": sum(prices) / len(prices),
                    "sample_count": len(prices),
                })
            outlets.sort(key=lambda o: (o["latest_price"], o["outlet"]))
            cheapest = outlets[0]
            dearest = outlets[-1]
            base = cheapest["latest_price"]
            for o in outlets:
                o["gap_rm"] = o["latest_price"] - base
                o["gap_pct"] = (o["gap_rm"] / base * 100.0) if base > 0 else 0.0
                o["pays_more"] = (
                    o["gap_pct"] >= min_gap_pct and round(o["gap_rm"], 4) >= min_gap_rm
                )
            spread_rm = dearest["latest_price"] - base
            spread_pct = (spread_rm / base * 100.0) if base > 0 else 0.0
            mismatch = base > 0 and dearest["latest_price"] / base > mismatch_ratio
            out.append({
                "canonical_item": canonical,
                "variant": variant,
                "label": item_label(canonical, variant),
                "outlets": outlets,
                "cheapest": cheapest,
                "dearest": dearest,
                "spread_rm": spread_rm,
                "spread_pct": spread_pct,
                "has_gap": any(o["pays_more"] for o in outlets),
                "unit_mismatch": bool(mismatch),
            })
        # Widest real gap first; "check unit" items sink to the bottom so a
        # kg-vs-pack misread never headlines the report.
        out.sort(key=lambda c: (
            c["unit_mismatch"], not c["has_gap"], -c["spread_pct"], c["label"],
        ))
        return out
    except Exception:
        logger.exception("bill analysis: compare_outlets failed")
        return []


def attach_alternatives(increases: list[dict], rows: list[dict],
                        comparisons: list[dict]) -> None:
    """Fill each increase's ``cheaper_shops`` (other suppliers below the
    new price, cheapest first) and ``cheaper_outlets`` (branches paying
    less, with their supplier). In place; never raises."""
    try:
        from shop_price_comparison import summarise_shops

        by_item: dict[tuple, list[dict]] = {}
        for row in rows:
            if isinstance(row, dict):
                by_item.setdefault(
                    (str(row.get("canonical_item") or ""), str(row.get("variant") or "")),
                    [],
                ).append(row)
        comp_index = {(c["canonical_item"], c["variant"]): c for c in comparisons}

        for entry in increases:
            try:
                key = (entry["canonical_item"], entry["variant"])
                price = float(entry["new_price"])
                shops = summarise_shops(by_item.get(key, []))
                entry["cheaper_shops"] = [
                    {"shop": s["shop"], "latest_price": float(s["latest_price"])}
                    for s in shops
                    if s.get("shop") and str(s["shop"]).strip()
                    and float(s["latest_price"]) < price
                    and _same_shop(s["shop"], entry["shop"]) is False
                ][:MAX_CHEAPER_SHOPS]
                comp = comp_index.get(key)
                cheaper_outlets = []
                if comp and not comp.get("unit_mismatch"):
                    for o in comp["outlets"]:
                        if o["outlet_code"] == entry.get("outlet_code"):
                            continue
                        if float(o["latest_price"]) < price:
                            cheaper_outlets.append({
                                "outlet": o["outlet"],
                                "outlet_code": o["outlet_code"],
                                "latest_price": float(o["latest_price"]),
                                "shop": o.get("shop") or "",
                            })
                entry["cheaper_outlets"] = cheaper_outlets[:MAX_CHEAPER_OUTLETS]
            except Exception:
                logger.exception("bill analysis: alternatives failed for one entry")
                continue
    except Exception:
        logger.exception("bill analysis: attach_alternatives failed")


def _same_shop(a: Any, b: Any) -> bool:
    try:
        from shop_price_comparison import shop_key

        return shop_key(a) == shop_key(b)
    except Exception:
        return str(a or "").strip().upper() == str(b or "").strip().upper()


# --- gather ------------------------------------------------------------------

def gather_bill_analysis(
    supabase,
    *,
    now: datetime | None = None,
    today: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    outlet_window_days: int = DEFAULT_OUTLET_WINDOW_DAYS,
    hours: int = DEFAULT_NEW_BILL_HOURS,
) -> dict:
    """End-to-end: load the cleaned corpus once, then both analyses.

    Returns ``{'increases', 'decreases', 'comparisons', 'stats', 'load_stats',
    'window_days', 'hours', 'outlet_codes'}``. Never raises — an empty
    bundle on failure.
    """
    empty = {
        "increases": [], "decreases": [], "comparisons": [], "stats": {},
        "load_stats": {}, "window_days": outlet_window_days, "hours": hours,
        "outlet_codes": [],
    }
    try:
        from shop_price_comparison import load_all_price_rows_with_stats

        now = now or datetime.now(timezone.utc)
        base_today = today or now.date()
        rows, load_stats = load_all_price_rows_with_stats(
            supabase, lookback_days=lookback_days, today=base_today
        )
        cutoff = (base_today - timedelta(days=int(outlet_window_days))).isoformat()
        window_rows = [r for r in rows if str(r.get("receipt_date") or "") >= cutoff]

        comparisons = compare_outlets(window_rows)
        changes = find_price_changes(rows, now, hours=hours, today=base_today)
        attach_alternatives(changes["increases"], window_rows, comparisons)

        codes: set[str] = set()
        for e in changes["increases"]:
            if e.get("outlet_code"):
                codes.add(e["outlet_code"])
        for c in comparisons:
            for o in c["outlets"]:
                codes.add(o["outlet_code"])
        return {
            "increases": changes["increases"],
            "decreases": changes["decreases"],
            "comparisons": comparisons,
            "stats": changes["stats"],
            "load_stats": load_stats,
            "window_days": outlet_window_days,
            "hours": hours,
            "outlet_codes": sorted(codes),
        }
    except Exception:
        logger.exception("bill analysis: gather failed")
        return empty


def entries_for_outlet(bundle: dict, outlet_code: str) -> dict:
    """The slice of a bundle one manager should hear about: their own
    increases, the items where their branch pays more than the cheapest
    branch (unit-mismatch items excluded), and the items where they are the
    cheapest. Never raises."""
    try:
        code = str(outlet_code or "").strip().upper()
        increases = [
            e for e in bundle.get("increases") or []
            if str(e.get("outlet_code") or "").upper() == code
        ]
        pays_more: list[dict] = []
        cheapest: list[dict] = []
        for comp in bundle.get("comparisons") or []:
            if comp.get("unit_mismatch"):
                continue
            mine = next(
                (o for o in comp["outlets"] if o["outlet_code"] == code), None
            )
            if mine is None:
                continue
            if mine.get("pays_more"):
                pays_more.append({
                    "label": comp["label"],
                    "mine": mine,
                    "cheapest": comp["cheapest"],
                })
            elif mine is comp["cheapest"] and comp.get("has_gap"):
                cheapest.append(comp["label"])
        pays_more.sort(key=lambda p: -p["mine"]["gap_pct"])
        return {"increases": increases, "pays_more": pays_more, "cheapest": cheapest}
    except Exception:
        logger.exception("bill analysis: entries_for_outlet failed")
        return {"increases": [], "pays_more": [], "cheapest": []}


# --- owner (English) ---------------------------------------------------------

def _rm(value: float) -> str:
    return f"RM{float(value):.2f}"


def format_owner_price_report(bundle: dict) -> str:
    """The owners' price-change report for today's bills. ``""`` when no
    bills were analysed AND nothing moved (the caller then stays quiet on
    the scheduled run). Never raises."""
    try:
        increases = bundle.get("increases") or []
        decreases = bundle.get("decreases") or []
        stats = bundle.get("stats") or {}
        hours = int(bundle.get("hours") or DEFAULT_NEW_BILL_HOURS)
        if not stats.get("new_rows") and not increases and not decreases:
            return ""

        lines = [f"📈 Bill analysis — price changes (bills uploaded in the last {hours}h)"]
        if increases:
            lines += ["", "PRICE INCREASES (vs the same shop's previous price):"]
            grouped: dict[tuple, list[dict]] = {}
            for e in increases:
                grouped.setdefault((e["label"], e["shop_key"]), []).append(e)
            for entries in grouped.values():
                first = entries[0]
                outlets = ", ".join(
                    sorted({e["outlet"] for e in entries if e.get("outlet")})
                )
                where = f" [{outlets}]" if outlets else ""
                lines.append(f"• {first['label']} — {first['shop']}{where}")
                prev_when = _fmt_date(first["previous_date"])
                new_when = _fmt_date(first["new_date"])
                prev_part = f" ({prev_when})" if prev_when else ""
                new_part = f" ({new_when})" if new_when else ""
                lines.append(
                    f"   {_rm(first['previous_price'])}{prev_part} → "
                    f"{_rm(first['new_price'])}{new_part}  "
                    f"+{_rm(first['change_rm'])} (+{first['change_pct']:.1f}%) · "
                    f"avg {_rm(first['avg_price'])} ({first['sample_count']}x)"
                )
                # Different outlets may have got different new prices.
                if len({round(e["new_price"], 4) for e in entries}) > 1:
                    lines.append(
                        "   per outlet: " + " · ".join(
                            f"{e['outlet'] or '?'} {_rm(e['new_price'])}" for e in entries
                        )
                    )
                tips = []
                if first.get("cheaper_shops"):
                    tips.append("cheaper at " + ", ".join(
                        f"{s['shop']} {_rm(s['latest_price'])}"
                        for s in first["cheaper_shops"]
                    ))
                if first.get("cheaper_outlets"):
                    tips.append(", ".join(
                        f"{o['outlet']} pays {_rm(o['latest_price'])}"
                        + (f" ({o['shop']})" if o.get("shop") else "")
                        for o in first["cheaper_outlets"]
                    ))
                if tips:
                    lines.append("   💡 " + " · ".join(tips))
        else:
            lines += ["", "✅ No price increases on today's bills."]

        if decreases:
            lines += ["", "PRICE DROPS:"]
            seen: set[tuple] = set()
            for e in decreases:
                key = (e["label"], e["shop_key"])
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"• {e['label']} — {e['shop']}: {_rm(e['previous_price'])} → "
                    f"{_rm(e['new_price'])} ({e['change_pct']:.1f}%)"
                )

        lines += [
            "",
            f"Bills analysed: {stats.get('new_bills', 0)} · line items: "
            f"{stats.get('new_rows', 0)} · compared with history: "
            f"{stats.get('compared', 0)} · no history yet: "
            f"{stats.get('no_history', 0)}"
            + (
                f" · implausible (check OCR): {stats['implausible']}"
                if stats.get("implausible") else ""
            ),
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("bill analysis: owner price report failed")
        return ""


def format_owner_outlet_report(bundle: dict, *, max_items: int | None = None) -> str:
    """Every item two or more branches buy, what each paid last, cheapest
    first. ``""`` when nothing is comparable. Never raises."""
    try:
        comparisons = bundle.get("comparisons") or []
        if not comparisons:
            return ""
        days = int(bundle.get("window_days") or DEFAULT_OUTLET_WINDOW_DAYS)
        lines = [f"🏪 Outlet price comparison — every item (last {days} days)"]
        shown = comparisons[:max_items] if max_items else comparisons
        gaps = sum(1 for c in comparisons if c["has_gap"])
        mismatches = sum(1 for c in comparisons if c["unit_mismatch"])
        for comp in shown:
            parts = []
            for i, o in enumerate(comp["outlets"]):
                shop = f" ({o['shop']})" if o.get("shop") else ""
                if i == 0:
                    parts.append(f"🥇 {o['outlet']} {_rm(o['latest_price'])}{shop}")
                else:
                    parts.append(
                        f"{o['outlet']} {_rm(o['latest_price'])}{shop} "
                        f"+{o['gap_pct']:.0f}%"
                    )
            flag = " ⚠️ check unit" if comp["unit_mismatch"] else ""
            lines.append("")
            lines.append(f"{comp['label']}{flag}:")
            lines.append("   " + " · ".join(parts))
        if max_items and len(comparisons) > len(shown):
            lines.append("")
            lines.append(f"… +{len(comparisons) - len(shown)} more item(s)")
        lines += [
            "",
            f"Items compared: {len(comparisons)} · a branch pays ≥"
            f"{MIN_OUTLET_GAP_PCT:.0f}% more: {gaps}"
            + (f" · ⚠️ unit mismatch: {mismatches}" if mismatches else ""),
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("bill analysis: owner outlet report failed")
        return ""


def format_owner_delivery_summary(routes: list[dict], enabled: bool) -> str:
    """One line per outlet message: who it went to. ``""`` when nothing was
    sent. Never raises."""
    try:
        if not routes:
            return ""
        lines = ["📬 Bill analysis — manager notes:"]
        for r in routes:
            if r.get("reason") == "manager":
                who = f"→ {r.get('manager_name') or 'manager'}"
            elif r.get("reason") == "no_manager":
                who = "→ (no manager registered — sent to you)"
            else:
                who = "→ you (test)"
            lines.append(
                f"• {r.get('display') or r.get('outlet_code')}: "
                f"{r.get('increases', 0)} increase(s), "
                f"{r.get('pays_more', 0)} item(s) cheaper elsewhere {who}"
            )
        lines.append(
            "🟢 LIVE — delivered to registered managers" if enabled
            else "🧪 TEST MODE — every manager note above was sent to you, NOT to managers"
        )
        return "\n".join(lines)
    except Exception:
        logger.exception("bill analysis: delivery summary failed")
        return ""


# --- manager (Tamil) ---------------------------------------------------------

def format_manager_note(outlet_code: str, slice_: dict) -> str:
    """The outlet manager's Tamil note: the items on their bills that went
    up (ask the supplier why), the items another branch buys cheaper (ask
    for that rate / try that supplier), and where they are the cheapest.
    ``""`` when there is nothing to say. Never raises."""
    try:
        increases = list(slice_.get("increases") or [])
        pays_more = list(slice_.get("pays_more") or [])
        cheapest = list(slice_.get("cheapest") or [])
        if not increases and not pays_more:
            return ""
        outlet = outlet_label(outlet_code)
        lines = [f"🧾 Bill analysis — {outlet}"]

        if increases:
            lines += ["", "📈 Unga bill-la intha items vilai eriyirukku:"]
            for e in increases[:MAX_MANAGER_ITEMS]:
                when = _fmt_date(e["new_date"])
                when_part = f" · {when}" if when else ""
                lines.append(
                    f"• {e['label']} ({e['shop']}): {_rm(e['previous_price'])} → "
                    f"{_rm(e['new_price'])} (+{e['change_pct']:.0f}%){when_part}"
                )
                if e.get("cheaper_shops"):
                    lines.append("   Vera kadaiyila cheap: " + ", ".join(
                        f"{s['shop']} {_rm(s['latest_price'])}" for s in e["cheaper_shops"]
                    ))
                if e.get("cheaper_outlets"):
                    lines.append("   Vera branch: " + ", ".join(
                        f"{o['outlet']} {_rm(o['latest_price'])}"
                        + (f" ({o['shop']})" if o.get("shop") else "")
                        for o in e["cheaper_outlets"]
                    ))
            if len(increases) > MAX_MANAGER_ITEMS:
                lines.append(f"… innum {len(increases) - MAX_MANAGER_ITEMS} items")
            lines.append("👉 Supplier-kitta yen vilai eruchu-nu kelunga.")

        if pays_more:
            lines += ["", "🏪 Ithe item vera branch cheap-aa vaanguthu:"]
            for p in pays_more[:MAX_MANAGER_ITEMS]:
                mine = p["mine"]
                best = p["cheapest"]
                my_shop = f" ({mine['shop']})" if mine.get("shop") else ""
                best_shop = f" ({best['shop']})" if best.get("shop") else ""
                lines.append(
                    f"• {p['label']}: Neenga {_rm(mine['latest_price'])}{my_shop} · "
                    f"{best['outlet']} {_rm(best['latest_price'])}{best_shop} — "
                    f"{mine['gap_pct']:.0f}% cheap"
                )
            if len(pays_more) > MAX_MANAGER_ITEMS:
                lines.append(f"… innum {len(pays_more) - MAX_MANAGER_ITEMS} items")
            lines.append(
                "👉 Unga supplier-kitta antha rate kelunga, illanna antha "
                "branch-oda supplier-ai try pannunga."
            )

        if cheapest:
            names = ", ".join(cheapest[:MAX_PRAISE_ITEMS])
            more = f" +{len(cheapest) - MAX_PRAISE_ITEMS}" if len(cheapest) > MAX_PRAISE_ITEMS else ""
            lines += ["", f"✅ Neenga cheapest-aa vaangurathu: {names}{more} — super! 👍"]

        lines += ["", "Rate compare panni, enna aachunnu sollunga. 🙏"]
        return "\n".join(lines)
    except Exception:
        logger.exception("bill analysis: manager note failed")
        return ""


# --- on-demand: one item across outlets --------------------------------------

def build_outlet_price_report(supabase, query: str | None = None, *,
                              today: date | None = None,
                              window_days: int = DEFAULT_OUTLET_WINDOW_DAYS) -> str:
    """``/outlet_prices [item]`` — the cross-outlet comparison on demand,
    for one item (free text, same resolver as /shop_prices) or for every
    item. Always returns a string. Never raises."""
    try:
        from shop_price_comparison import resolve_item_query

        base_today = today or date.today()
        bundle = gather_bill_analysis(
            supabase, today=base_today, outlet_window_days=window_days
        )
        comparisons = bundle.get("comparisons") or []
        text = (query or "").strip()
        if not text:
            report = format_owner_outlet_report(bundle)
            return report or (
                f"No item was bought by two or more outlets in the last "
                f"{window_days} days — nothing to compare yet."
            )

        resolved = resolve_item_query(text)
        canonical = resolved.get("canonical") if isinstance(resolved, dict) else None
        if not canonical:
            suggestions = resolved.get("suggestions") if isinstance(resolved, dict) else None
            hint = (
                "\nDid you mean: " + ", ".join(str(s) for s in suggestions)
                if suggestions else ""
            )
            return f"I don't know an item called \"{text}\".{hint}"

        wanted = [c for c in comparisons if c["canonical_item"] == canonical]
        label = _display_item(canonical)
        if not wanted:
            return (
                f"{label}: only one outlet has bought it in the last {window_days} "
                f"days — nothing to compare across branches."
            )
        return format_owner_outlet_report({
            "comparisons": wanted, "window_days": window_days,
        })
    except Exception:
        logger.exception("bill analysis: outlet price report failed")
        return "Failed to build the outlet price comparison."
