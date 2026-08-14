"""Cook-to-demand forecast — how much each kitchen should cook TODAY.

Every wastage alert this bot sends ends the same way: *"sales பாத்து அளவா
masak பண்ணுங்க"* — cook to the sales. Nobody has ever been given the number.
This module produces it: for each outlet and each tracked kitchen item, the
demand expected for today's business day, and the quantity to cook to meet it
without leaving a tray of ayam behind at 02:00.

Where the history comes from
----------------------------
``kitchen_daily_usage`` already holds, per (outlet, business_date, item):
Cooked (18:00 form), Left (02:00 form), Used = Cooked − Left, and — once the
24h POS day is complete — ``pos_qty``, the dishes actually sold.

**Demand is always Used (Cooked − Left).** The first cut of this module
preferred ``pos_qty`` — customers bought it, so surely that is demand — and the
production backtest (120 days, ~1250 item-days) showed why that is wrong:

  * ``pos_qty`` is present on only **46 %** of rows, so a series built on
    "POS when available, else Used" silently alternates between two different
    signals, and the median of that mixture measures neither.
  * the two are not even close to the same scale. Measured median Used ÷ median
    POS per item: ayam_goreng **3.6x**, ayam_kicap **5.0x**, ikan_goreng
    **5.0x**, daging **5.6x**, telur_ikan **13.4x**. That is by design, not by
    error: ``kitchen_usage`` counts only dishes that map cleanly to a whole cut
    (``AYAM_EXCLUDE_SUBSTRINGS`` drops rendang/kurma/isi-ayam noodle and rice
    dishes; Thai-category and staff meals are excluded outright) because the
    Guna-vs-POS flag compares like with like on both sides. A deliberately
    conservative subset is the right input for a mismatch gate and the wrong
    one for an absolute level — forecasting on it told shops to cook a third
    of what they need.

Used carries its own bias (over-portioning and leakage ride along with it), but
it is present on every keyed-in day, it is on the same scale as the number the
chef is being asked to change, and it excludes exactly the leftovers this
feature exists to remove. ``pos_qty`` is still carried on each point for
diagnostics; it never enters the model. Telur Ikan needs no special case any
more — its ``pos_qty`` holds kg PURCHASED rather than sold, and nothing reads
it.

Censored days (the part that matters)
-------------------------------------
A day that ended with ``left_qty = 0`` is a **sell-out**: demand was at least
what was cooked, possibly much more, and every later customer who asked for
that dish was turned away. Treating a sell-out as ordinary demand teaches the
model to keep under-cooking exactly the dishes that sell best. Sell-out days
are therefore flagged and lifted by ``_SELLOUT_UPLIFT`` before they enter the
level, and a shop that keeps selling out gets extra safety buffer on top.

The model
---------
Deliberately small, robust and explainable — a mamak kitchen has ~30-60 days of
history per item, plenty of key-in noise, and any number the chef cannot argue
with is a number the chef ignores::

    forecast  = level x dow_factor x trend
    recommend = round(forecast x (1 + safety))

  * **level** — median of the trailing ``_LEVEL_DAYS`` demands (median, not
    mean: one 300-pc key-in typo must not move tomorrow's plan).
  * **dow_factor** — that weekday's median vs the level. Friday and Saturday
    genuinely run 20-40 % above a Tuesday. Shrunk toward 1.0 by sample count
    (``n / (n + k)``) so two observed Mondays never swing the plan, and
    clamped so a freak week cannot double it.
  * **trend** — recent window median vs the window before it, clamped to
    ±15 %: enough to follow a shop that is genuinely growing or fading,
    not enough to chase noise.
  * **safety** — a buffer, not a fudge: base + volatility (median absolute
    deviation relative to the level) + a bonus per recent sell-out, capped.
    Steady items get ~5 %, erratic or sell-out-prone items get up to 25 %.

Two guards run before any of it. Numpad fat-fingers are dropped
(``reject_outliers``) — production carries cooked entries of 2000 pcs, 3500 kg
and 10900 kg of kambing, which survive a median but wreck the thin per-weekday
samples and every surplus figure the plan quotes. And an item whose level is
under the volume floor gets no plan at all: most tracked items move 3-6 a day,
where a single portion swings the error by a third, so only the items that
genuinely move (ayam goreng, ayam bawang) are worth a number.

Then each item is compared with what the shop has actually been cooking
(``usual_cooked``, the recent median) and gets one action:

  * **CUT**   — cooking clearly above what the day needs (dual gate: % AND
    absolute), with the daily surplus spelled out.
  * **RAISE** — sold out ``_RAISE_SELLOUT_DAYS`` times recently and the plan
    is above the usual cook: lost sales, not wastage.
  * **HOLD**  — already about right; say so, so "no change" is a result.

Every forecast is written to ``kitchen_demand_forecast`` and scored the next
morning against what the day actually demanded, so /forecast_accuracy can
answer "should we believe this?" with measured numbers instead of a promise.

Hard rule (same as the rest of the reporting layer): nothing here ever raises —
every entry point swallows exceptions and returns a safe default, so a bad
forecast can never block the kitchen forms or the digest.
"""
from __future__ import annotations

import logging
import os
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

USAGE_TABLE = "kitchen_daily_usage"
FORECAST_TABLE = "kitchen_demand_forecast"

# Master kill-switch for the SCHEDULED 11:00 plan, same shape as
# ``kitchen_usage.kitchen_log_enabled``. Default OFF: a forecast that has not
# been backtested against the shops' own numbers must never reach a kitchen,
# and the first production backtest of this module found a median error of
# 23-53 %. ``/cook_plan_now`` still works for the owner regardless, so the plan
# can be previewed and re-measured before anyone acts on it.
_ENABLED_TRUTHY = {"1", "true", "yes", "on", "y"}


def cook_plan_enabled() -> bool:
    """True only when COOK_PLAN_ENABLED is explicitly set truthy. Default OFF."""
    return os.environ.get("COOK_PLAN_ENABLED", "").strip().lower() in _ENABLED_TRUTHY

# --- windows -----------------------------------------------------------------
_HISTORY_DAYS = 56        # how far back we read (2 x the level window + slack)
_LEVEL_DAYS = 28          # the robust level is the median over this window
MIN_DATA_DAYS = 10        # fewer data days than this -> the item is not forecast
_RECENT_DAYS = 14         # "what the shop has been cooking / wasting lately"

# --- day-of-week seasonality -------------------------------------------------
_DOW_SHRINK_K = 2.0       # n/(n+k): 2 observations of a weekday count half
_DOW_CLAMP = (0.70, 1.40)
_MIN_DOW_SAMPLES = 2

# --- trend -------------------------------------------------------------------
_TREND_WINDOW = 14        # recent 14 days vs the 14 before them
_MIN_TREND_SAMPLES = 5    # data days needed on BOTH sides, else trend = 1.0
_TREND_CLAMP = (0.85, 1.15)

# --- censoring + safety ------------------------------------------------------
_SELLOUT_UPLIFT = 0.10    # a sold-out day's demand was at least ~10% higher
_BASE_SAFETY = 0.05
_VOLATILITY_K = 0.50      # safety grows with relative MAD
_SELLOUT_SAFETY_PER_DAY = 0.03
_MAX_SAFETY = 0.25

# --- outlier rejection -------------------------------------------------------
# Production has cooked_qty values of 2000 pcs, 3500 kg and 10900 kg sitting in
# the kitchen log — fat-fingered numpad entries, not days. The median survives a
# few, but they poison the day-of-week medians (thin per-weekday samples) and
# every "surplus" number the plan quotes. A point is dropped when it exceeds
# BOTH a multiple of the item's median AND an absolute margin above it, so a
# genuine festival day on a small item is kept while 10900 kg of kambing is not.
_OUTLIER_FACTOR = 6.0
_OUTLIER_MIN_ABS = 20.0

# --- minimum volume ----------------------------------------------------------
# "Cook ~3 kg daging" is not advice, it is noise: at that size a single portion
# swings the percentage error by a third. Same reasoning as
# ``item_sales_watch._MIN_MEDIAN_QTY`` — an item must genuinely move before the
# shop is told anything about it.
_MIN_LEVEL_PCS = 10.0
_MIN_LEVEL_KG = 3.0        # daging's measured 2.0 kg/day is below the line

# --- advice gates (dual, mamak-tuned — same shape as the mismatch gates) ------
_CUT_PCT_GATE = 10.0
_CUT_ABS_GATE_PCS = 5.0
_CUT_ABS_GATE_KG = 1.0
_RAISE_SELLOUT_DAYS = 2   # sell-outs within _RECENT_DAYS that justify RAISE

_MAX_ITEMS_PER_PLAN = 8   # a wall of items helps nobody; biggest gaps first

_CONFIDENCE_HIGH_SAMPLES = 21
_CONFIDENCE_MED_SAMPLES = 14
_CONFIDENCE_HIGH_MAD = 0.25


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def target_cook_day(today: date | None = None) -> date:
    """The business day being planned: TODAY.

    ``kitchen_daily_usage.business_date`` is the 18:00 COOKED date, and the
    plan is posted in the late morning — before anything goes in the pot. By
    then yesterday's row is complete (Cooked keyed at 18:00, Left at 02:00), so
    the freshest possible history feeds today's number."""
    return today or date.today()


def _round_qty(value: float, unit: str) -> float:
    """Round a recommendation to a quantity a kitchen can actually act on.

    pcs are whole birds/pieces (and to the nearest 5 once the number is big
    enough that single pieces are noise); kg go to the nearest half kilo, which
    is how the scale is read at the bench."""
    if value <= 0:
        return 0.0
    if unit == "kg":
        return round(round(value * 2.0) / 2.0, 1)
    if value >= 50:
        return float(int(round(value / 5.0) * 5))
    return float(int(round(value)))


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


# --- loading -----------------------------------------------------------------

def load_usage_rows(supabase, start_iso: str, end_iso: str) -> list[dict]:
    """``kitchen_daily_usage`` rows across every outlet for a date range.

    One paged read serves every outlet's forecast — ten outlets x eleven items
    x 56 days is comfortably past the PostgREST 1000-row cap, so this must page
    (``db_pagination.fetch_all_pages``) or shops would silently forecast off a
    truncated slice. ``[]`` on failure, never raises."""

    def _build():
        return (
            supabase.table(USAGE_TABLE)
            .select(
                "id, outlet_code, business_date, item_code, "
                "cooked_qty, left_qty, used_qty, pos_qty"
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
                "cook plan: kitchen_daily_usage read failed (%s..%s)",
                start_iso, end_iso,
            )
            return []
    return [r for r in rows if isinstance(r, dict)]


# --- demand extraction (pure) ------------------------------------------------

def day_demand(row: dict, item_code: str) -> dict | None:
    """The demand signal carried by one ``kitchen_daily_usage`` row.

    Returns ``{'demand', 'censored', 'cooked', 'left', 'pos', 'source'}`` or
    ``None`` when the row carries no usable demand. Demand is **always** Used
    (Cooked − Left) — see the module docstring for the production measurement
    that ruled POS out as a level. ``pos`` is carried for diagnostics only and
    never enters the model. ``censored`` marks a sell-out (Left = 0) — demand
    was AT LEAST this."""
    try:
        cooked = _to_float(row.get("cooked_qty"))
        left = _to_float(row.get("left_qty"))
        used = _to_float(row.get("used_qty"))
        if used is None and cooked is not None and left is not None:
            used = cooked - left

        # Left keyed in above Cooked is a key-in error, not negative demand
        # (production: kambing's median Left, 4.5, exceeds its median Cooked).
        if used is None or used < 0:
            return None
        demand, source = used, "used"
        return {
            "demand": float(demand),
            "censored": left is not None and left <= 0 and (cooked or 0) > 0,
            "cooked": cooked,
            "left": left,
            "pos": _to_float(row.get("pos_qty")),   # diagnostics only
            "source": source,
        }
    except Exception:
        logger.exception("cook plan: demand extraction failed (%s)", item_code)
        return None


def demand_series(rows: list[dict], outlet_code: str) -> dict[str, dict[str, dict]]:
    """``{item_code: {business_date_iso: demand_point}}`` for ONE outlet.

    Outlet matching bridges the kitchen/POS code forms via
    ``kitchen_usage.outlets_match``. Days with no usable demand simply do not
    appear — an absent day means "no data", never "sold nothing". Never
    raises."""
    out: dict[str, dict[str, dict]] = {}
    try:
        from kitchen_usage import outlets_match

        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if not outlets_match(row.get("outlet_code"), outlet_code):
                continue
            day = str(row.get("business_date") or "").strip()
            item = str(row.get("item_code") or "").strip()
            if not day or not item:
                continue
            point = day_demand(row, item)
            if point is None:
                continue
            out.setdefault(item, {})[day] = point
        return out
    except Exception:
        logger.exception("cook plan: series build failed (outlet=%s)", outlet_code)
        return {}


# --- the model (pure) --------------------------------------------------------

def reject_outliers(points: list[tuple[date, dict]]) -> list[tuple[date, dict]]:
    """Drop numpad fat-fingers before they reach the model.

    Two-pass: take the median of the raw demands, then keep every point at or
    below ``max(factor × median, median + margin)``. The margin keeps the filter
    from turning brutal on small items — kambing's median of 3 kg would
    otherwise cap at 18 kg and throw away a real 20 kg day, while 10900 kg is
    still dropped either way. Returns the points unchanged when there is
    nothing to judge against. Pure; never raises."""
    try:
        if len(points) < 3:
            return list(points)
        values = [float(p.get("demand") or 0.0) for _, p in points]
        median = _median(values)
        if median <= 0:
            return list(points)
        ceiling = max(_OUTLIER_FACTOR * median, median + _OUTLIER_MIN_ABS)
        return [(d, p) for d, p in points
                if float(p.get("demand") or 0.0) <= ceiling]
    except Exception:
        logger.exception("cook plan: outlier rejection failed")
        return list(points)


def _min_level(unit: str) -> float:
    return _MIN_LEVEL_KG if unit == "kg" else _MIN_LEVEL_PCS


def _adjusted(point: dict) -> float:
    """A sell-out day's observed demand understates the truth — lift it before
    it feeds the level, or the model learns to keep the shop dry."""
    value = float(point.get("demand") or 0.0)
    return value * (1.0 + _SELLOUT_UPLIFT) if point.get("censored") else value


def dow_factor(points: list[tuple[date, dict]], weekday: int, level: float) -> float:
    """Weekday seasonality multiplier, shrunk toward 1.0 by sample count.

    Two observed Fridays are not evidence of a Friday pattern; ten are. The
    shrink ``n / (n + k)`` moves smoothly between the two, and the clamp stops
    a single festival week from doubling the plan. Returns 1.0 when there is
    nothing to learn from."""
    if level <= 0:
        return 1.0
    same = [_adjusted(p) for d, p in points if d.weekday() == weekday]
    if len(same) < _MIN_DOW_SAMPLES:
        return 1.0
    raw = _median(same) / level
    shrink = len(same) / (len(same) + _DOW_SHRINK_K)
    return _clamp(1.0 + (raw - 1.0) * shrink, *_DOW_CLAMP)


def trend_factor(points: list[tuple[date, dict]]) -> float:
    """Recent-window median vs the window before it, clamped to ±15 %.

    Enough to follow a shop that is genuinely growing or fading; not enough to
    chase a quiet week into under-cooking the next one. 1.0 when either side is
    too thin to compare."""
    if not points:
        return 1.0
    ordered = sorted(points, key=lambda t: t[0])
    recent = [_adjusted(p) for _, p in ordered[-_TREND_WINDOW:]]
    prior = [_adjusted(p) for _, p in ordered[-2 * _TREND_WINDOW:-_TREND_WINDOW]]
    if len(recent) < _MIN_TREND_SAMPLES or len(prior) < _MIN_TREND_SAMPLES:
        return 1.0
    prior_median = _median(prior)
    if prior_median <= 0:
        return 1.0
    return _clamp(_median(recent) / prior_median, *_TREND_CLAMP)


def safety_pct(points: list[tuple[date, dict]], level: float, sellouts: int) -> float:
    """The buffer on top of the expected demand.

    Base, plus volatility (median absolute deviation relative to the level — an
    item that swings 40 % day to day needs more headroom than one that never
    moves), plus a bonus per recent sell-out. Capped so "safety" can never
    become a licence to over-cook."""
    if level <= 0:
        return _BASE_SAFETY
    values = [_adjusted(p) for _, p in points]
    mad = _median([abs(v - level) for v in values]) if values else 0.0
    rel_mad = mad / level
    pct = (
        _BASE_SAFETY
        + _VOLATILITY_K * rel_mad
        + _SELLOUT_SAFETY_PER_DAY * max(0, sellouts)
    )
    return round(_clamp(pct, _BASE_SAFETY, _MAX_SAFETY), 3)


def _confidence(samples: int, rel_mad: float) -> str:
    if samples >= _CONFIDENCE_HIGH_SAMPLES and rel_mad <= _CONFIDENCE_HIGH_MAD:
        return "high"
    if samples >= _CONFIDENCE_MED_SAMPLES:
        return "medium"
    return "low"


def _cut_gate(unit: str) -> float:
    return _CUT_ABS_GATE_KG if unit == "kg" else _CUT_ABS_GATE_PCS


def decide_action(recommend: float, usual_cooked: float | None, sellouts: int,
                  unit: str) -> str:
    """CUT / RAISE / HOLD for one item — symmetric around what the shop cooks.

    RAISE means "cook more than usual", CUT means "cook less", HOLD means "the
    usual amount is about right". Both directions need BOTH gates (% and
    absolute) so a shop two pieces off its plan is left alone; sell-out
    evidence raises on its own, because a shop that keeps running dry is losing
    sales whatever the gates say.

    Recent sell-outs also VETO a cut: telling a kitchen to cook less in a week
    it kept running out is the fastest way to make it stop reading."""
    if usual_cooked is None or usual_cooked <= 0:
        return "HOLD"
    diff = recommend - usual_cooked
    gate = _cut_gate(unit)
    if diff > 0:
        if sellouts >= _RAISE_SELLOUT_DAYS:
            return "RAISE"
        pct = diff / usual_cooked * 100.0
        return "RAISE" if pct > _CUT_PCT_GATE and diff > gate else "HOLD"
    if sellouts >= _RAISE_SELLOUT_DAYS:
        return "HOLD"
    surplus = -diff
    if surplus <= 0:
        return "HOLD"
    pct = surplus / usual_cooked * 100.0
    return "CUT" if pct > _CUT_PCT_GATE and surplus > gate else "HOLD"


def _reason_for(action: str, sellouts: int, dow: float, trend: float) -> str:
    """Why an item got its action — drives which explanation the kitchen reads.

    A raise off the back of repeated sell-outs is a different conversation from
    a raise because today is a Friday, and the message has to say which."""
    if action == "CUT":
        return "surplus"
    if action != "RAISE":
        return "steady"
    if sellouts >= _RAISE_SELLOUT_DAYS:
        return "sellout"
    return "busy_day" if dow >= 1.05 else "trend"


def forecast_item(item_code: str, day_points: dict[str, dict],
                  target: date) -> dict | None:
    """The full forecast for one (outlet, item), or ``None`` when the item has
    too little history to be worth a number.

    ``day_points`` is one item's slice of ``demand_series``. Only days strictly
    BEFORE ``target`` are used — a plan for today can never peek at today.
    Pure; never raises."""
    try:
        from kitchen_usage import ITEM_BY_CODE

        meta = ITEM_BY_CODE.get(item_code)
        if not meta:
            return None
        unit = meta.get("unit", "pcs")

        points: list[tuple[date, dict]] = []
        for iso, point in (day_points or {}).items():
            try:
                d = date.fromisoformat(str(iso))
            except ValueError:
                continue
            if d >= target:
                continue
            points.append((d, point))
        points.sort(key=lambda t: t[0])
        # Fat-fingered entries out first: they distort the level, the weekday
        # medians and every surplus number the plan quotes.
        points = reject_outliers(points)
        if len(points) < MIN_DATA_DAYS:
            return None

        level_points = points[-_LEVEL_DAYS:]
        values = [_adjusted(p) for _, p in level_points]
        level = _median(values)
        if level <= 0:
            return None
        # Below the floor the item does not move enough for a plan to mean
        # anything — one portion would swing it by a third.
        if level < _min_level(unit):
            return None

        dow = dow_factor(level_points, target.weekday(), level)
        trend = trend_factor(points)
        forecast = level * dow * trend

        recent = points[-_RECENT_DAYS:]
        sellouts = sum(1 for _, p in recent if p.get("censored"))
        safety = safety_pct(level_points, level, sellouts)
        recommend = _round_qty(forecast * (1.0 + safety), unit)

        cooked_recent = [
            float(p["cooked"]) for _, p in recent if p.get("cooked") is not None
        ]
        usual_cooked = _median(cooked_recent) if cooked_recent else None
        left_recent = [
            float(p["left"]) for _, p in recent if p.get("left") is not None
        ]
        usual_left = _median(left_recent) if left_recent else None

        mad = _median([abs(v - level) for v in values])
        rel_mad = mad / level if level else 0.0
        action = decide_action(recommend, usual_cooked, sellouts, unit)
        reason = _reason_for(action, sellouts, dow, trend)

        return {
            "code": item_code,
            "label": meta.get("label", item_code),
            "unit": unit,
            "level": round(level, 2),
            "dow_factor": round(dow, 3),
            "trend": round(trend, 3),
            "forecast": round(forecast, 2),
            "safety": safety,
            "recommend": recommend,
            "usual_cooked": usual_cooked,
            "usual_left": usual_left,
            "sellouts": sellouts,
            "samples": len(points),
            "confidence": _confidence(len(points), rel_mad),
            "action": action,
            "reason": reason,
            "day_name": _DAY_NAME_MY.get(target.weekday(), ""),
            "surplus": (
                round(usual_cooked - recommend, 2)
                if usual_cooked is not None else None
            ),
            "source": recent[-1][1].get("source") if recent else "used",
        }
    except Exception:
        logger.exception("cook plan: item forecast failed (%s)", item_code)
        return None


def evaluate_outlet(series: dict[str, dict[str, dict]], outlet_code: str,
                    target: date) -> list[dict]:
    """Every forecastable item for one outlet, ordered by how much the plan
    differs from what the shop currently cooks (biggest correction first, then
    the HOLDs). Items the outlet does not carry are skipped. Never raises."""
    try:
        from kitchen_usage import items_for_outlet

        out = []
        for item in items_for_outlet(outlet_code):
            fc = forecast_item(item["code"], series.get(item["code"], {}), target)
            if fc is not None:
                out.append(fc)

        def _gap(fc: dict) -> float:
            usual = fc.get("usual_cooked")
            if usual is None or not usual:
                return 0.0
            return abs(usual - fc["recommend"]) / usual

        out.sort(key=lambda fc: (fc["action"] == "HOLD", -_gap(fc)))
        return out[:_MAX_ITEMS_PER_PLAN]
    except Exception:
        logger.exception("cook plan: outlet evaluation failed (%s)", outlet_code)
        return []


# --- messages ----------------------------------------------------------------

_DAY_NAME_MY = {
    0: "Isnin", 1: "Selasa", 2: "Rabu", 3: "Khamis",
    4: "Jumaat", 5: "Sabtu", 6: "Ahad",
}


def _fmt(value, unit: str) -> str:
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if unit == "pcs":
        return str(int(round(num)))
    return f"{num:g}"


def _plan_line(fc: dict) -> str:
    """One item's line: the number to cook, then the one-line reason for it.

    The reason has to match the number — an earlier cut told a shop to cook 190
    against its usual 160 and then said "your current amount is right"."""
    unit = fc["unit"]
    head = f"• {fc['label']}: masak ~{_fmt(fc['recommend'], unit)} {unit}"
    usual = fc.get("usual_cooked")
    usual_txt = f"~{_fmt(usual, unit)} {unit}" if usual is not None else ""
    reason = fc.get("reason") or "steady"

    if fc["action"] == "CUT" and usual is not None:
        return (
            f"{head}  ⬇️\n"
            f"   இப்ப {usual_txt} masak பண்றீங்க — "
            f"~{_fmt(fc.get('surplus'), unit)} {unit} அதிகம், அது தான் "
            f"தினமும் மிச்சம் ஆகுது."
        )
    if fc["action"] == "RAISE":
        if reason == "sellout":
            note = (
                f"{head}  ⬆️\n   கடந்த {_RECENT_DAYS} நாள்ல {fc['sellouts']} நாள் "
                "முழுசா தீந்து போச்சு — customer கேட்டும் குடுக்க முடியல."
            )
            if usual:
                note += f" இப்ப {usual_txt} தான் masak ஆகுது."
            return note
        if reason == "busy_day":
            day = fc.get("day_name") or "இந்த நாள்"
            return (
                f"{head}  ⬆️\n"
                f"   {day} வழக்கமா busy — உங்க usual {usual_txt}-அ விட "
                "கொஞ்சம் கூட்டி வெச்சுக்குங்க."
            )
        return (
            f"{head}  ⬆️\n"
            f"   கடந்த சில வாரமா sales ஏறிட்டு வருது — usual {usual_txt}-அ "
            "விட கொஞ்சம் அதிகம் தேவைப்படும்."
        )
    return f"{head}  ✅\n   இப்பயிருக்கிற அளவு சரியா இருக்கு, அப்படியே தொடருங்க."


def format_cook_plan(entry: dict) -> str:
    """The Malay-headed, Tamil-explained cook plan for one outlet's kitchen.

    Same register as the kitchen form and the wastage follow-up: Malay for the
    header and the trade words the crew reads on the form (masak, guna, POS),
    Tamil for the reasoning. ``""`` on malformed input; never raises."""
    try:
        if not isinstance(entry, dict):
            return ""
        outlet = str(entry.get("display") or entry.get("outlet_code") or "").strip()
        items = entry.get("items") or []
        if not outlet or not items:
            return ""
        day_iso = str(entry.get("business_date") or "")
        try:
            day_name = _DAY_NAME_MY[date.fromisoformat(day_iso).weekday()]
        except (ValueError, KeyError):
            day_name = ""
        header = f"🍳 Cadangan masak hari ini — {outlet} • {day_iso}"
        if day_name:
            header += f" ({day_name})"

        lines = [
            header,
            "",
            f"கடந்த {_LEVEL_DAYS} நாள் sales-அ வெச்சு, இன்னைக்கு "
            f"{day_name or 'இந்த நாள்'} எவ்வளவு போகும்னு பாத்து சொல்றேன். "
            "இது ஒரு guide — நீங்க பாத்து முடிவு பண்ணுங்க:",
            "",
        ]
        for fc in items:
            lines.append(_plan_line(fc))

        cuts = [f for f in items if f["action"] == "CUT"]
        raises = [f for f in items if f["action"] == "RAISE"]
        lines.append("")
        if cuts:
            lines.append(
                "⬇️ = தேவைக்கு மேல masak ஆகுது. மிச்சம் = wastage = கடைக்கு "
                "loss. கொஞ்சம் குறைச்சு பாருங்க."
            )
        if raises:
            lines.append(
                "⬆️ = போதலை, முன்னாடியே தீந்துடுது. கொஞ்சம் கூட்டி masak "
                "பண்ணுங்க — வித்த முடியாத sales தான் பெரிய loss."
            )
        lines += [
            "",
            "Number சரியில்லைனு தோணுதா? விழா, order, மழை — ஏதாவது "
            "இருந்தா சொல்லுங்க, அதுக்கேத்த மாதிரி பாக்கலாம். 🙏",
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("cook plan: manager message failed")
        return ""


def format_owner_summary(entries: list[dict]) -> str:
    """English overview for the alert group: per outlet, what was recommended
    up or down. ``""`` when no outlet has an actionable change."""
    try:
        if not entries:
            return ""
        lines = ["🍳 Cook-to-demand plan (today):"]
        body = 0
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                items = e.get("items") or []
                parts = []
                for fc in items:
                    # Only structural problems reach the owner: chronic
                    # over-cooking, and shops that keep running dry. A routine
                    # Friday uplift is the plan working, not news.
                    if fc["action"] == "HOLD":
                        continue
                    if fc["action"] == "RAISE" and fc.get("reason") != "sellout":
                        continue
                    arrow = "↓" if fc["action"] == "CUT" else "↑"
                    unit = fc["unit"]
                    part = (
                        f"{fc['label']} {arrow} {_fmt(fc['recommend'], unit)}{unit}"
                        f" (usual {_fmt(fc.get('usual_cooked'), unit)})"
                    )
                    if fc["action"] == "RAISE":
                        part += f", {fc['sellouts']} sell-outs/{_RECENT_DAYS}d"
                    parts.append(part)
                if parts:
                    body += 1
                    lines.append(
                        f"• {e.get('display') or e.get('outlet_code')}: "
                        + "; ".join(parts)
                    )
            except Exception:
                continue
        if not body:
            return ""
        lines.append(
            "Kitchens asked in Tamil to cook to these numbers; ↓ is wastage, "
            "↑ is lost sales. Accuracy: /forecast_accuracy"
        )
        return "\n".join(lines)
    except Exception:
        logger.exception("cook plan: owner summary failed")
        return ""


# --- persistence + scoring ---------------------------------------------------

def persist_forecasts(supabase, entry: dict) -> int:
    """Upsert one outlet's forecasts into ``kitchen_demand_forecast``.

    Advisory only: a failure (including the migration not being applied yet)
    is logged and swallowed — the plan still goes to the kitchen, it just
    isn't scored later. Returns the number of rows written."""
    try:
        outlet = entry.get("outlet_code")
        day = entry.get("business_date")
        items = entry.get("items") or []
        if not outlet or not day or not items:
            return 0
        payload = [
            {
                "outlet_code": outlet,
                "business_date": str(day),
                "item_code": fc["code"],
                "unit": fc["unit"],
                "forecast_qty": fc["forecast"],
                "recommend_qty": fc["recommend"],
                "safety_pct": fc["safety"],
                "dow_factor": fc["dow_factor"],
                "trend_factor": fc["trend"],
                "level_qty": fc["level"],
                "samples": fc["samples"],
                "confidence": fc["confidence"],
                "action": fc["action"],
                "usual_cooked": fc.get("usual_cooked"),
            }
            for fc in items
        ]
        (
            supabase.table(FORECAST_TABLE)
            .upsert(payload, on_conflict="outlet_code,business_date,item_code")
            .execute()
        )
        return len(payload)
    except Exception:
        logger.warning(
            "cook plan: forecast log write failed (outlet=%s) — plan still sent; "
            "is migration 0041 applied?",
            entry.get("outlet_code"), exc_info=True,
        )
        return 0


def score_previous_day(supabase, day: date) -> dict:
    """Fill ``actual_qty`` / errors on the forecasts made for ``day``.

    Run the morning after: yesterday's Cooked+Left are in and its POS day is
    reconciled, so the demand the forecast was aiming at is finally knowable.
    Returns ``{'scored', 'rows'}``; never raises."""
    result = {"scored": 0, "rows": 0}
    try:
        day_iso = day.isoformat()
        try:
            resp = (
                supabase.table(FORECAST_TABLE)
                .select("id, outlet_code, item_code, forecast_qty, actual_qty")
                .eq("business_date", day_iso)
                .execute()
            )
            forecasts = [r for r in (getattr(resp, "data", None) or [])
                         if isinstance(r, dict)]
        except Exception:
            logger.warning(
                "cook plan: forecast log read failed for %s — nothing to score",
                day_iso, exc_info=True,
            )
            return result
        pending = [f for f in forecasts if f.get("actual_qty") is None]
        result["rows"] = len(pending)
        if not pending:
            return result

        usage = load_usage_rows(supabase, day_iso, day_iso)
        # Match the outlet the same way the forecast did (``outlets_match``,
        # not string equality): the kitchen and POS code forms differ across
        # tables, and an exact-match join would silently leave those outlets
        # unscored forever.
        try:
            from kitchen_usage import outlets_match
        except Exception:
            def outlets_match(a, b):  # pragma: no cover - import guard
                return str(a or "").strip().upper() == str(b or "").strip().upper()

        by_item: dict[str, list[dict]] = {}
        for row in usage:
            item = str(row.get("item_code") or "")
            if item:
                by_item.setdefault(item, []).append(row)

        for f in pending:
            item_code = str(f.get("item_code"))
            row = next(
                (
                    r for r in by_item.get(item_code, [])
                    if outlets_match(r.get("outlet_code"), f.get("outlet_code"))
                ),
                None,
            )
            if row is None:
                continue
            point = day_demand(row, item_code)
            if point is None:
                continue
            actual = float(point["demand"])
            forecast = _to_float(f.get("forecast_qty"))
            update = {
                "actual_qty": actual,
                "sold_out": bool(point.get("censored")),
                "scored_at": _now_iso(),
            }
            if forecast is not None:
                abs_err = abs(actual - forecast)
                update["abs_error"] = round(abs_err, 2)
                update["pct_error"] = (
                    round(abs_err / actual * 100.0, 1) if actual > 0 else None
                )
            try:
                (
                    supabase.table(FORECAST_TABLE)
                    .update(update)
                    .eq("id", f.get("id"))
                    .execute()
                )
                result["scored"] += 1
            except Exception:
                logger.warning(
                    "cook plan: scoring update failed (id=%s)", f.get("id"),
                    exc_info=True,
                )
        return result
    except Exception:
        logger.exception("cook plan: scoring failed for %s", day)
        return result


def summarise_accuracy(rows: list[dict]) -> dict:
    """Aggregate scored forecast rows into the accuracy numbers (pure).

    Reports the MEDIAN absolute percentage error, not the mean: one item that
    sold 2 instead of 20 would otherwise drag a whole month's score. Sell-out
    days are counted separately — on those the forecast is measured against a
    censored actual, so being "wrong low" there is the honest reading."""
    out = {
        "n": 0, "median_pct_error": None, "within_10": 0, "within_20": 0,
        "sold_out": 0, "by_outlet": {},
    }
    try:
        errors: list[float] = []
        per_outlet: dict[str, list[float]] = {}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            pct = _to_float(r.get("pct_error"))
            if r.get("sold_out"):
                out["sold_out"] += 1
            if pct is None:
                continue
            out["n"] += 1
            errors.append(pct)
            per_outlet.setdefault(str(r.get("outlet_code") or "?"), []).append(pct)
            if pct <= 10.0:
                out["within_10"] += 1
            if pct <= 20.0:
                out["within_20"] += 1
        if errors:
            out["median_pct_error"] = round(_median(errors), 1)
        out["by_outlet"] = {
            code: {"n": len(vals), "median_pct_error": round(_median(vals), 1)}
            for code, vals in sorted(per_outlet.items())
        }
        return out
    except Exception:
        logger.exception("cook plan: accuracy summary failed")
        return out


def format_accuracy(stats: dict, days: int) -> str:
    """The /forecast_accuracy reply. ``""`` when nothing has been scored yet."""
    try:
        if not stats or not stats.get("n"):
            return (
                "📐 Cook-to-demand accuracy: nothing scored yet. Forecasts are "
                "scored the morning after the day they cover — give it a day."
            )
        n = stats["n"]
        lines = [
            f"📐 Cook-to-demand accuracy (last {days} days, {n} scored items)",
            "",
            f"Median error: {stats['median_pct_error']}%",
            f"Within 10%: {stats['within_10']}/{n} "
            f"({round(stats['within_10'] / n * 100)}%)",
            f"Within 20%: {stats['within_20']}/{n} "
            f"({round(stats['within_20'] / n * 100)}%)",
        ]
        if stats.get("sold_out"):
            lines.append(
                f"Sold-out days: {stats['sold_out']} — on those the real demand "
                "was higher than recorded, so the error shown is a floor."
            )
        by_outlet = stats.get("by_outlet") or {}
        if by_outlet:
            lines += ["", "Per outlet (median error):"]
            for code, s in sorted(
                by_outlet.items(), key=lambda kv: -kv[1]["median_pct_error"]
            ):
                lines.append(f"• {code}: {s['median_pct_error']}% ({s['n']} items)")
        return "\n".join(lines)
    except Exception:
        logger.exception("cook plan: accuracy formatting failed")
        return ""


def accuracy_report(supabase, days: int = 28, today: date | None = None) -> str:
    """Read the scored forecast log and render the accuracy report. Never
    raises — returns a plain explanation when the log can't be read."""
    try:
        end = (today or date.today())
        start = end - timedelta(days=days)
        try:
            resp = (
                supabase.table(FORECAST_TABLE)
                .select("outlet_code, item_code, pct_error, sold_out, business_date")
                .gte("business_date", start.isoformat())
                .lte("business_date", end.isoformat())
                .execute()
            )
            rows = [r for r in (getattr(resp, "data", None) or [])
                    if isinstance(r, dict)]
        except Exception:
            logger.warning("cook plan: accuracy read failed", exc_info=True)
            return (
                "📐 Cook-to-demand accuracy unavailable — the forecast log "
                "could not be read (is migration 0041 applied?)."
            )
        return format_accuracy(summarise_accuracy(rows), days)
    except Exception:
        logger.exception("cook plan: accuracy report failed")
        return ""


# --- end-to-end --------------------------------------------------------------

def gather_cook_plans(supabase, today: date | None = None) -> dict:
    """The daily run: score yesterday, then plan today for every active outlet.

    Returns ``{'business_date', 'entries', 'skipped_thin', 'scored'}`` where
    entries carry outlet_code / display / business_date / items for outlets
    with at least one forecastable item. Outlets whose history is too thin are
    counted, never guessed at. Never raises."""
    result = {"business_date": "", "entries": [], "skipped_thin": 0, "scored": 0}
    try:
        from manager_registration import load_active_outlets

        target = target_cook_day(today)
        result["business_date"] = target.isoformat()

        # Yesterday's forecasts can finally be marked right or wrong.
        try:
            result["scored"] = score_previous_day(
                supabase, target - timedelta(days=1)
            ).get("scored", 0)
        except Exception:
            logger.exception("cook plan: scoring pass failed — planning anyway")

        start = (target - timedelta(days=_HISTORY_DAYS)).isoformat()
        rows = load_usage_rows(supabase, start, target.isoformat())
        if not rows:
            return result

        for outlet in load_active_outlets(supabase):
            try:
                series = demand_series(rows, outlet.code)
                items = evaluate_outlet(series, outlet.code, target)
                if not items:
                    result["skipped_thin"] += 1
                    continue
                entry = {
                    "outlet_code": outlet.code,
                    "display": outlet.display,
                    "business_date": target.isoformat(),
                    "items": items,
                }
                persist_forecasts(supabase, entry)
                result["entries"].append(entry)
            except Exception:
                logger.exception(
                    "cook plan: per-outlet failure (%s) — skipping", outlet.code
                )
                continue
        return result
    except Exception:
        logger.exception("cook plan: gather failed")
        return result
