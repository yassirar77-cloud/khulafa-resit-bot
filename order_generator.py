"""Order-list generator — the DB glue that ties cadence + quantity + drafts.

Reads the passive ``item_prices`` corpus (one row per receipt line, already
canonicalised, with outlet / qty / unit_price / merchant / receipt_date),
groups it per (outlet, canonical item) over the lookback window, then for each
item:

  * learns the buying rhythm        (order_cadence.detect_cadence)
  * decides if it's due tomorrow     (order_cadence.is_due)
  * forecasts the quantity           (order_draft.forecast_qty)
  * picks the supplier + flags        (dominant merchant, spike, cheaper alt)

and builds one Telegram draft per outlet. Drafts are persisted to
``order_drafts`` (status='draft') so a manager's later edit/send can update the
row. NOTHING here sends to a supplier — that's a human step (spec §1, §9).

I/O convention matches the rest of the repo: every DB function takes the
``supabase`` client as its first argument, so the heavy logic stays pure and
testable with a fake client.
"""
from __future__ import annotations

import logging
import os
import statistics
from datetime import date, datetime, timedelta

import order_cadence as oc
import order_draft
import order_items

logger = logging.getLogger(__name__)

_ITEM_PRICES_TABLE = "item_prices"
_DRAFTS_TABLE = "order_drafts"

# Spec §6 config (env-overridable, never hardcoded secrets).
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_SEND_HOUR = 20  # 20:00 MY — ahead of the 23:00 digest so it isn't buried

# Spike flag reuses the live detector's rule: >110% of the historical average
# with at least 5 prior samples — but computed from the rows we already have,
# so the draft adds no extra DB load.
_SPIKE_THRESHOLD = 1.10
_SPIKE_MIN_SAMPLES = 5


def lookback_days() -> int:
    raw = os.environ.get("CADENCE_LOOKBACK_DAYS")
    try:
        return int(raw) if raw else DEFAULT_LOOKBACK_DAYS
    except (TypeError, ValueError):
        return DEFAULT_LOOKBACK_DAYS


def send_hour() -> int:
    raw = os.environ.get("ORDER_DRAFT_SEND_HOUR")
    try:
        h = int(raw) if raw else DEFAULT_SEND_HOUR
        return h if 0 <= h <= 23 else DEFAULT_SEND_HOUR
    except (TypeError, ValueError):
        return DEFAULT_SEND_HOUR


def failure_alert(*, gather_error: str | None = None, total_messages: int = 0,
                  failed_messages: int = 0, hq_failed: bool = False) -> str | None:
    """Owner-facing alert text when an evening order-draft run had ANY failure,
    else ``None``. Pure so the evening job can never fail silently again: bot.py
    sends the returned string to the owner (ALERT_CHAT_ID).

    ``gather_error`` short-circuits — a build crash means nothing was sent at all.
    Otherwise it reports partial send failures and/or a missing HQ summary.
    """
    if gather_error:
        return "⚠️ Order drafts: build failed (%s) — no drafts sent tonight." % gather_error
    problems: list[str] = []
    if failed_messages:
        problems.append("%d/%d draft message(s) failed to send"
                        % (failed_messages, total_messages))
    if hq_failed:
        problems.append("HQ summary failed to send")
    if not problems:
        return None
    return "⚠️ Order drafts: " + "; ".join(problems) + "."


# --- fetch + group (DB) ------------------------------------------------------

def fetch_item_price_rows(supabase, *, today: date, lookback: int) -> list[dict]:
    """All ``item_prices`` rows with a receipt_date inside the lookback window.
    Returns ``[]`` on any failure — a draft run must never crash the bot."""
    cutoff = (today - timedelta(days=lookback)).isoformat()
    try:
        resp = (
            supabase.table(_ITEM_PRICES_TABLE)
            .select("outlet_code, canonical_item, qty, unit_price, merchant, "
                    "raw_item_name, receipt_date")
            .gte("receipt_date", cutoff)
            .execute()
        )
        return resp.data or []
    except Exception:
        logger.exception("order_generator: fetch_item_price_rows failed")
        return []


def group_rows(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group raw rows by (outlet_code, canonical_item) into the lightweight
    record shape the pure layer expects: ``{date, qty, unit_price, merchant}``.
    Rows missing an outlet, canonical item, or date are dropped."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in rows or []:
        outlet = (r.get("outlet_code") or "").strip()
        canonical = (r.get("canonical_item") or "").strip().lower()
        d = r.get("receipt_date")
        if not outlet or not canonical or d is None:
            continue
        grouped.setdefault((outlet, canonical), []).append({
            "date": d,
            "qty": r.get("qty"),
            "unit_price": r.get("unit_price"),
            "merchant": r.get("merchant"),
            "raw_item_name": r.get("raw_item_name"),
        })
    return grouped


# --- pure analysis -----------------------------------------------------------

def dominant_supplier(records: list[dict]) -> str | None:
    """The merchant this item is bought from most often (the natural supplier)."""
    counts: dict[str, int] = {}
    for r in records:
        m = (r.get("merchant") or "").strip()
        if m:
            counts[m] = counts.get(m, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def spike_note(records: list[dict]) -> str | None:
    """If the most recent unit price is >110% of the average of the prior
    samples (≥5), return a short note. Self-contained — no extra DB query."""
    priced = [
        (oc._to_date(r.get("date")), float(r["unit_price"]))
        for r in records
        if isinstance(r.get("unit_price"), (int, float))
        and not isinstance(r.get("unit_price"), bool)
        and r.get("unit_price") and float(r["unit_price"]) > 0
        and oc._to_date(r.get("date")) is not None
    ]
    if len(priced) < _SPIKE_MIN_SAMPLES + 1:
        return None
    priced.sort(key=lambda t: t[0])
    latest = priced[-1][1]
    prior = [p for _, p in priced[:-1]]
    avg = statistics.fmean(prior)
    if avg > 0 and latest > _SPIKE_THRESHOLD * avg:
        pct = (latest - avg) / avg * 100.0
        return ("harga naik: RM%.2f vs purata RM%.2f (+%.0f%%) — tanya supplier?"
                % (latest, avg, pct))
    return None


def build_lines_for_outlet(items: dict[str, list[dict]], *, today: date) -> list[dict]:
    """Build the due draft lines for one outlet.

    ``items`` maps canonical_item -> records. Returns the list of line dicts
    (ready for ``order_draft.build_outlet_message``) for items that are due
    tomorrow, plus any erratic item flagged NEEDS_REVIEW (never silently
    dropped)."""
    tomorrow = today + timedelta(days=1)
    lines: list[dict] = []
    for canonical, records in items.items():
        if not order_items.is_orderable(canonical):
            continue
        cadence_info = oc.detect_cadence(
            [r.get("date") for r in records], today=today, lookback_days=lookback_days())
        cadence_info["canonical_item"] = canonical
        due = oc.is_due(cadence_info, today=today, tomorrow=tomorrow)
        if not due["due"] and not cadence_info.get("needs_review"):
            continue

        fc = order_draft.forecast_qty(records, cadence_info, target_day=tomorrow,
                                      today=today, lookback_days=lookback_days())
        supplier = dominant_supplier(records)
        lines.append({
            "canonical_item": canonical,
            "qty": fc["qty"],
            "pack": fc["pack"],
            "pack_known": fc["pack_known"],
            "qty_anomaly": fc.get("qty_anomaly", False),
            "raw_qty": fc.get("raw_qty"),
            "excluded_count": fc.get("excluded_count", 0),
            "excluded_qtys": fc.get("excluded_qtys"),
            "history_expired": fc.get("history_expired", False),
            "cadence_info": cadence_info,
            "due_info": due,
            "supplier": supplier,
            "alternate": order_items.cheaper_alternate(canonical, supplier),
            "spike": spike_note(records),
            "basis": fc.get("basis"),
        })

    # Stable, readable order: needs-review last, otherwise by item name.
    lines.sort(key=lambda ln: (ln["cadence_info"].get("needs_review", False),
                               order_items.display_name(ln["canonical_item"])))
    return lines


# --- persistence (DB) --------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def persist_drafts(supabase, outlet_code: str, due_date: date, lines: list[dict]) -> int:
    """Replace today's draft rows for this outlet+due_date, then insert the
    current lines (status='draft'). Best-effort: never raises."""
    if not lines:
        return 0
    try:
        supabase.table(_DRAFTS_TABLE).delete().eq("outlet", outlet_code).eq(
            "due_date", due_date.isoformat()).eq("status", "draft").execute()
        rows = []
        for ln in lines:
            ci = ln["cadence_info"]
            flags = []
            if not ln.get("pack_known"):
                flags.append("PACK_UNKNOWN")
            if ci.get("needs_review"):
                flags.append("NEEDS_REVIEW")
            if ln.get("alternate"):
                flags.append("CHEAPER_ALT")
            if ln.get("spike"):
                flags.append("PRICE_SPIKE")
            if ln.get("qty_anomaly"):
                flags.append("QTY_ANOMALY")
            if ln.get("excluded_count"):
                flags.append("QTY_OUTLIER_EXCLUDED")
            if ln.get("history_expired"):
                flags.append("HISTORY_EXPIRED")
            rows.append({
                "outlet": outlet_code,
                "supplier": ln.get("supplier"),
                "item": ln["canonical_item"],
                "qty": ln.get("qty"),
                "pack": ln.get("pack"),
                "due_date": due_date.isoformat(),
                "cadence": ci.get("cadence"),
                "flags": ",".join(flags),
                "status": "draft",
                "created_at": _now_iso(),
            })
        supabase.table(_DRAFTS_TABLE).insert(rows).execute()
        return len(rows)
    except Exception:
        logger.exception("order_generator: persist_drafts failed for %s", outlet_code)
        return 0


def persist_cadence(supabase, outlet_code: str, items: dict[str, list[dict]],
                    *, today: date) -> int:
    """Refresh the learned ``item_cadence`` snapshot for one outlet's items.
    Best-effort: never raises. Returns rows written."""
    written = 0
    for canonical, records in items.items():
        if not order_items.is_orderable(canonical):
            continue
        ci = oc.detect_cadence([r.get("date") for r in records],
                               today=today, lookback_days=lookback_days())
        dow = ci.get("dow_pattern")
        last = ci.get("last_purchase_date")
        row = {
            "outlet": outlet_code,
            "item": canonical,
            "cadence": ci.get("cadence"),
            "median_gap_days": ci.get("median_gap_days"),
            "last_purchase_date": last.isoformat() if last else None,
            "confidence": ci.get("confidence"),
            "dow_pattern": ",".join(dow) if dow else None,
            "sample_count": ci.get("sample_count"),
            "needs_review": ci.get("needs_review", False),
            "updated_at": _now_iso(),
        }
        try:
            supabase.table("item_cadence").delete().eq("outlet", outlet_code).eq(
                "item", canonical).execute()
            supabase.table("item_cadence").insert(row).execute()
            written += 1
        except Exception:
            logger.exception("order_generator: persist_cadence failed (%s/%s)",
                             outlet_code, canonical)
    return written


def gather_order_drafts(supabase, *, today: date | None = None,
                        display_for=None, persist: bool = True) -> dict:
    """Top-level entry: fetch history, build per-outlet drafts, persist, and
    return everything bot.py needs to route the messages.

    ``display_for(outlet_code) -> str`` resolves a human outlet name (defaults to
    the code). Returns ``{'target_day', 'outlets': [ {outlet_code, display,
    message, line_count, review_count} ], 'has_data'}``.
    """
    today = today or date.today()
    target_day = today + timedelta(days=1)
    display_for = display_for or (lambda code: code)

    rows = fetch_item_price_rows(supabase, today=today, lookback=lookback_days())
    grouped = group_rows(rows)

    # Re-shape to outlet -> {canonical -> records}.
    by_outlet: dict[str, dict[str, list[dict]]] = {}
    for (outlet, canonical), records in grouped.items():
        by_outlet.setdefault(outlet, {})[canonical] = records

    outlets_out: list[dict] = []
    for outlet_code in sorted(by_outlet):
        if persist:
            persist_cadence(supabase, outlet_code, by_outlet[outlet_code], today=today)
        lines = build_lines_for_outlet(by_outlet[outlet_code], today=today)
        if not lines:
            continue
        display = display_for(outlet_code)
        # ``message`` is the full unbounded draft (debugging / back-compat);
        # ``messages`` is the Telegram-safe split that delivery actually sends.
        message = order_draft.build_outlet_message(display, target_day, lines)
        messages = order_draft.build_outlet_messages(display, target_day, lines)
        if persist:
            persist_drafts(supabase, outlet_code, target_day, lines)
        outlets_out.append({
            "outlet_code": outlet_code,
            "display": display,
            "message": message,
            "messages": messages,
            "line_count": len(lines),
            "review_count": sum(1 for ln in lines
                                if ln["cadence_info"].get("needs_review")),
        })

    return {
        "target_day": target_day,
        "outlets": outlets_out,
        "has_data": bool(rows),
    }
