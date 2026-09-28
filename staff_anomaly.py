"""Anomaly questions: when a number is far off, ask about THAT instead.

Before each check-in the outlet's known numbers — items sold, food left
over (wastage) and order quantity per item — are compared with the same
weekday over the trailing 4 weeks. When one is outside ±``ANOMALY_PCT``
(Render env, default 35) of that average, the generic check-in is
replaced by a targeted question naming the item and both numbers, e.g.
facts ``{item: "Ayam", today: "20", usual: "10"}``. One anomaly question
per check-in at most: the largest deviation wins, and an item already
asked about today is not asked about again.

The numbers come from the database (bot.py gathers them); the AI provider
only phrases the question and the usual fact check applies. Logged to
``staff_chat_log`` with ``kind = 'anomaly'`` and ``deviation_pct`` in the
facts, so the threshold can be tuned later.

Pure: detection and wording only.
"""
from __future__ import annotations

import os

import staff_chat

DEFAULT_PCT = 35
MIN_WEEKS = 2          # fewer same-weekday data points than this is not an average
KIND = "anomaly"
SLOT = "anomaly"       # the pseudo check-in the templates and purpose use
METRICS = ("sales", "wastage", "order")


def threshold() -> float:
    """``ANOMALY_PCT`` as a fraction (0.35 by default)."""
    raw = (os.environ.get("ANOMALY_PCT") or "").strip().rstrip("%")
    try:
        pct = float(raw) if raw else DEFAULT_PCT
    except ValueError:
        pct = DEFAULT_PCT
    return max(1.0, pct) / 100.0


def detect(metrics: list[dict], *, asked_recently=()) -> dict | None:
    """The one metric furthest outside the band, as check-in facts, or None.

    ``metrics``: ``[{metric, item, today, usual: [same-weekday values],
    unit}]``. ``asked_recently``: ``(metric, item)`` pairs already asked
    about today."""
    band = threshold()
    best, best_dev = None, 0.0
    for m in metrics or []:
        try:
            today = float(m.get("today"))
            usual = [float(u) for u in (m.get("usual") or []) if u is not None]
        except (TypeError, ValueError):
            continue
        usual = [u for u in usual if u >= 0]
        if len(usual) < MIN_WEEKS or today < 0:
            continue
        avg = sum(usual) / len(usual)
        if avg <= 0:
            continue
        dev = today / avg - 1
        if abs(dev) < band or abs(dev) <= abs(best_dev):
            continue
        if (m.get("metric"), m.get("item")) in set(asked_recently):
            continue
        best, best_dev = m, dev
    if best is None:
        return None
    unit = str(best.get("unit") or "")
    avg = sum(float(u) for u in best["usual"]) / len(best["usual"])
    return {
        "anomaly": True,
        "metric": best.get("metric") if best.get("metric") in METRICS else "order",
        "item": staff_chat.item_label(best.get("item")) if best.get("metric") != "sales"
        else str(best.get("item") or "Sales"),
        "item_code": str(best.get("item") or ""),
        "today": staff_chat._qty_pack(staff_chat.fmt_qty(float(best["today"]), unit or None), unit),
        "usual": staff_chat._qty_pack(staff_chat.fmt_qty(avg, unit or None), unit),
        "direction": "high" if best_dev > 0 else "low",
        "deviation_pct": int(round(best_dev * 100)),
        "weeks": len(best["usual"]),
    }


def purpose(facts: dict) -> str:
    what = {"sales": "items sold", "wastage": "food left over / thrown",
            "order": "quantity ordered"}.get(facts.get("metric"), "quantity")
    return (f"today's {what} for {facts.get('item')} is {facts.get('today')} against a usual "
            f"{facts.get('usual')} on this weekday — ask, without blaming, why it is so "
            f"{'high' if facts.get('direction') == 'high' else 'low'} today")


def log_row(slot, outlet_code, chat_id, cashier, language, facts, result, mode) -> dict:
    row = staff_chat.log_row(slot, outlet_code, chat_id, cashier, language, facts, result, mode)
    row["kind"] = KIND
    return row
