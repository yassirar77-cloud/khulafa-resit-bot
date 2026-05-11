"""Historical context and anomaly detection for receipt purchases.

Loads March 2026 outlet baselines once at import time and exposes lookup
helpers used by the receipt pipeline to flag unusually large purchases
against each outlet's monthly category spend.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).resolve().parent / "data" / "outlet_benchmarks.json"

_CRITICAL_PCT = 50.0
_HIGH_PCT = 30.0
_ELEVATED_PCT = 15.0


def _load() -> tuple[dict[str, dict[str, Any]], dict[str, float], dict[str, str]]:
    with _DATA_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    outlets: dict[str, dict[str, Any]] = raw["outlets"]
    group_avg: dict[str, float] = raw["group_avg_per_category_per_outlet"]
    code_lookup: dict[str, str] = {code.upper(): code for code in outlets}
    return outlets, group_avg, code_lookup


_OUTLETS, _GROUP_AVG, _CODE_LOOKUP = _load()


def _resolve_outlet_code(pos_outlet_code: Any) -> str | None:
    if not isinstance(pos_outlet_code, str):
        return None
    key = pos_outlet_code.strip().upper()
    if not key:
        return None
    return _CODE_LOOKUP.get(key)


def get_outlet_baseline(
    pos_outlet_code: str, canonical_category: str
) -> dict | None:
    """Return baseline data for an outlet+category, or None if missing."""
    code = _resolve_outlet_code(pos_outlet_code)
    if code is None:
        return None
    outlet = _OUTLETS[code]
    categories = outlet.get("categories", {})
    cat = categories.get(canonical_category)
    if cat is None:
        return None
    return {
        "outlet_display": outlet["display_name"],
        "march_total": float(cat["march_total"]),
        "vs_group_avg_pct": float(cat["vs_group_avg_pct"]),
        "group_avg_per_outlet": float(_GROUP_AVG.get(canonical_category, 0.0)),
    }


def _classify(monthly_pct_used: float) -> str:
    if monthly_pct_used >= _CRITICAL_PCT:
        return "critical"
    if monthly_pct_used >= _HIGH_PCT:
        return "high"
    if monthly_pct_used >= _ELEVATED_PCT:
        return "elevated"
    return "normal"


def _fmt_rm(amount: float) -> str:
    if amount == int(amount):
        return f"RM{int(amount)}"
    return f"RM{amount:.2f}"


_CATEGORY_QUESTIONS = {
    "gas": "Tank top up scheduled? Supplier change?",
}


def _question_for(canonical: str) -> str:
    return _CATEGORY_QUESTIONS.get(canonical, "Confirm large buy planned?")


def _format_short(
    canonical: str, current_amount: float, monthly_pct_used: float
) -> str:
    pct = int(round(monthly_pct_used))
    return (
        f"⚡ {canonical.upper()} {_fmt_rm(current_amount)} "
        f"({pct}% of monthly avg). Why big buy?"
    )


def _format_detail(
    canonical: str,
    current_amount: float,
    monthly_pct_used: float,
    baseline: dict,
) -> str:
    cat_upper = canonical.upper()
    pct_monthly = int(round(monthly_pct_used))
    vs_group = baseline["vs_group_avg_pct"]
    vs_group_abs = abs(int(round(vs_group)))
    outlet_display = baseline["outlet_display"]
    if vs_group > 0:
        comparison = (
            f"{outlet_display} spends {vs_group_abs}% more than group avg "
            f"on {cat_upper}"
        )
    elif vs_group < 0:
        comparison = (
            f"{outlet_display} spends {vs_group_abs}% less than group avg "
            f"on {cat_upper}"
        )
    else:
        comparison = f"{outlet_display} spends same as group avg on {cat_upper}"
    return (
        f"📊 {cat_upper} purchase analysis:\n"
        f"• Today: {_fmt_rm(current_amount)}\n"
        f"• March 2026 avg: {_fmt_rm(baseline['march_total'])} at {outlet_display}\n"
        f"• This is {pct_monthly}% of monthly budget in 1 buy\n"
        f"• Group avg: {_fmt_rm(baseline['group_avg_per_outlet'])} per outlet\n"
        f"• {comparison}\n"
        f"\n"
        f"Question: {_question_for(canonical)}"
    )


def _normal_result(baseline: dict | None) -> dict:
    return {
        "is_anomaly": False,
        "severity": "normal",
        "monthly_pct_used": 0.0,
        "vs_group_pct": float(baseline["vs_group_avg_pct"]) if baseline else 0.0,
        "message_short": "",
        "message_detail": "",
        "baseline": baseline,
    }


def detect_anomaly(
    outlet: str, canonical: str, current_amount: float
) -> dict:
    """Classify a purchase against the outlet's March baseline."""
    baseline = get_outlet_baseline(outlet, canonical)

    if baseline is None:
        return _normal_result(None)

    try:
        amount = float(current_amount)
    except (TypeError, ValueError):
        return _normal_result(baseline)

    if amount <= 0:
        return _normal_result(baseline)

    march_total = baseline["march_total"]
    if march_total <= 0:
        return _normal_result(baseline)

    monthly_pct_used = (amount / march_total) * 100.0
    severity = _classify(monthly_pct_used)
    is_anomaly = severity != "normal"

    if is_anomaly:
        message_short = _format_short(canonical, amount, monthly_pct_used)
        message_detail = _format_detail(
            canonical, amount, monthly_pct_used, baseline
        )
    else:
        message_short = ""
        message_detail = ""

    return {
        "is_anomaly": is_anomaly,
        "severity": severity,
        "monthly_pct_used": monthly_pct_used,
        "vs_group_pct": baseline["vs_group_avg_pct"],
        "message_short": message_short,
        "message_detail": message_detail,
        "baseline": baseline,
    }
