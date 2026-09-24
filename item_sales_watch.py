"""Daily slow-item watch — items selling below the shop's OWN usual level.

The business runs 24 hours: a business day D spans D 07:00 to D+1 07:00 and
reports itself in TWO shift-close emails (the ~19:00 day shift and the
~07:00-next-morning overnight shift), both folded onto business date D at
ingest. This module combines BOTH shifts' item-group quantities (the
``GROUP WISE ITEM SALES (shiftno)`` block -> ``sales_items``: AYAM, NAAN,
TOSAI, MEE n MAGGIE, ...) into one full-24h count per shop per day, and never
judges a day until both shift emails are in.

Each morning the just-closed business day is checked per outlet: every item
group is compared against what THAT shop usually sells per day — the median
over the trailing 28 business days. Dual gate, mamak-tuned, so small items
never spam:

    day_qty <= _SLOW_FACTOR x median      (relative: sold under 60% of usual)
    AND median - day_qty >= _MIN_DROP_QTY (absolute: at least 5 under)
    AND median >= _MIN_MEDIAN_QTY         (the group usually moves >= 8/day)

A group slow for ``_STREAK_DAYS`` consecutive data days escalates from "push
this today" to a quality question — 3 days of naan selling under usual is no
longer a promotion problem, it's "any issue with the taste? check with the
pandari, add what's needed".

Only flagged outlets produce a message: the manager gets it in Tamil (push
suggestions + the streak quality question, with the full-24h framing spelled
out), the owner gets an English summary. Evaluation targets ONLY yesterday's
business day, once per day — no state, no repeats.

Hard rule (same as the rest of the reporting layer): nothing here ever raises —
every entry point swallows exceptions and returns a safe default.
"""
from __future__ import annotations

import logging
import re
import statistics
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_SALES_DAILY_TABLE = "sales_daily"
_SALES_ITEMS_TABLE = "sales_items"

_BASELINE_DAYS = 28
_MIN_BASELINE_DAYS = 7     # data days needed before a shop is judged at all
_MIN_MEDIAN_QTY = 8.0      # group must usually move at least this many/day
_SLOW_FACTOR = 0.6         # today <= 60% of usual
_MIN_DROP_QTY = 5.0        # AND at least this many under usual
_STREAK_DAYS = 3           # consecutive slow data-days -> quality question
_MAX_FLAGS_PER_SHOP = 5    # biggest drops only; a wall of items helps nobody

# Groups that are counting artifacts rather than dishes anyone can "push".
_IGNORED_GROUPS = frozenset({"KARI LAUK", "GULA GULA"})


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_group(name) -> str | None:
    """Join key for an item-group name: upper-cased, whitespace collapsed."""
    if not isinstance(name, str):
        return None
    n = re.sub(r"\s+", " ", name).strip().upper()
    return n or None


def target_business_day(today: date | None = None) -> date:
    """The business day to judge: yesterday. Today's overnight shift won't
    close until ~07:00 TOMORROW, so today is never judgeable."""
    return (today or date.today()) - timedelta(days=1)


# --- loading -----------------------------------------------------------------

def load_shift_rows(supabase, start_iso: str, end_iso: str) -> list[dict]:
    """Per-shift ``sales_daily`` rows for a business-date range. ``[]`` on
    failure, never raises."""

    def _build():
        return (
            supabase.table(_SALES_DAILY_TABLE)
            .select("id, outlet_code, outlet_canonical, shift_business_date, shift_type")
            .gte("shift_business_date", start_iso)
            .lte("shift_business_date", end_iso)
        )

    try:
        from db_pagination import fetch_all_pages

        rows = fetch_all_pages(lambda: _build().order("id", desc=False))
    except Exception:
        try:
            rows = getattr(_build().execute(), "data", None) or []
        except Exception:
            logger.exception(
                "slow items: sales_daily read failed (%s..%s)", start_iso, end_iso
            )
            return []
    return [r for r in rows if isinstance(r, dict)]


def load_item_rows(supabase, daily_ids: list) -> list[dict]:
    """``sales_items`` rows for the given sales_daily ids (chunked so a month
    of shifts never builds one oversized IN clause). ``[]`` on failure."""
    out: list[dict] = []
    ids = [i for i in (daily_ids or []) if i is not None]
    try:
        for start in range(0, len(ids), 200):
            chunk = ids[start:start + 200]
            resp = (
                supabase.table(_SALES_ITEMS_TABLE)
                .select("sales_daily_id, item_name, qty")
                .in_("sales_daily_id", chunk)
                .execute()
            )
            out.extend(r for r in (getattr(resp, "data", None) or []) if isinstance(r, dict))
    except Exception:
        logger.exception("slow items: sales_items read failed")
        return []
    return out


# --- folding (pure) ----------------------------------------------------------

def fold_item_days(shift_rows: list[dict], item_rows: list[dict], outlet_code: str) -> dict:
    """``{business_date_iso: {group_key: {'label', 'qty'}}}`` for one outlet,
    summing every shift that folded onto the day (day + overnight = the full
    24h). Matching bridges the kitchen/POS code forms via ``outlets_match``.
    Days appear ONLY when the outlet has at least one shift row — so an absent
    day means "no data", while a present day with a missing group means the
    group genuinely sold 0. Never raises."""
    try:
        from kitchen_usage import outlets_match

        day_by_id: dict = {}
        for row in shift_rows or []:
            if not isinstance(row, dict):
                continue
            day = str(row.get("shift_business_date") or "").strip()
            if not day:
                continue
            code = row.get("outlet_code") or row.get("outlet_canonical")
            if not outlets_match(code, outlet_code):
                continue
            day_by_id[row.get("id")] = day

        out: dict[str, dict] = {d: {} for d in day_by_id.values()}
        for row in item_rows or []:
            if not isinstance(row, dict):
                continue
            day = day_by_id.get(row.get("sales_daily_id"))
            if day is None:
                continue
            key = _norm_group(row.get("item_name"))
            if key is None or key in _IGNORED_GROUPS:
                continue
            qty = _to_float(row.get("qty"))
            if qty is None:
                continue
            bucket = out[day].setdefault(key, {"label": key, "qty": 0.0})
            bucket["qty"] += qty
        return out
    except Exception:
        logger.exception("slow items: fold failed (outlet=%s)", outlet_code)
        return {}


# --- evaluation (pure) -------------------------------------------------------

def _is_slow(qty: float, median: float) -> bool:
    return (
        median >= _MIN_MEDIAN_QTY
        and qty <= _SLOW_FACTOR * median
        and median - qty >= _MIN_DROP_QTY
    )


def evaluate_day(target_iso: str, day_map: dict) -> list[dict]:
    """The slow-item flags for one outlet's completed business day.

    ``day_map`` is ``fold_item_days`` output. For every group seen in the
    baseline window, today's qty (0 when absent) is compared against the
    group's median over the trailing data days. Slow groups also get their
    consecutive-slow-day streak (over data days, most recent first) so a
    3-day slump escalates to the quality question. Pure; never raises."""
    try:
        base_days = sorted(d for d in (day_map or {}) if d < target_iso)[-_BASELINE_DAYS:]
        if len(base_days) < _MIN_BASELINE_DAYS or target_iso not in (day_map or {}):
            return []
        today = day_map[target_iso]

        groups: set = set()
        for d in base_days:
            groups.update((day_map.get(d) or {}).keys())

        # Most recent data days first (today, then backwards) for streaks.
        recent = [target_iso] + list(reversed(base_days))

        flags = []
        for key in groups:
            series = [float((day_map[d].get(key) or {}).get("qty") or 0.0) for d in base_days]
            median = statistics.median(series)
            qty = float((today.get(key) or {}).get("qty") or 0.0)
            if not _is_slow(qty, median):
                continue
            streak = 0
            for d in recent:
                q = float(((day_map.get(d) or {}).get(key) or {}).get("qty") or 0.0)
                if _is_slow(q, median):
                    streak += 1
                else:
                    break
            label = (today.get(key) or {}).get("label")
            if not label:
                for d in reversed(base_days):
                    label = ((day_map.get(d) or {}).get(key) or {}).get("label")
                    if label:
                        break
            flags.append({
                "group": key,
                "label": label or key,
                "qty": qty,
                "median": median,
                "drop_pct": round((median - qty) / median * 100.0) if median else 0,
                "streak": streak,
            })
        flags.sort(key=lambda f: -(f["median"] - f["qty"]))
        return flags[:_MAX_FLAGS_PER_SHOP]
    except Exception:
        logger.exception("slow items: day evaluation failed")
        return []


# --- messages ----------------------------------------------------------------

def _fmt_qty(value: float) -> str:
    rounded = round(float(value))
    return f"{rounded:,}"


def format_manager_slow_items(entry: dict) -> str:
    """Tamil note to the outlet manager: which items sold under the shop's own
    usual yesterday (full 24h spelled out), push suggestions for today, and —
    for items slow ``_STREAK_DAYS`` days running — the quality question (taste
    okay? checked with the pandari? added what's needed?). ``""`` on malformed
    input; never raises."""
    try:
        if not isinstance(entry, dict):
            return ""
        outlet = str(entry.get("display") or entry.get("outlet_code") or "").strip()
        flags = entry.get("flags") or []
        if not outlet or not flags:
            return ""
        day = str(entry.get("business_date") or "")

        lines = [
            f"📉 Jualan item kurang — {outlet} • {day}",
            "",
            "நேத்து FULL business day கணக்கு "
            "(day shift + night shift, 24 மணி நேரம் சேர்த்து):",
            "",
        ]
        for f in flags:
            lines.append(
                f"• {f['label']}: {_fmt_qty(f['qty'])} தான் போச்சு — "
                f"வழக்கமா ~{_fmt_qty(f['median'])} போகும் (-{f['drop_pct']}%)"
            )
        lines += [
            "",
            "இன்னைக்கு இந்த items-அ push பண்ணுங்க 👉 customer-கிட்ட "
            "recommend பண்ணச் சொல்லுங்க, fresh-ஆ ready வெச்சிருங்க, "
            "counter/board-ல நல்லா தெரியற மாதிரி வைங்க.",
        ]
        streaked = [f for f in flags if f.get("streak", 0) >= _STREAK_DAYS]
        if streaked:
            names = ", ".join(f["label"] for f in streaked)
            days = max(f["streak"] for f in streaked)
            lines += [
                "",
                f"⚠️ {names}: {days} நாள் தொடர்ந்து குறைவா போகுது. "
                "Promotion problem மட்டும் இல்ல —",
                "taste/quality-ல issue இருக்கான்னு பாருங்க: பண்டாரிகிட்ட "
                "கேளுங்க — recipe மாறிடுச்சா, fresh-ஆ இருக்கா, portion "
                "சரியா இருக்கா. என்ன தேவைன்னா add பண்ணி, சரி பண்ணுங்க.",
            ]
        lines += [
            "",
            "என்ன காரணம்னு நினைக்கிறீங்க? சொல்லுங்க. 🙏",
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("slow items: manager message failed")
        return ""


def format_owner_summary(entries: list[dict]) -> str:
    """English overview for the alert group. ``""`` when nothing flagged."""
    try:
        if not entries:
            return ""
        lines = ["📉 Slow sellers (yesterday's full 24h business day):"]
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                parts = []
                for f in (e.get("flags") or []):
                    part = (
                        f"{f['label']} {_fmt_qty(f['qty'])} vs ~{_fmt_qty(f['median'])} usual"
                    )
                    if f.get("streak", 0) >= _STREAK_DAYS:
                        part += f" ({f['streak']}-day slump)"
                    parts.append(part)
                if parts:
                    lines.append(
                        f"• {e.get('display') or e.get('outlet_code')}: " + "; ".join(parts)
                    )
            except Exception:
                continue
        if len(lines) == 1:
            return ""
        lines.append(
            "Managers asked in Tamil to push these today; multi-day slumps "
            "get the taste/quality question."
        )
        return "\n".join(lines)
    except Exception:
        logger.exception("slow items: owner summary failed")
        return ""


# --- end-to-end --------------------------------------------------------------

def _both_shifts_in(supabase, outlet_code, business_date_iso) -> bool:
    """Both shift-close emails of the 24h day ingested? (S-file completeness
    only — this watch reads sales_items, so a late D-file must not block it.)"""
    try:
        from kitchen_usage import pos_shift_coverage

        cov = pos_shift_coverage(supabase, outlet_code, business_date_iso)
        return bool(cov.get("has_day") and cov.get("has_overnight"))
    except Exception:
        logger.warning(
            "slow items: shift coverage check failed for %s %s",
            outlet_code, business_date_iso, exc_info=True,
        )
        return False


def gather_slow_item_flags(supabase, today: date | None = None) -> dict:
    """End-to-end daily run. Returns ``{'business_date', 'entries',
    'skipped_incomplete'}`` where entries carry outlet_code/display/flags for
    FLAGGED outlets only. Outlets whose 24h day is not complete (both shift
    emails in) are skipped — never judged on a half day — and counted. Never
    raises."""
    result = {"business_date": "", "entries": [], "skipped_incomplete": 0}
    try:
        from manager_registration import load_active_outlets

        target = target_business_day(today)
        target_iso = target.isoformat()
        result["business_date"] = target_iso
        base_start = (target - timedelta(days=_BASELINE_DAYS)).isoformat()

        shift_rows = load_shift_rows(supabase, base_start, target_iso)
        if not shift_rows:
            return result
        item_rows = load_item_rows(supabase, [r.get("id") for r in shift_rows])
        if not item_rows:
            return result

        for outlet in load_active_outlets(supabase):
            try:
                if not _both_shifts_in(supabase, outlet.code, target_iso):
                    result["skipped_incomplete"] += 1
                    logger.info(
                        "slow items: %s %s skipped — both shift emails not in yet",
                        outlet.code, target_iso,
                    )
                    continue
                day_map = fold_item_days(shift_rows, item_rows, outlet.code)
                flags = evaluate_day(target_iso, day_map)
                if flags:
                    result["entries"].append({
                        "outlet_code": outlet.code,
                        "display": outlet.display,
                        "business_date": target_iso,
                        "flags": flags,
                    })
            except Exception:
                logger.exception(
                    "slow items: per-outlet failure (%s) — skipping", outlet.code
                )
                continue
        return result
    except Exception:
        logger.exception("slow items: gather failed")
        return result
