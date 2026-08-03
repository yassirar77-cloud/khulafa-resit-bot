"""Ingest the POS MONTHLY REPORT: email -> parsed sections -> Supabase.

Reuses the daily-sales poller (``sales_email_fetcher``): the same mailbox, the
same ``.TXT`` attachment extraction, the same UTF-16 decode. Only the subject
classification (``'M'``) and the parser differ.

The stored rows are what the POS PRINTED, verbatim. No reclassification happens
here — ``monthly_close`` derives COGS/wages/advances at read time, so changing a
rule re-reads the source instead of needing a backfill, and the stored month can
always be diffed against the original attachment.

Re-ingesting a month REPLACES its detail rows rather than appending: a POS that
re-sends a corrected report must not leave the month holding both versions. The
delete+insert is per (outlet, period) and ordered so a failure part-way leaves
the header row pointing at whatever detail survived, which the sections_found
column makes visible.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from monthly_report_parser import (
    MonthlyReportIntegrityError,
    format_integrity_alert,
    is_advance_row,
    missing_controls,
    parse_header,
    parse_monthly_report,
    verify_report_integrity,
)

logger = logging.getLogger(__name__)

MONTHLY_REPORT_TABLE = "monthly_report"
DAILY_SALES_TABLE = "monthly_daily_sales"
PAYOUTS_TABLE = "monthly_payouts"
ITEMWISE_TABLE = "monthly_itemwise"
STAFF_SALES_TABLE = "monthly_staff_sales"
STAFF_ADVANCE_TABLE = "monthly_staff_advance"

_DETAIL_TABLES = (
    DAILY_SALES_TABLE, PAYOUTS_TABLE, ITEMWISE_TABLE,
    STAFF_SALES_TABLE, STAFF_ADVANCE_TABLE,
)

_HEADER_COLUMNS = (
    "total_sales", "net_sales", "total_bank_sales", "total_fp_sales",
    "total_payouts", "total_purchase", "total_pinjam", "payout_pinjam",
    "total_pay_salary", "total_salary", "payout_purchase", "payout_expense",
    "total_paikkasu", "tax", "discount", "rounded",
    "total_cash", "qr_pay", "grab_pay", "total_customers",
)

#: Supabase insert batch size. The itemwise section of a busy outlet runs to
#: several hundred rows; PostgREST is happier with batches than one huge body.
BATCH_SIZE = 200


def resolve_outlet(outlet_code: Any, subject_code: Any = None) -> str:
    """Canonical outlet name for a monthly report.

    The attachment header wins over the email subject — the subject is typed by
    the POS operator, the header is generated. Falls back to the raw code so an
    unmapped outlet still ingests under a stable key rather than being dropped.
    """
    from outlet_resolver import canonical_outlet

    for candidate in (outlet_code, subject_code):
        if not candidate:
            continue
        resolved = canonical_outlet(str(candidate))
        if resolved:
            return resolved
    for candidate in (outlet_code, subject_code):
        if candidate:
            return re.sub(r"\s+", " ", str(candidate)).strip().upper()
    return "UNKNOWN"


def _period_bounds(period: str) -> tuple[str, str] | None:
    """``'2026-07'`` -> first/last day ISO, for the daily-sales date column."""
    import calendar

    match = re.fullmatch(r"(\d{4})-(\d{2})", str(period or ""))
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last).isoformat()


def _sale_date(date_cell: Any, period: str) -> str | None:
    """Turn a daily-table date cell into an ISO date within ``period``.

    Cells appear as ``01``, ``1/7``, ``01/07/2026`` across POS versions. A bare
    day number is interpreted within the report's own period — never against
    today's month, which would silently file July's sales under August.
    """
    bounds = _period_bounds(period)
    if bounds is None:
        return None
    year, month = int(period[:4]), int(period[5:7])
    text = str(date_cell or "").strip()
    if not text:
        return None
    parts = [p for p in re.split(r"[/\-.]", text) if p]
    try:
        if len(parts) == 1:
            day = int(parts[0])
        elif len(parts) == 2:
            day = int(parts[0])
        else:
            day, month_cell = int(parts[0]), int(parts[1])
            if month_cell != month:
                # A row whose printed month disagrees with the report period is
                # not silently re-dated; it keeps date_cell and a NULL date.
                return None
        if not 1 <= day <= 31:
            return None
        return date(year, month, day).isoformat()
    except (ValueError, TypeError):
        return None


def build_rows(parsed: dict, outlet: str, period: str) -> dict[str, list[dict]]:
    """Turn parser output into the insert payloads for each detail table."""
    key = {"outlet": outlet, "period": period}

    daily = [
        {**key,
         "sale_date": _sale_date(r.get("date_cell"), period),
         "date_cell": str(r.get("date_cell") or ""),
         "sale": r.get("sale"), "payout": r.get("payout"), "tax": r.get("tax")}
        for r in parsed.get("daily_sales") or []
    ]

    # 'purchase' holds the payouts block MINUS the advances; 'pinjam' holds the
    # ADVANCE TO rows the parser split out of it. Storing the block twice would
    # double-count every advance.
    payouts: list[dict] = []
    for block, rows in (("purchase", parsed.get("payouts")),
                        ("pinjam", parsed.get("pinjam")),
                        ("salary_block", parsed.get("salary_block")),
                        ("cheque", parsed.get("cheque")),
                        ("staff_meals", parsed.get("staff_meals"))):
        for index, row in enumerate(rows or [], start=1):
            payouts.append({
                **key, "line_no": index, "trno": row.get("trno"),
                "description": row.get("description"), "amount": row.get("amount"),
                "block": block,
            })

    itemwise = [
        {**key, "line_no": index, "item_name": r.get("item_name"),
         "qty": r.get("qty"), "amount": r.get("amount")}
        for index, r in enumerate(parsed.get("itemwise") or [], start=1)
    ]

    # Staff tables are keyed on (outlet, period, staff_name), so a POS that
    # prints one row per shift must be summed here or the second row would
    # collide with the first and silently replace it.
    staff_sales: dict[str, dict] = {}
    for row in parsed.get("staff_sales") or []:
        name = row.get("staff_name")
        entry = staff_sales.setdefault(name, {**key, "staff_name": name, "amount": 0.0})
        entry["amount"] = round(entry["amount"] + float(row.get("amount") or 0), 2)

    staff_advance: dict[str, dict] = {}
    for row in parsed.get("staff_advance") or []:
        name = row.get("staff_name")
        entry = staff_advance.setdefault(
            name, {**key, "staff_name": name, "advance": 0.0, "netsal": None}
        )
        entry["advance"] = round(entry["advance"] + float(row.get("advance") or 0), 2)
        if row.get("netsal") is not None:
            entry["netsal"] = row.get("netsal")

    return {
        DAILY_SALES_TABLE: daily,
        PAYOUTS_TABLE: payouts,
        ITEMWISE_TABLE: itemwise,
        STAFF_SALES_TABLE: list(staff_sales.values()),
        STAFF_ADVANCE_TABLE: list(staff_advance.values()),
    }


def _batched(rows: list[dict]):
    for start in range(0, len(rows), BATCH_SIZE):
        yield rows[start:start + BATCH_SIZE]


def store_monthly_report(
    supabase_client, parsed: dict, *, outlet: str, period: str,
    source_filename: str | None = None, email_uid: str | None = None,
    raw_text: str | None = None,
) -> dict:
    """Upsert the header row and replace the month's detail rows.

    Returns ``{"monthly_report_id", "counts", "replaced"}``. Raises on a failed
    header write (nothing else can be keyed without it); a failed detail batch is
    logged and counted so the caller can see a partial month rather than assume
    a whole one.
    """
    header = {
        "outlet": outlet,
        "period": period,
        "outlet_code": parsed.get("outlet_code"),
        "source_filename": source_filename,
        "email_uid": email_uid,
        "sections_found": parsed.get("sections_found") or [],
        "raw_text": raw_text,
    }
    totals = parsed.get("totals") or {}
    for column in _HEADER_COLUMNS:
        if column in totals:
            header[column] = totals[column]

    result = (
        supabase_client.table(MONTHLY_REPORT_TABLE)
        .upsert(header, on_conflict="outlet,period")
        .execute()
    )
    rows = getattr(result, "data", None) or []
    report_id = rows[0].get("id") if rows else None

    replaced = {}
    for table in _DETAIL_TABLES:
        try:
            deleted = (
                supabase_client.table(table).delete()
                .eq("outlet", outlet).eq("period", period).execute()
            )
            replaced[table] = len(getattr(deleted, "data", None) or [])
        except Exception:
            logger.exception("monthly ingest: failed clearing %s for %s %s",
                             table, outlet, period)
            replaced[table] = 0

    payloads = build_rows(parsed, outlet, period)
    counts: dict[str, int] = {}
    for table, rows_for_table in payloads.items():
        written = 0
        for batch in _batched(rows_for_table):
            for row in batch:
                row["monthly_report_id"] = report_id
            try:
                written += len(
                    getattr(supabase_client.table(table).insert(batch).execute(), "data", None)
                    or batch
                )
            except Exception:
                logger.exception(
                    "monthly ingest: insert failed for %s (%s %s, %d rows)",
                    table, outlet, period, len(batch),
                )
        counts[table] = written

    logger.info(
        "monthly ingest: stored %s %s — report_id=%s counts=%s replaced=%s",
        outlet, period, report_id, counts, replaced,
    )
    return {"monthly_report_id": report_id, "counts": counts, "replaced": replaced}


def ingest_monthly_email(supabase_client, email_data: dict) -> dict:
    """Parse one fetched monthly email and store it.

    ``email_data`` is a ``sales_email_fetcher.extract_shift_close`` dict. Returns
    a status dict; never raises for content reasons, so one malformed month
    cannot stop the poll loop.
    """
    content = email_data.get("content") or ""
    if not content.strip():
        return {"status": "error", "reason": "empty attachment",
                "subject": email_data.get("subject")}

    header = parse_header(content)
    if not header.get("period"):
        return {"status": "skipped", "reason": "no MONTHLY REPORT header line",
                "subject": email_data.get("subject")}

    parsed = parse_monthly_report(content)
    outlet = resolve_outlet(header.get("outlet_code"), email_data.get("outlet_code"))
    period = header["period"]

    if not parsed.get("itemwise") and not parsed.get("payouts"):
        logger.warning(
            "monthly ingest: %s %s parsed with no itemwise AND no payouts — "
            "sections found: %s", outlet, period, parsed.get("sections_found"),
        )

    # The silent-zero guard. A month whose blocks do not reconcile with its own
    # printed totals is NOT stored: a half-parsed block yields a plausible P&L
    # that is quietly wrong, which is worse than no P&L at all.
    try:
        verify_report_integrity(parsed)
    except MonthlyReportIntegrityError as exc:
        return {"status": "error", "reason": f"integrity check failed: {exc}",
                "failures": exc.failures,
                # Ready-to-post ops message naming the block and both numbers.
                "alert": format_integrity_alert(outlet, period, exc.failures),
                "outlet": outlet, "period": period,
                "sections_found": parsed.get("sections_found")}

    unverified = missing_controls(parsed)
    if unverified:
        logger.warning(
            "monthly ingest: %s %s stored, but these controls were absent so the "
            "corresponding checks could not run: %s", outlet, period, unverified,
        )

    try:
        stored = store_monthly_report(
            supabase_client, parsed, outlet=outlet, period=period,
            source_filename=email_data.get("filename"),
            email_uid=email_data.get("message_id"),
            raw_text=content,
        )
    except Exception as exc:  # noqa: BLE001 - report, don't abort the batch
        logger.exception("monthly ingest: store failed for %s %s", outlet, period)
        return {"status": "error", "reason": str(exc), "outlet": outlet, "period": period}

    return {"status": "ok", "outlet": outlet, "period": period,
            "sections_found": parsed.get("sections_found"),
            "unverified_controls": unverified, **stored}


def run_monthly_ingest(supabase_client, *, mailbox=None, since=None, mark_seen: bool = True) -> dict:
    """Poll the mailbox for monthly reports and ingest each one.

    Mirrors the daily poller's contract: a message is marked read only after it
    has been successfully ingested, so a crash mid-batch leaves the remainder to
    be retried rather than silently swallowed.
    """
    from sales_email_fetcher import Mailbox, extract_shift_close

    own = mailbox is None
    mailbox = mailbox or Mailbox.connect()
    results = {"ingested": [], "skipped": [], "errors": []}
    try:
        for msg_id in mailbox.search(since=since):
            try:
                data = extract_shift_close(mailbox.fetch(msg_id))
            except Exception:
                logger.exception("monthly ingest: failed reading message %r", msg_id)
                results["errors"].append({"msg_id": str(msg_id), "reason": "unreadable"})
                continue
            if data is None or data.get("email_type") != "M":
                continue
            outcome = ingest_monthly_email(supabase_client, data)
            if outcome["status"] == "ok":
                results["ingested"].append(outcome)
                if mark_seen:
                    mailbox.mark_seen(msg_id)
            elif outcome["status"] == "skipped":
                results["skipped"].append(outcome)
            else:
                results["errors"].append(outcome)
    finally:
        if own:
            mailbox.close()
    logger.info(
        "monthly ingest run: %d ingested, %d skipped, %d errors",
        len(results["ingested"]), len(results["skipped"]), len(results["errors"]),
    )
    return results
