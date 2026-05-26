"""POS shift-close ingestion orchestration (PR #35).

Drives the IMAP mailbox (search → fetch → parse → store → mark-seen) and writes
one ``sales_daily`` row (+ child rows) per outlet shift. Idempotent: a shift is
keyed on (outlet_canonical, shift_no, shift_business_date, shift_type), so
re-running over the same inbox never double-inserts.

Imported by both the bot (APScheduler job + /sales_ingest_manual) and the CLI
entry point (scripts/ingest_sales_emails.py), mirroring the
digest_data.py / scripts/send_daily_digest.py split.

A message is flagged ``\\Seen`` ONLY after it is successfully ingested or
recognised as a duplicate. Parse/empty-attachment failures are left unread so
they can be retried/inspected rather than silently lost.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from sales_email_fetcher import Mailbox, extract_shift_close
from sales_parser import canonical_outlet_for_code, parse_shift_close

logger = logging.getLogger(__name__)

MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")

SALES_DAILY_TABLE = "sales_daily"
SALES_INGEST_LOG_TABLE = "sales_ingest_log"
CHILD_TABLES = (
    "sales_items",
    "sales_payments",
    "sales_categories",
    "sales_tax",
    "sales_discounts",
    "sales_deleted_items",
    "sales_stock",
    "sales_cashdrawer",
)


# --- record building (pure) --------------------------------------------------

def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _parse_received_at(raw):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def resolve_business_date(parsed, now_my):
    """The shift's business date. Comes from the close time (already adjusted for
    overnight shifts by the parser); falls back to today so the NOT NULL column
    is always satisfied."""
    bd = parsed.get("shift_business_date")
    if bd is not None:
        return bd
    return now_my.date()


def build_sales_record(email_dict, parsed, outlet_canonical, now_my) -> dict:
    """Assemble the ``sales_daily`` parent row + grouped child rows + the
    idempotency key from a parsed report. Pure: no I/O."""
    code = email_dict.get("outlet_code")
    # Never drop a shift just because the outlet is unknown — keep the code (or
    # a sentinel) so it is still queryable / fixable later.
    outlet = outlet_canonical or code or "UNKNOWN"
    shift_no = parsed.get("shift_no") or email_dict.get("shift_no_from_subject")
    business_date = resolve_business_date(parsed, now_my)
    shift_type = parsed.get("shift_type") or "unknown"

    parent = {
        "outlet_canonical": outlet,
        "outlet_code": code,
        "shift_no": shift_no,
        "terminal": parsed.get("terminal"),
        "cashier": parsed.get("cashier"),
        "shift_open_at": _iso(parsed.get("open_time")),
        "shift_close_at": _iso(parsed.get("close_time")),
        "shift_type": shift_type,
        "shift_business_date": business_date.isoformat(),
        "gross_sales": parsed.get("gross_sales"),
        "discount": parsed.get("discount"),
        "service_charge": parsed.get("service_charge"),
        "tax": parsed.get("tax") if parsed.get("tax") is not None else 0.0,
        "net_sales": parsed.get("net_sales"),
        "total_sales": parsed.get("total_sales") if parsed.get("total_sales") is not None else 0.0,
        "header_outlet_raw": parsed.get("header_outlet_raw"),
        "sections_present": parsed.get("sections_present"),
        "source_subject": email_dict.get("subject"),
        "source_message_id": email_dict.get("message_id"),
        "source_filename": email_dict.get("filename"),
        "received_at": _parse_received_at(email_dict.get("received_at")),
        "raw_content": email_dict.get("content"),
    }

    children = {
        "sales_items": [
            {"qty": i["qty"], "item_name": i["name"], "amount": i["amount"]}
            for i in parsed.get("items", [])
        ],
        "sales_payments": [
            {"method": p["label"], "amount": p["amount"]} for p in parsed.get("payments", [])
        ],
        "sales_categories": [
            {"category": c["label"], "amount": c["amount"]} for c in parsed.get("categories", [])
        ],
        "sales_tax": [
            {"label": t["label"], "amount": t["amount"]} for t in parsed.get("tax_breakdown", [])
        ],
        "sales_discounts": [
            {"label": d["label"], "amount": d["amount"]} for d in parsed.get("discounts", [])
        ],
        "sales_deleted_items": [
            {"qty": i["qty"], "item_name": i["name"], "amount": i["amount"]}
            for i in parsed.get("deleted_items", [])
        ],
        "sales_stock": [
            {"item_name": s["item"], "qty": s["qty"]} for s in parsed.get("stock", [])
        ],
        "sales_cashdrawer": [
            {"label": c["label"], "amount": c["amount"]} for c in parsed.get("cashdrawer", [])
        ],
    }

    return {
        "parent": parent,
        "children": children,
        "key": (outlet, shift_no, business_date.isoformat(), shift_type),
    }


# --- DB store (Supabase) -----------------------------------------------------

class SupabaseSalesStore:
    """Persists shift records to Supabase and exposes the idempotency check.
    Tests substitute an in-memory fake with the same three methods."""

    def __init__(self, client):
        self.client = client

    def exists(self, outlet_canonical, shift_no, business_date, shift_type) -> bool:
        q = (
            self.client.table(SALES_DAILY_TABLE)
            .select("id")
            .eq("outlet_canonical", outlet_canonical)
            .eq("shift_business_date", business_date)
            .eq("shift_type", shift_type)
        )
        # shift_no can legitimately be NULL; match it explicitly either way.
        q = q.is_("shift_no", "null") if shift_no is None else q.eq("shift_no", shift_no)
        resp = q.limit(1).execute()
        return bool(resp.data)

    def save(self, record) -> int:
        resp = self.client.table(SALES_DAILY_TABLE).insert(record["parent"]).execute()
        daily_id = resp.data[0]["id"]
        for table in CHILD_TABLES:
            rows = record["children"].get(table) or []
            if not rows:
                continue
            payload = [{**r, "sales_daily_id": daily_id} for r in rows]
            self.client.table(table).insert(payload).execute()
        return daily_id

    def log(self, entry) -> None:
        try:
            self.client.table(SALES_INGEST_LOG_TABLE).insert(entry).execute()
        except Exception:  # noqa: BLE001 - logging must never break ingestion
            logger.warning("Could not write sales_ingest_log row", exc_info=True)


# --- per-email processing ----------------------------------------------------

def process_email(store, email_dict, *, now_my, resolve_outlet=canonical_outlet_for_code):
    """Ingest one shift-close email. Returns ``(status, detail)`` where status is
    ``inserted`` / ``skipped`` (duplicate) / ``error``. Never raises for expected
    data problems (empty attachment, unparseable total) — those return an error
    status so the caller leaves the message unread."""
    content = email_dict.get("content") or ""
    if not content.strip():
        return "error", "empty_attachment"

    outlet_canonical = resolve_outlet(email_dict.get("outlet_code"))
    parsed = parse_shift_close(content)

    if parsed.get("total_sales") is None:
        return "error", "no_total_parsed"

    record = build_sales_record(email_dict, parsed, outlet_canonical, now_my)
    if store.exists(*record["key"]):
        return "skipped", "duplicate"
    store.save(record)
    return "inserted", None


def _log_entry(email_dict, status, detail, outlet_canonical):
    return {
        "outlet_code": email_dict.get("outlet_code"),
        "outlet_canonical": outlet_canonical,
        "source_subject": email_dict.get("subject"),
        "source_message_id": email_dict.get("message_id"),
        "status": status,
        "detail": detail,
    }


def _maybe_log(store, entry):
    log = getattr(store, "log", None)
    if callable(log):
        log(entry)


def run(*, store, mailbox, now_my, since=None, resolve_outlet=canonical_outlet_for_code) -> dict:
    """Drive the mailbox end-to-end. Returns a summary dict.

    Marks a message ``\\Seen`` only on ``inserted``/``skipped``; ``error`` leaves
    it unread for retry/inspection."""
    summary = {"fetched": 0, "inserted": 0, "skipped": 0, "errors": 0}
    for msg_id in mailbox.search(since=since):
        try:
            msg = mailbox.fetch(msg_id)
            email_dict = extract_shift_close(msg)
        except Exception:  # noqa: BLE001 - one bad message must not abort the batch
            logger.exception("Failed to read message %r", msg_id)
            summary["errors"] += 1
            continue
        if email_dict is None:
            summary["errors"] += 1
            _maybe_log(store, _log_entry(
                {"message_id": str(msg_id)}, "error", "no_txt_attachment", None))
            continue

        summary["fetched"] += 1
        outlet_canonical = resolve_outlet(email_dict.get("outlet_code"))
        try:
            status, detail = process_email(
                store, email_dict, now_my=now_my, resolve_outlet=resolve_outlet)
        except Exception as exc:  # noqa: BLE001 - unexpected; record, leave unread
            logger.exception("Ingest failed for %r", email_dict.get("subject"))
            status, detail = "error", f"exception: {exc}"

        _maybe_log(store, _log_entry(email_dict, status, detail, outlet_canonical))

        if status in ("inserted", "skipped"):
            summary[status] += 1
            try:
                mailbox.mark_seen(msg_id)
            except Exception:  # noqa: BLE001
                logger.warning("Could not mark %r seen", msg_id, exc_info=True)
        else:
            summary["errors"] += 1
    return summary


# --- convenience entry point -------------------------------------------------

def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def run_ingest_once(client=None, *, now_my=None, since=None) -> dict:
    """Connect to the inbox, ingest all unread shift-close emails, return the
    summary. Used by the APScheduler job in bot.py and the manual command."""
    client = client or _build_client()
    now_my = now_my or datetime.now(MALAYSIA_TZ)
    store = SupabaseSalesStore(client)
    mailbox = Mailbox.connect()
    try:
        summary = run(store=store, mailbox=mailbox, now_my=now_my, since=since)
    finally:
        mailbox.close()
    logger.info("Sales ingest: %s", summary)
    return summary
