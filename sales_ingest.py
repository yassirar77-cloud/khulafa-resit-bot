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

from sales_daily_parser import parse_daily_summary
from sales_email_fetcher import Mailbox, extract_shift_close
from sales_parser import parse_shift_close

logger = logging.getLogger(__name__)

MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")

SALES_DAILY_TABLE = "sales_daily"
SALES_INGEST_LOG_TABLE = "sales_ingest_log"
OUTLET_CANONICAL_TABLE = "outlet_canonical"
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

# D-file (daily summary) tables (PR #60).
DAILY_SUMMARY_TABLE = "sales_daily_summary"
DAILY_CHILD_TABLES = (
    "sales_daily_payouts",
    "sales_daily_deleted",
    "sales_daily_top_items",
    "sales_daily_itemwise",
    "sales_daily_shift_breakdown",
)


# --- record building (pure) --------------------------------------------------

def _strip_nulls(value):
    """Remove NUL (U+0000) chars from a string; Postgres TEXT rejects them.
    Primary stripping happens in the parser, this is a defensive backstop."""
    return value.replace("\x00", "") if isinstance(value, str) else value


def _sanitize_record(record):
    """Strip NUL bytes from every string in the parent row + child rows."""
    record["parent"] = {k: _strip_nulls(v) for k, v in record["parent"].items()}
    for rows in record["children"].values():
        for row in rows:
            for k, v in row.items():
                row[k] = _strip_nulls(v)
    return record


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
            {
                "method": p["method"],
                "amount": p["amount"],
                "transaction_id": p.get("transaction_id"),
                "transaction_at": _iso(p.get("transaction_at")),
            }
            for p in parsed.get("payments", [])
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

    return _sanitize_record({
        "parent": parent,
        "children": children,
        "key": (outlet, shift_no, business_date.isoformat(), shift_type),
    })


def _daily_top_rows(parsed):
    rows = []
    for ranking in ("top_30_food", "top_30_drinks", "top_20_combined"):
        for rank, it in enumerate(parsed.get(ranking, []), start=1):
            rows.append({
                "ranking": ranking, "rank": rank,
                "item_name": it["name"], "qty": it["qty"], "amount": it["amount"],
            })
    return rows


def build_daily_record(email_dict, parsed, outlet_canonical, business_date) -> dict:
    """Assemble the ``sales_daily_summary`` parent + its 5 child lists + the
    idempotency key (outlet_canonical, business_date) from a parsed D-file."""
    h = parsed.get("header", {})
    d = parsed.get("daily_aggregate", {})
    parent = {
        "outlet_canonical": outlet_canonical,
        "outlet_code": email_dict.get("outlet_code"),
        "business_date": business_date.isoformat(),
        "total_shifts": h.get("total_shifts"),
        "business_name": h.get("business_name"),
        "address": h.get("address"),
        "printed_at": _iso(h.get("printed_at")),
        "day_sales": d.get("day_sales") if d.get("day_sales") is not None else 0.0,
        "tax": d.get("tax") if d.get("tax") is not None else 0.0,
        "rounded": d.get("rounded"),
        "inactive_cr_sale": d.get("inactive_cr_sale"),
        "net_sales": d.get("net_sales"),
        "cash_payment": d.get("cash_payment"),
        "cash_in_draw": d.get("cash_in_draw"),
        "discount": d.get("discount"),
        "customers": d.get("customers"),
        "average_spent": d.get("average_spent"),
        "take_away": d.get("take_away"),
        "dine_in": d.get("dine_in"),
        "deleted_items_total": d.get("deleted_items_total"),
        "source_subject": email_dict.get("subject"),
        "source_message_id": email_dict.get("message_id"),
        "source_filename": email_dict.get("filename"),
        "received_at": _parse_received_at(email_dict.get("received_at")),
        "raw_content": email_dict.get("content"),
    }
    children = {
        "sales_daily_payouts": [
            {"shiftno": p["shiftno"], "description": p["description"],
             "vendor_name": p["vendor_name"], "amount": p["amount"]}
            for p in parsed.get("payouts", [])
        ],
        "sales_daily_deleted": [
            {"item_name": x["item_name"], "qty": x["qty"], "rate": x["rate"],
             "amount": x["amount"], "staff": x["staff"], "del_time": x["time"],
             "reason": x["reason"]}
            for x in parsed.get("deleted_items", [])
        ],
        "sales_daily_top_items": _daily_top_rows(parsed),
        "sales_daily_itemwise": [
            {"category": cat, "item_name": it["name"], "qty": it["qty"], "amount": it["amount"]}
            for cat, items in parsed.get("itemwise_sales", {}).items()
            for it in items
        ],
        "sales_daily_shift_breakdown": [
            {"shift_index": s["shift_index"], "shift_id": s["shift_id"], "sales": s["sales"],
             "net_sales": s["net_sales"], "cash_payment": s["cash_payment"],
             "cash_in_draw": s["cash_in_draw"], "customers": s["customers"],
             "average_spent": s["average_spent"], "take_away": s["take_away"],
             "dine_in": s["dine_in"], "deleted_items_total": s["deleted_items_total"]}
            for s in parsed.get("shifts", [])
        ],
    }
    return _sanitize_record({
        "parent": parent,
        "children": children,
        "key": (outlet_canonical, business_date.isoformat()),
    })


# --- DB store (Supabase) -----------------------------------------------------

class SupabaseSalesStore:
    """Persists shift records to Supabase and exposes the idempotency check.
    Tests substitute an in-memory fake with the same three methods."""

    def __init__(self, client):
        self.client = client

    def load_outlets(self) -> dict:
        """Load the outlet registry once: code -> {canonical_name, active,
        confirmed}. Codes are upper-cased to match the parsed subject codes."""
        resp = (
            self.client.table(OUTLET_CANONICAL_TABLE)
            .select("code, canonical_name, active, confirmed")
            .execute()
        )
        out: dict = {}
        for r in (resp.data or []):
            code = (r.get("code") or "").upper()
            if not code:
                continue
            out[code] = {
                "canonical_name": r.get("canonical_name"),
                "active": bool(r.get("active")),
                "confirmed": bool(r.get("confirmed")),
            }
        return out

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

    def exists_daily(self, outlet_canonical, business_date) -> bool:
        resp = (
            self.client.table(DAILY_SUMMARY_TABLE)
            .select("id")
            .eq("outlet_canonical", outlet_canonical)
            .eq("business_date", business_date)
            .limit(1)
            .execute()
        )
        return bool(resp.data)

    def save_daily(self, record) -> int:
        resp = self.client.table(DAILY_SUMMARY_TABLE).insert(record["parent"]).execute()
        summary_id = resp.data[0]["id"]
        for table in DAILY_CHILD_TABLES:
            rows = record["children"].get(table) or []
            if not rows:
                continue
            payload = [{**r, "summary_id": summary_id} for r in rows]
            self.client.table(table).insert(payload).execute()
        return summary_id

    def log(self, entry) -> None:
        try:
            self.client.table(SALES_INGEST_LOG_TABLE).insert(entry).execute()
        except Exception:  # noqa: BLE001 - logging must never break ingestion
            logger.warning("Could not write sales_ingest_log row", exc_info=True)


# --- per-email processing ----------------------------------------------------

def _canonical_code(outlet_code) -> str | None:
    """Strip the 2-char S-/D- prefix and re-prefix with S- so both email types
    resolve against the same outlet_canonical row (D-SEK20 -> S-SEK20)."""
    code = (outlet_code or "").upper()
    if len(code) <= 2:
        return None
    return "S-" + code[2:]


def _process_s_file(store, email_dict, canonical, now_my):
    parsed = parse_shift_close(email_dict["content"])
    if parsed.get("total_sales") is None:
        return "error", "no_total_parsed"
    record = build_sales_record(email_dict, parsed, canonical, now_my)
    if store.exists(*record["key"]):
        return "skipped", "duplicate"
    store.save(record)
    return "inserted", None


def _process_d_file(store, email_dict, canonical, now_my):
    parsed = parse_daily_summary(email_dict["content"])
    if parsed.get("daily_aggregate", {}).get("day_sales") is None:
        return "error", "no_day_sales_parsed"
    business_date = parsed.get("header", {}).get("business_date") or now_my.date()
    record = build_daily_record(email_dict, parsed, canonical, business_date)
    if store.exists_daily(canonical, business_date.isoformat()):
        return "skipped", "duplicate"
    store.save_daily(record)
    return "inserted", None


def process_email(store, email_dict, *, now_my, outlets):
    """Ingest one POS email, routed by type and gated by the ``outlet_canonical``
    registry (``outlets``: S-code -> {canonical_name, active, confirmed}).

    S-files -> sales_daily (per shift); D-files -> sales_daily_summary (per day).
    Both resolve the outlet by stripping the prefix and looking up the S-code.

    Returns ``(status, detail)``:
      * ``inserted`` / ``skipped`` (duplicate)
      * ``skipped_inactive`` — outlet active=false (partnership outlets)
      * ``skipped_unknown``  — unrecognised subject or outlet not in registry
      * ``error``            — empty attachment / unparseable
    """
    content = email_dict.get("content") or ""
    if not content.strip():
        return "error", "empty_attachment"

    email_type = email_dict.get("email_type")
    code = (email_dict.get("outlet_code") or "").upper() or None
    if not email_type or not code:
        return "skipped_unknown", "unrecognised subject"

    s_code = _canonical_code(code)
    info = outlets.get(s_code) if s_code else None
    if info is None:
        logger.warning("Outlet %r (%s-file) not in outlet_canonical — skipping", code, email_type)
        return "skipped_unknown", f"{code} not in outlet_canonical"
    if not info.get("active", False):
        logger.info("Outlet %r is inactive — skipping", code)
        return "skipped_inactive", f"{code} inactive"
    if not info.get("confirmed", True):
        logger.warning(
            "Outlet %r canonical name %r is unconfirmed — ingesting anyway",
            code, info.get("canonical_name"),
        )

    canonical = info.get("canonical_name") or s_code
    if email_type == "S":
        return _process_s_file(store, email_dict, canonical, now_my)
    return _process_d_file(store, email_dict, canonical, now_my)


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


# Statuses whose email we flag \Seen (terminal decisions). skipped_unknown and
# error are left UNREAD so they retry once the outlet is registered / fixed.
_MARK_SEEN_STATUSES = frozenset({"inserted", "skipped", "skipped_inactive"})
_COUNTER_BY_STATUS = {
    "inserted": "inserted",
    "skipped": "skipped",
    "skipped_inactive": "skipped_inactive",
    "skipped_unknown": "skipped_unknown",
    "error": "errors",
}


def run(*, store, mailbox, now_my, since=None) -> dict:
    """Drive the mailbox end-to-end. Returns a summary dict.

    The ``outlet_canonical`` registry is loaded ONCE per run (no per-email
    queries) and used to gate active/confirmed. A message is flagged ``\\Seen``
    only on inserted / duplicate / inactive; unknown-outlet and error messages
    stay unread for retry/inspection."""
    summary = {
        "fetched": 0, "inserted": 0, "skipped": 0,
        "skipped_inactive": 0, "skipped_unknown": 0, "errors": 0,
    }
    outlets = store.load_outlets()  # one query, reused for the whole batch
    # subject_token=None: fetch all unread POS mail (S- AND D-), classify in code.
    for msg_id in mailbox.search(since=since, subject_token=None):
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
        s_code = _canonical_code(email_dict.get("outlet_code"))
        outlet_name = (outlets.get(s_code) or {}).get("canonical_name") or email_dict.get("outlet_code")
        try:
            status, detail = process_email(store, email_dict, now_my=now_my, outlets=outlets)
        except Exception as exc:  # noqa: BLE001 - unexpected; record, leave unread
            logger.exception("Ingest failed for %r", email_dict.get("subject"))
            status, detail = "error", f"exception: {exc}"

        _maybe_log(store, _log_entry(email_dict, status, detail, outlet_name))
        summary[_COUNTER_BY_STATUS.get(status, "errors")] += 1
        if status in _MARK_SEEN_STATUSES:
            try:
                mailbox.mark_seen(msg_id)
            except Exception:  # noqa: BLE001
                logger.warning("Could not mark %r seen", msg_id, exc_info=True)
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
