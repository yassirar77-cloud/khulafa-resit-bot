"""Missing supplier-bill detection — "BESTARI FARM bill எங்க?"

The bot holds months of supplier purchase history per upload chat. When a
supplier that reliably showed up every ~N days suddenly stops appearing,
the most common reason is not a dropped supplier — it's a cashier who
stopped photographing the bills. This module finds those silences and
asks the outlet's own chat, in Tamil (+ a Malay line), why nothing was
uploaded, naming the supplier.

Rhythm detection reuses ``order_cadence.detect_cadence`` anchored at the
supplier's LAST bill date, so the silence being measured can never
corrupt the rhythm it is measured against. A supplier is chased only
when ALL of these hold:

  * at least ``_MIN_BILLS`` distinct bill days in the 90 days before the
    last bill — a genuinely regular supplier, not a one-off stall;
  * the rhythm was confident while it was alive (not NEEDS_REVIEW);
  * the silence exceeds max(2 x median gap, median gap + _GRACE_DAYS) —
    one late delivery is normal, two missed cycles is a question;
  * the silence is at most ``_MAX_SILENCE_DAYS`` — beyond that it's a
    dropped supplier, and a chat message won't bring the bills back.

Chase pacing is stateless: the daily job asks on the first overdue day
and then every ``_REASK_EVERY`` days while the silence lasts, computed
from the dates alone — no new table, nothing to migrate.

I/O convention matches the rest of the repo: the supabase client is the
first argument everywhere, and every entry point swallows exceptions and
returns a safe default. A detection failure must never break the bot.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

from db_pagination import fetch_all_pages
from order_cadence import detect_cadence

logger = logging.getLogger(__name__)

_RECEIPTS_TABLE = "receipts"

# Same "filter by what a receipt is NOT" rule as shop_price_comparison:
# receipt_type defaults to 'UNKNOWN' and only a slice of the table was ever
# classified, so requiring SUPPLIER_PURCHASE would erase most history.
_NON_SUPPLIER_RECEIPT_TYPES = frozenset({
    "INTERNAL_TRANSFER", "STAFF_ADVANCE", "UTILITY", "RENT_LICENSE",
    "PETTY_CASH",
})

_MIN_BILLS = 6          # distinct bill days needed to call a supplier regular
_GRACE_DAYS = 2         # slack on top of the median gap before chasing
_MAX_SILENCE_DAYS = 45  # past this, it's a supplier change, not a lost bill
_REASK_EVERY = 3        # chase on overdue day 1, then every 3 days
_LOOKBACK_DAYS = 120    # receipts window read from the DB
_CADENCE_WINDOW = 90    # rhythm window, anchored at the last bill


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip().replace("Z", "").split("T")[0].split(" ")[0]
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    return None


def load_supplier_bill_rows(supabase, *, today: date | None = None) -> list[dict]:
    """Supplier-bill rows from ``receipts`` for the lookback window.

    Each row: ``{'chat_id', 'outlet', 'merchant', 'receipt_date'(date)}``.
    Drops non-supplier receipt types, own-outlet transfers, blank
    merchants, dateless rows and corrupt future dates. Returns ``[]`` on
    any failure. Never raises.
    """
    try:
        from shop_price_comparison import _OWN_OUTLET_MARKERS

        base_today = today or date.today()
        cutoff = (base_today - timedelta(days=_LOOKBACK_DAYS)).isoformat()

        def _build():
            return (
                supabase.table(_RECEIPTS_TABLE)
                .select("id, chat_id, outlet, merchant, receipt_date, receipt_type")
                .gte("receipt_date", cutoff)
                .order("id", desc=False)
            )

        try:
            raw = fetch_all_pages(_build)
        except Exception:
            logger.debug(
                "missing bills: paginated read unavailable, falling back",
                exc_info=True,
            )
            raw = (
                supabase.table(_RECEIPTS_TABLE)
                .select("id, chat_id, outlet, merchant, receipt_date, receipt_type")
                .gte("receipt_date", cutoff)
                .execute()
                .data
                or []
            )

        rows: list[dict] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            merchant = str(row.get("merchant") or "").strip()
            if not merchant:
                continue
            lowered = merchant.lower()
            if any(marker in lowered for marker in _OWN_OUTLET_MARKERS):
                continue
            rtype = str(row.get("receipt_type") or "").strip().upper()
            if rtype in _NON_SUPPLIER_RECEIPT_TYPES:
                continue
            when = _to_date(row.get("receipt_date"))
            if when is None or when > base_today:
                continue
            chat_id = row.get("chat_id")
            if chat_id is None:
                continue
            rows.append({
                "chat_id": chat_id,
                "outlet": row.get("outlet"),
                "merchant": merchant,
                "receipt_date": when,
            })
        return rows
    except Exception:
        logger.exception("missing bills: receipts read failed")
        return []


def _group_suppliers(rows: list[dict]) -> list[dict]:
    """Group rows by (chat, supplier): OCR spelling variants collapse onto
    one shop via ``shop_key``, labelled with the most common spelling."""
    from shop_price_comparison import shop_display, shop_key

    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (row["chat_id"], shop_key(row["merchant"]))
        bucket = groups.setdefault(key, {
            "chat_id": row["chat_id"],
            "dates": set(),
            "_names": {},
            "_outlet_key": None,
            "outlet": None,
        })
        bucket["dates"].add(row["receipt_date"])
        bucket["_names"][row["merchant"]] = bucket["_names"].get(row["merchant"], 0) + 1
        # Outlet label: whatever the newest row says (labels can be fixed up).
        outlet = row.get("outlet")
        if outlet and (
            bucket["_outlet_key"] is None or row["receipt_date"] >= bucket["_outlet_key"]
        ):
            bucket["_outlet_key"] = row["receipt_date"]
            bucket["outlet"] = outlet

    out: list[dict] = []
    for bucket in groups.values():
        label = sorted(
            bucket["_names"].items(), key=lambda kv: (-kv[1], len(kv[0]))
        )[0][0]
        out.append({
            "chat_id": bucket["chat_id"],
            "outlet": bucket["outlet"],
            "supplier": shop_display(label),
            "dates": sorted(bucket["dates"]),
        })
    return out


def overdue_threshold_days(median_gap: float) -> int:
    """Days of silence tolerated before a supplier counts as missing."""
    return int(math.ceil(max(2.0 * median_gap, median_gap + _GRACE_DAYS)))


def find_missing_bills(rows: list[dict], *, today: date | None = None) -> list[dict]:
    """The suppliers whose bills have gone quiet, per upload chat.

    Takes rows from ``load_supplier_bill_rows``. Each entry:
    ``{'chat_id', 'outlet', 'supplier', 'last_date', 'days_missing',
    'median_gap_days', 'sample_count', 'threshold_days', 'days_overdue',
    'ask_today'}`` — ``ask_today`` implements the stateless re-ask pacing.
    Sorted by outlet then supplier. Never raises.
    """
    try:
        base_today = today or date.today()
        entries: list[dict] = []
        for group in _group_suppliers([r for r in rows if isinstance(r, dict)]):
            try:
                dates = group["dates"]
                if len(dates) < _MIN_BILLS:
                    continue
                last = dates[-1]
                days_missing = (base_today - last).days
                if days_missing <= 0 or days_missing > _MAX_SILENCE_DAYS:
                    continue

                # Anchor the rhythm at the last bill: we're asking whether the
                # supplier WAS regular, and the current silence must not be
                # allowed to answer that question itself.
                cadence = detect_cadence(
                    dates, today=last, lookback_days=_CADENCE_WINDOW
                )
                median_gap = cadence.get("median_gap_days")
                if cadence.get("needs_review") or not median_gap:
                    continue

                threshold = overdue_threshold_days(median_gap)
                if days_missing <= threshold:
                    continue
                days_overdue = days_missing - threshold
                entries.append({
                    "chat_id": group["chat_id"],
                    "outlet": group["outlet"],
                    "supplier": group["supplier"],
                    "last_date": last,
                    "days_missing": days_missing,
                    "median_gap_days": float(median_gap),
                    "sample_count": cadence.get("sample_count") or len(dates),
                    "threshold_days": threshold,
                    "days_overdue": days_overdue,
                    "ask_today": (days_overdue - 1) % _REASK_EVERY == 0,
                })
            except Exception:
                logger.exception("missing bills: per-supplier failure (skipping)")
                continue
        entries.sort(key=lambda e: (str(e["outlet"] or "~"), e["supplier"]))
        return entries
    except Exception:
        logger.exception("missing bills: unexpected failure")
        return []


def _rhythm_tamil(median_gap: float) -> str:
    if median_gap < 2.0:
        return "தினமும்"
    return f"~{median_gap:.0f} நாளுக்கு ஒரு தடவை"


def format_missing_bill_message(entry: dict) -> str:
    """The chat question, Tamil first, one Malay line after — the same
    bilingual register as the audit warnings. Returns ``""`` on malformed
    input so the caller skips silently. Never raises."""
    try:
        if not isinstance(entry, dict):
            return ""
        supplier = str(entry.get("supplier") or "").strip()
        if not supplier:
            return ""
        last = entry["last_date"]
        last_txt = f"{last.day:02d}/{last.month:02d}"
        days = int(entry["days_missing"])
        rhythm = _rhythm_tamil(float(entry["median_gap_days"]))
        n = int(entry["sample_count"])

        return (
            f"🧾 {supplier} bill எங்க?\n"
            "\n"
            f"வழக்கமா {rhythm} {supplier} bill upload ஆகும் ({n} bills history).\n"
            f"கடைசி bill: {last_txt} — அதுக்கப்புறம் {days} நாளா ஒன்னும் இல்ல.\n"
            "\n"
            "ஏன் upload பண்ணல? Bill இருந்தா இப்பவே photo எடுத்து போடுங்க 📸\n"
            "Supplier மாத்திட்டீங்களா / வாங்கறது நிறுத்திட்டீங்களா-ன்னா "
            "reply-ல சொல்லுங்க.\n"
            "\n"
            f"Resit {supplier} dah {days} hari tak upload (last {last_txt}). "
            "Kalau ada bill, upload sekarang. Kalau dah tukar supplier, "
            "bagitau ya. 🙏"
        )
    except Exception:
        logger.exception("missing bills: failed to format chat message")
        return ""


def format_owner_summary(entries: list[dict], outlet_label=None) -> str:
    """Compact English overview for the alert group: every currently-quiet
    supplier, asked today or not, so the director sees the whole hole.
    ``outlet_label`` maps an outlet code to a display name. Returns ``""``
    when there is nothing to report. Never raises."""
    try:
        if not entries:
            return ""
        label = outlet_label if callable(outlet_label) else (lambda c: str(c or "?"))
        lines = ["🧾 Supplier bills gone quiet:"]
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                mark = "❓ asked today" if e.get("ask_today") else "⏳ chased recently"
                lines.append(
                    f"• {label(e.get('outlet'))} — {e['supplier']}: last "
                    f"{e['last_date'].isoformat()}, {int(e['days_missing'])}d silent "
                    f"(usually every ~{float(e['median_gap_days']):.0f}d) — {mark}"
                )
            except Exception:
                continue
        if len(lines) == 1:
            return ""
        return "\n".join(lines)
    except Exception:
        logger.exception("missing bills: failed to format owner summary")
        return ""
