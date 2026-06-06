"""DB glue for the smart receipt/POS-payout merge (PR #37).

Fetches the day's receipts (supplier purchases), POS payouts
(sales_daily_payouts) and sales (sales_daily_summary), runs the pure matcher in
purchase_reconciliation.py, and UPSERTs the result into purchase_reconciliation
plus a per-match purchase_match_log audit trail. Idempotent: re-running a date
overwrites that date's rows.

Called nightly before the digest and on-demand by /reconcile_now.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date as _date_type
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import purchase_reconciliation as pr
from date_utils import clamp_business_date
from outlet_resolver import canonical_outlet

logger = logging.getLogger(__name__)

MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")

RECEIPTS_TABLE = "receipts"
MERCHANT_CANONICAL_TABLE = "merchant_canonical"
SALES_DAILY_SUMMARY_TABLE = "sales_daily_summary"
SALES_DAILY_PAYOUTS_TABLE = "sales_daily_payouts"
RECONCILIATION_TABLE = "purchase_reconciliation"
MATCH_LOG_TABLE = "purchase_match_log"

SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"

# Food cost = food/supplies spend only. We include SUPPLIER_PURCHASE + PETTY_CASH
# + UNKNOWN (unresolved-merchant receipts are still real food spend) and exclude
# only the clearly-non-food types — mirroring the POS side, which already drops
# utility (Type E) and staff (Type D) payouts. Whitelisting SUPPLIER_PURCHASE
# alone dropped ~95% of receipts that hadn't been merchant-canonicalised yet.
NON_FOOD_RECEIPT_TYPES = ["STAFF_ADVANCE", "UTILITY", "RENT_LICENSE", "INTERNAL_TRANSFER"]

# Sanity bounds: receipts outside (RM5, RM5000] are almost always OCR errors
# (RM/Sen split-column phantoms, stray decimals) and shouldn't move food cost %.
MIN_RECEIPT_AMOUNT = 5.0
MAX_RECEIPT_AMOUNT = 5000.0


def _rows(resp):
    return getattr(resp, "data", None) or []


def _parse_date(value):
    return _date_type.fromisoformat(str(value)[:10])


def _load_canonical_merchants(client) -> dict:
    """{merchant_canonical_id: display_name}."""
    rows = _rows(
        client.table(MERCHANT_CANONICAL_TABLE).select("id, display_name").execute()
    )
    return {r["id"]: r.get("display_name") for r in rows if r.get("id") is not None}


_RECEIPT_COLUMNS = (
    "id, total, merchant, merchant_canonical_id, outlet, receipt_date, "
    "receipt_type, created_at"
)


def _fetch_receipts(client, business_date):
    """All food-relevant receipts whose *effective* business date is
    ``business_date``, plus a count of those whose future OCR date was clamped.

    Returns ``(rows, clamped_count)``. Two pools, deduped by id:
    1. receipt_date == business_date (the normal case).
    2. receipts uploaded (created_at) on that day in MY local time — this
       catches both NULL OCR dates (the old fallback) and future-dated OCR
       errors (receipt_date >3 days after upload), which would otherwise land
       on a future day that never gets reconciled.

    Each candidate's effective date is computed with ``clamp_business_date``;
    only those landing on ``business_date`` are kept. A receipt clamped INTO
    this day (future OCR date) is counted in ``clamped_count`` so the digest can
    surface the data-quality issue. A receipt whose receipt_date == this day but
    whose upload was >3 days earlier is clamped OUT (counted on its upload day
    instead) and dropped here."""
    day = business_date if isinstance(business_date, _date_type) else _parse_date(business_date)
    dated = _rows(
        client.table(RECEIPTS_TABLE)
        .select(_RECEIPT_COLUMNS)
        .not_.in_("receipt_type", NON_FOOD_RECEIPT_TYPES)
        .eq("receipt_date", str(business_date))
        .execute()
    )
    uploaded = _fetch_uploaded_receipts(client, day)

    kept: dict = {}
    clamped = 0
    for r in dated + uploaded:
        effective, was_clamped = clamp_business_date(r.get("receipt_date"), r.get("created_at"))
        if effective != day:
            continue
        rid = r.get("id")
        if rid in kept:
            continue
        kept[rid] = r
        if was_clamped:
            clamped += 1
    if clamped:
        logger.warning(
            "reconcile: clamped %d receipt(s) with a future OCR date to their "
            "upload day for %s", clamped, business_date,
        )
    return list(kept.values()), clamped


def _fetch_uploaded_receipts(client, day):
    """All food-relevant receipts uploaded on ``day`` (MY local), regardless of
    OCR'd receipt_date — the caller clamps each to its effective day."""
    try:
        start_local = datetime.combine(day, time.min, tzinfo=MALAYSIA_TZ)
        end_local = start_local + timedelta(days=1)
        return _rows(
            client.table(RECEIPTS_TABLE)
            .select(_RECEIPT_COLUMNS)
            .not_.in_("receipt_type", NON_FOOD_RECEIPT_TYPES)
            .gte("created_at", start_local.astimezone(timezone.utc).isoformat())
            .lt("created_at", end_local.astimezone(timezone.utc).isoformat())
            .execute()
        )
    except Exception:
        logger.warning("reconcile: upload-day receipt fetch failed for %s", day,
                       exc_info=True)
        return []



def _fetch_summaries(client, business_date):
    return _rows(
        client.table(SALES_DAILY_SUMMARY_TABLE)
        .select("id, outlet_canonical, business_date, day_sales")
        .eq("business_date", str(business_date))
        .execute()
    )


def _fetch_payouts(client, summary_ids):
    if not summary_ids:
        return []
    return _rows(
        client.table(SALES_DAILY_PAYOUTS_TABLE)
        .select("id, summary_id, description, vendor_name, amount, created_at")
        .in_("summary_id", summary_ids)
        .execute()
    )


def _float(value):
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _receipts_by_outlet(receipt_rows, canonical_by_id) -> dict:
    by_outlet: dict[str, list] = defaultdict(list)
    unresolved: Counter = Counter()
    skipped_amount = 0
    for r in receipt_rows:
        amount = _float(r.get("total"))
        if amount < MIN_RECEIPT_AMOUNT or amount > MAX_RECEIPT_AMOUNT:
            skipped_amount += 1
            continue
        outlet = canonical_outlet(r.get("outlet"))
        if outlet is None:
            raw = (r.get("outlet") or "").strip()
            if raw and raw.upper() != "UNKNOWN":
                unresolved[raw] += 1
            continue
        by_outlet[outlet].append(pr.Receipt(
            id=r.get("id"),
            amount=amount,
            merchant_canonical=canonical_by_id.get(r.get("merchant_canonical_id")),
            merchant=r.get("merchant"),
            receipt_type=r.get("receipt_type"),
            created_at=r.get("created_at"),
        ))
    if skipped_amount:
        logger.info("reconcile: skipped %d receipt(s) outside (RM%.0f, RM%.0f]",
                    skipped_amount, MIN_RECEIPT_AMOUNT, MAX_RECEIPT_AMOUNT)
    if unresolved:
        # Surface so the variants can be added to outlet_resolver rather than
        # silently dropping real spend (the second drop point from the audit).
        logger.warning(
            "reconcile: %d receipt(s) dropped — outlet did not resolve to a "
            "canonical outlet: %s",
            sum(unresolved.values()),
            ", ".join(f"{name!r}x{n}" for name, n in unresolved.most_common(10)),
        )
    return by_outlet


def _payouts_by_outlet(summaries, payout_rows) -> dict:
    summary_outlet = {s["id"]: s.get("outlet_canonical") for s in summaries}
    by_outlet: dict[str, list] = defaultdict(list)
    for p in payout_rows:
        outlet = summary_outlet.get(p.get("summary_id"))
        if outlet is None:
            continue
        by_outlet[outlet].append(pr.POSPayout(
            id=p.get("id"),
            description=p.get("description") or "",
            vendor_name=p.get("vendor_name"),
            amount=_float(p.get("amount")),
            created_at=p.get("created_at"),
        ))
    return by_outlet


def reconcile_outlet(client, outlet, business_date, receipts, payouts,
                     canonical_names, sales_total, *, dry_run=False):
    """Run the pure matcher for one outlet/date and persist the result.
    Returns the stored purchase_reconciliation row dict (with id).

    ``dry_run``: compute the row exactly as a real run would (same matcher, same
    ``summarize``) but write nothing — no UPSERT, no match-log rewrite. Returns
    the would-be row with ``id=None`` so a caller can preview sales_total etc."""
    result = pr.match_receipts_to_payouts(receipts, payouts, canonical_names)
    row = pr.summarize(result, outlet, business_date, sales_total=sales_total)

    if dry_run:
        preview = dict(row)
        preview["id"] = None
        return preview

    upserted = _rows(
        client.table(RECONCILIATION_TABLE)
        .upsert(row, on_conflict="outlet_canonical,business_date")
        .execute()
    )
    if not upserted:
        # Some Supabase clients don't return rows on upsert; read it back.
        upserted = _rows(
            client.table(RECONCILIATION_TABLE)
            .select("id")
            .eq("outlet_canonical", outlet)
            .eq("business_date", str(business_date))
            .execute()
        )
    recon_id = upserted[0].get("id") if upserted else None

    if recon_id is not None:
        # Idempotent rewrite of the per-match audit trail for this run.
        client.table(MATCH_LOG_TABLE).delete().eq("reconciliation_id", recon_id).execute()
        log_rows = pr.build_match_log(result)
        for lr in log_rows:
            lr["reconciliation_id"] = recon_id
        if log_rows:
            _insert_match_log(client, log_rows)
    stored = dict(row)
    stored["id"] = recon_id
    return stored


def _insert_match_log(client, log_rows) -> None:
    """Insert the audit rows; if the receipt_classification column (migration
    0023) isn't applied yet, retry without it so the rest of the trail still
    writes (mirrors digest_data.log_digest's message_bytes fallback)."""
    try:
        client.table(MATCH_LOG_TABLE).insert(log_rows).execute()
    except Exception:
        stripped = [{k: v for k, v in r.items() if k != "receipt_classification"}
                    for r in log_rows]
        try:
            client.table(MATCH_LOG_TABLE).insert(stripped).execute()
        except Exception:
            logger.warning("reconcile: could not write purchase_match_log", exc_info=True)


def run_reconciliation(client, business_date, *, dry_run=False) -> dict:
    """Reconcile every outlet that has sales or receipts on ``business_date``.
    Returns {outlets_processed, business_date, rows}.

    ``dry_run`` flows through to ``reconcile_outlet``: the rows are computed (so
    the caller can preview the would-be sales_total / purchases) but nothing is
    written."""
    canonical_by_id = _load_canonical_merchants(client)
    canonical_names = [n for n in canonical_by_id.values() if n]

    receipt_rows, clamped_count = _fetch_receipts(client, business_date)
    summaries = _fetch_summaries(client, business_date)
    payout_rows = _fetch_payouts(client, [s["id"] for s in summaries])

    receipts_by_outlet = _receipts_by_outlet(receipt_rows, canonical_by_id)
    payouts_by_outlet = _payouts_by_outlet(summaries, payout_rows)
    sales_by_outlet = {
        s.get("outlet_canonical"): _float(s.get("day_sales")) for s in summaries
    }

    outlets = set(receipts_by_outlet) | set(payouts_by_outlet) | set(sales_by_outlet)
    outlets.discard(None)

    stored_rows = []
    for outlet in sorted(outlets):
        try:
            stored = reconcile_outlet(
                client, outlet, business_date,
                receipts_by_outlet.get(outlet, []),
                payouts_by_outlet.get(outlet, []),
                canonical_names,
                sales_by_outlet.get(outlet),
                dry_run=dry_run,
            )
            stored_rows.append(stored)
        except Exception:
            logger.exception("reconcile failed for %s on %s", outlet, business_date)
    return {
        "business_date": str(business_date),
        "outlets_processed": len(stored_rows),
        "rows": stored_rows,
        "clamped_receipts": clamped_count,
    }


def run_reconciliation_for_dates(client, business_dates, *, dry_run=False) -> list[dict]:
    return [run_reconciliation(client, d, dry_run=dry_run) for d in business_dates]
