"""Director Q&A: a plain question in the director chat becomes one SELECT.

"chicken orders sek 7 this week", "which outlet had the most no-replies
last month", "bills from Bestari in September" — the AI provider
(``staff_ai``) is given the table catalogue below and asked for a single
SELECT. The hard rules live HERE, in code, not in the prompt:

  * exactly one statement, and it must start with SELECT — no WITH, no
    INTO, no FOR UPDATE, no data-modifying or administrative words at all;
  * comments are stripped before the checks, so nothing hides in them;
  * ``LIMIT 200`` is enforced (added, or lowered when larger);
  * it runs through ``director_sql(q)`` (migrations/0055): a SECURITY
    DEFINER function that switches to a role with SELECT only, sets the
    transaction read-only and a 5-second statement timeout;
  * every question, SQL and row count is logged (``director_sql_log``).

The result is a short table plus a one-line English answer the provider
writes from the rows; a number in that line that is not in the rows drops
the line. Off unless ``DIRECTOR_QA=on``. Only the director chat and the
reviewers' private chats ever reach this.

Pure except for the two callables handed in: ``complete`` (the provider)
and ``run_sql`` (the database). bot.py owns both.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import staff_ai
import staff_chat

logger = logging.getLogger(__name__)

LOG_TABLE = "director_sql_log"
RPC = "director_sql"
MAX_ROWS = 200
SHOW_ROWS = 15
MAX_SQL_CHARS = 2000
TIMEOUT_SECONDS = 5

# What the provider is allowed to know about. Column lists come from the
# migrations; keep this in step when a table changes.
SCHEMA = {
    "receipts": ("id, created_at, chat_id, message_id, merchant, outlet, receipt_date (date), "
                 "total (numeric, RM), currency, items (json), receipt_type "
                 "(SUPPLIER_PURCHASE|STAFF_ADVANCE|PETTY_CASH|UTILITY|UNKNOWN...), "
                 "verification_status, confidence, image_url, po_mismatch (json), po_explanation",
                 "every bill photo uploaded; one row per receipt"),
    "item_prices": ("id, receipt_id, receipt_date (date), outlet_code, chat_id, merchant, "
                    "canonical_item (ayam, ikan, sotong ...), raw_item_name, qty, unit_price, "
                    "line_total, created_at",
                    "one row per receipt line with a price; the buying history"),
    "staff_order_items": ("id, created_at, outlet_code, order_for (date), raw_item, "
                          "canonical_item, qty, unit, cashier, thread_id, reply_text, "
                          "source (reply|confirmed)",
                          "what cashiers ordered / confirmed for a day (the order history)"),
    "order_drafts": ("id, outlet, supplier, item, qty, pack, due_date (date), cadence, flags, "
                     "status, created_at", "generated order drafts per outlet and day"),
    "staff_chat_thread": ("id, created_at, outlet_code, chat_id, slot (open|stock|cook|lunch|"
                          "order|bills|night|invoice|minimarket|po_mismatch|...), status "
                          "(queued|open|reminded|answered|no_reply|dropped|info), question_text, "
                          "question_en, facts (json), language, cashier, asked_at, answered_at, "
                          "reply_text, reply_en, reply_status, reply_clear, shift, shift_date, "
                          "nudge_count, answer_source",
                          "every check-in question sent to an outlet group and its answer"),
    "staff_chat_log": ("id, created_at, kind (checkin|nudge|digest|anomaly|voice), mode, slot, "
                       "outlet_code, language, facts (json), final_text, source (ai|template), "
                       "problems (json), provider, model, tokens_in, tokens_out",
                       "every message the AI wrote or fell back on"),
    "staff_issues": ("id, created_at, ts, outlet_code, type (equipment|staff|supplier|customer|"
                     "cash|other), summary_en, urgent, raw_reply, resolved_at",
                     "problems staff reported in replies"),
    "staff_bill_handins": ("id, created_at, outlet_code, supplier, last_bill, days_missing, cashier",
                           "paper bills handed to the boss instead of uploaded"),
    "staff_advances": ("id, receipt_id, outlet, staff_name, amount, advance_date, repaid, "
                       "repaid_date, repaid_method, notes", "staff cash advances"),
    "sales_daily_summary": ("id, outlet_code (D-<CODE>), business_date (date), day_sales, net_sales, "
                            "cash_payment, customers, average_spent, take_away, dine_in",
                            "POS full-day sales per outlet"),
    "sales_daily_itemwise": ("id, summary_id -> sales_daily_summary.id, category, item_name, qty, "
                             "amount", "POS items sold per full day"),
    "sales_daily": ("id, outlet_code (S-<CODE>), shift_type (day|overnight), shift_business_date, "
                    "cashier, gross_sales, net_sales, total_sales, shift_open_at, shift_close_at",
                    "POS per-shift reports"),
    "kitchen_daily_usage": ("id, outlet_code, business_date, item_code, item_label, unit, "
                            "cooked_qty, left_qty, used_qty, pos_qty, mismatch_flag",
                            "kitchen cooked / left / sold per dish per day"),
    "kitchen_demand_forecast": ("id, outlet_code, business_date, item_code, unit, recommend_qty, "
                                "usual_cooked, action (CUT|RAISE|HOLD), actual_qty, pct_error",
                                "cook-plan forecast per dish"),
    "outlet_managers": ("id, outlet_code, manager_name, chat_id", "outlet groups / managers"),
    "cashier_names": ("outlet_code, shift (morning|night), name, language", "who is on shift"),
}

OUTLET_NOTE = ("Outlet codes: BISTRO7 (Bistro 7), SEK7, SEK14, SEK15, SEK20, SEK6, KLANG, SBESI "
               "(Sungai Besi), DAMANSARA (D.U; item_prices may say 'D'), VISTA. Sales tables "
               "prefix the code: D-SEK7 (full day), S-SEK7 (shift). 'chicken' = ayam, 'fish' = "
               "ikan, 'squid' = sotong, 'prawn' = udang, 'egg' = telur, 'mutton' = kambing, "
               "'beef' = daging.")


def enabled() -> bool:
    return (os.environ.get("DIRECTOR_QA") or "").strip().lower() in ("on", "1", "true", "yes")


def schema_text() -> str:
    return "\n".join(f"- {t}({cols}) — {what}" for t, (cols, what) in SCHEMA.items())


SYSTEM_PROMPT = (
    "You turn a restaurant director's question into ONE PostgreSQL SELECT over "
    "the tables below, for a Supabase (Postgres) database in Malaysia (dates are "
    "Asia/Kuala_Lumpur; use current_date for 'today').\n"
    "Rules:\n"
    "- Exactly one SELECT statement. Never INSERT, UPDATE, DELETE, DROP, ALTER, "
    "CREATE, WITH, INTO or any other statement. No semicolon.\n"
    "- Only the tables and columns listed. Aggregate when the question is a "
    "total or a count; otherwise return the most useful few columns.\n"
    "- Sort sensibly and add LIMIT 200 or less.\n"
    "- 'this week' = since the last Monday; 'last month' = the previous calendar "
    "month; 'today' = current_date.\n"
    "- If the question cannot be answered from these tables, return sql null "
    "with a short reason.\n"
    f"{OUTLET_NOTE}\n"
    "Tables:\n" + schema_text() + "\n"
    'Reply with JSON only: {"sql": "<one SELECT or null>", "note": "<what it returns, '
    'one short line>"}'
)

ANSWER_PROMPT = (
    "A restaurant director asked a question; a database query returned the rows "
    "given (JSON). Write ONE short plain-English sentence answering the question "
    "from those rows only. Every number you write must appear in the rows exactly "
    "(no totals or averages you compute yourself, no percentages, no guesses). If "
    "there are no rows, say that nothing matched.\n"
    'Reply with JSON only: {"answer": "<one sentence>"}'
)

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|into|execute|"
    r"call|do|lock|vacuum|analyze|analyse|refresh|set|reset|listen|notify|unlisten|"
    r"begin|commit|rollback|savepoint|prepare|deallocate|declare|fetch|move|cluster|"
    r"reindex|comment|security|with|returning|pg_sleep|pg_read_file|pg_read_binary_file|"
    r"pg_ls_dir|pg_stat_file|dblink|lo_import|lo_export|lo_get|pg_terminate_backend|"
    r"pg_cancel_backend|set_config|current_setting|pg_catalog|information_schema|"
    r"pg_shadow|pg_authid|auth|storage|vault|extensions)\b",
    re.IGNORECASE,
)
_FOR_LOCK = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", re.IGNORECASE)
_LIMIT = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_LIMIT_ALL = re.compile(r"\blimit\s+(all|null)\b", re.IGNORECASE)
_OFFSET = re.compile(r"\boffset\s+\d+\b", re.IGNORECASE)


def sanitize(sql) -> tuple[str | None, str]:
    """``(safe_sql, reason)``: the statement the database may run, or None
    and why not. Comments are stripped; a trailing semicolon is tolerated;
    ``LIMIT 200`` is enforced."""
    text = str(sql or "")
    text = _COMMENT_BLOCK.sub(" ", text)
    text = _COMMENT_LINE.sub(" ", text)
    text = " ".join(text.split()).strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        return None, "empty"
    if len(text) > MAX_SQL_CHARS:
        return None, "too long"
    if ";" in text:
        return None, "more than one statement"
    if "\x00" in text or "$$" in text:
        return None, "bad characters"
    if not re.match(r"^select\b", text, re.IGNORECASE):
        return None, "not a SELECT"
    if _FORBIDDEN.search(text):
        return None, f"forbidden word: {_FORBIDDEN.search(text).group(1).lower()}"
    if _FOR_LOCK.search(text):
        return None, "row locks not allowed"
    if _LIMIT_ALL.search(text):
        text = _LIMIT_ALL.sub(f"LIMIT {MAX_ROWS}", text)
    m = _LIMIT.search(text)
    if m:
        if int(m.group(1)) > MAX_ROWS:
            text = text[:m.start()] + f"LIMIT {MAX_ROWS}" + text[m.end():]
    else:
        off = _OFFSET.search(text)
        if off:
            text = text[:off.start()] + f"LIMIT {MAX_ROWS} " + text[off.start():]
        else:
            text = f"{text} LIMIT {MAX_ROWS}"
    return text, ""


def looks_like_question(text) -> bool:
    """In the director GROUP only questions are answered, so chatter between
    the owners is left alone: a question mark, or a question word up front."""
    t = str(text or "").strip().lower()
    if not t:
        return False
    if "?" in t:
        return True
    return bool(re.match(
        r"^(how|what|which|when|where|who|why|show|list|count|total|berapa|bila|mana|"
        r"siapa|apa|kenapa|tunjuk|senarai|berapakah|how many|how much|top|sum|average|"
        r"avg|compare|give me|any)\b", t))


def generate(question: str, complete) -> dict:
    """Ask the provider for the SELECT. ``{sql, note, raw, error}``."""
    try:
        result = complete(SYSTEM_PROMPT, json.dumps({"question": question}, ensure_ascii=False))
    except Exception:
        logger.exception("director sql: provider failed")
        result = None
    if not result:
        return {"sql": None, "note": "", "raw": None, "error": "ai unavailable"}
    data = result.get("data") or {}
    raw = data.get("sql")
    if not raw:
        return {"sql": None, "note": str(data.get("note") or ""), "raw": None,
                "error": str(data.get("note") or data.get("reason") or "cannot answer from the tables")}
    safe, reason = sanitize(raw)
    if safe is None:
        return {"sql": None, "note": str(data.get("note") or ""), "raw": str(raw),
                "error": f"rejected: {reason}"}
    return {"sql": safe, "note": str(data.get("note") or ""), "raw": str(raw), "error": ""}


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers_in_rows(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    for r in rows or []:
        for v in (r.values() if isinstance(r, dict) else [r]):
            s = str(v)
            for n in _NUM.findall(s):
                out.add(staff_chat._norm_num(n))
                try:
                    f = float(n.replace(",", "."))
                    out.add(f"{round(f):g}")
                    out.add(f"{f:.2f}")
                    out.add(f"{f:.1f}")
                except ValueError:
                    pass
    return out


def check_answer(answer: str, rows: list[dict], question: str = "") -> list[str]:
    """Numbers in the sentence that are not in the rows, the row count or the
    question itself ("Sek 7", "last 30 days")."""
    allowed = _numbers_in_rows(rows) | {str(len(rows or []))}
    allowed |= {staff_chat._norm_num(n) for n in _NUM.findall(str(question or ""))}
    bad = []
    for n in _NUM.findall(str(answer or "")):
        norm = staff_chat._norm_num(n)
        try:
            alt = {norm, f"{float(n.replace(',', '.')):.2f}", f"{round(float(n.replace(',', '.'))):g}"}
        except ValueError:
            alt = {norm}
        if not (alt & allowed):
            bad.append(n)
    return bad


def answer_line(question: str, rows: list[dict], complete) -> tuple[str, list[str]]:
    """One English sentence from the rows, or a plain count when the
    provider fails or invents a number."""
    plain = ("Nothing matched." if not rows else
             f"{len(rows)} row{'s' if len(rows) != 1 else ''} matched.")
    try:
        result = complete(ANSWER_PROMPT, json.dumps(
            {"question": question, "rows": rows[:SHOW_ROWS], "row_count": len(rows)},
            ensure_ascii=False, default=str))
    except Exception:
        logger.exception("director sql: answer failed")
        result = None
    text = str(((result or {}).get("data") or {}).get("answer") or "").strip()
    if not text:
        return plain, ["ai unavailable"]
    bad = check_answer(text, rows, question)
    if bad or len(text) > 300 or staff_chat._MONEY.search(text.replace("RM", "")):
        return plain, [f"number {n} not in rows" for n in bad] or ["answer rejected"]
    return text, []


def _cell(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}" if v == int(v) else f"{v:.2f}"
    s = str(v)
    return s if len(s) <= 24 else s[:23] + "…"


def format_rows(rows: list[dict], limit: int = SHOW_ROWS) -> str:
    """A compact text table for a phone: header, up to ``limit`` rows."""
    if not rows:
        return ""
    cols = list(rows[0].keys()) if isinstance(rows[0], dict) else ["value"]
    cols = cols[:6]
    body = [[_cell(r.get(c) if isinstance(r, dict) else r) for c in cols] for r in rows[:limit]]
    widths = [max(len(c), *(len(b[i]) for b in body)) for i, c in enumerate(cols)]
    lines = [" | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))]
    lines += [" | ".join(b[i].ljust(widths[i]) for i in range(len(cols))) for b in body]
    if len(rows) > limit:
        lines.append(f"… {len(rows) - limit} more row(s)")
    return "\n".join(lines)


def run(question: str, *, complete=None, run_sql) -> dict:
    """The whole answer: ``{text, sql, row_count, ok, error, ms, answer}``.
    ``run_sql(sql) -> list[dict]`` is the read-only executor. Never raises."""
    complete = complete or staff_ai.complete_json
    started = time.monotonic()
    gen = generate(question, complete)
    out = {"question": question, "sql": gen["sql"], "raw_sql": gen["raw"], "row_count": 0,
           "ok": False, "error": gen["error"], "answer": "", "ms": 0, "text": ""}
    if not gen["sql"]:
        out["text"] = (f"I couldn't turn that into a safe query ({gen['error']})."
                       if gen["error"] else "I couldn't turn that into a query.")
        out["ms"] = int((time.monotonic() - started) * 1000)
        return out
    try:
        rows = run_sql(gen["sql"]) or []
        if isinstance(rows, dict):
            rows = [rows]
        rows = [r for r in rows if isinstance(r, dict)][:MAX_ROWS]
    except Exception as exc:
        logger.exception("director sql: query failed")
        out.update(error=f"query failed: {str(exc)[:160]}",
                   text="The query failed (a timeout or a bad column). Try asking differently.",
                   ms=int((time.monotonic() - started) * 1000))
        return out
    answer, problems = answer_line(question, rows, complete)
    table = format_rows(rows)
    parts = [answer]
    if table:
        parts.append(table)
    parts.append(f"SQL: {gen['sql'][:300]}")
    out.update(ok=True, row_count=len(rows), answer=answer, problems=problems,
               text="\n\n".join(parts), ms=int((time.monotonic() - started) * 1000))
    return out


def log_row(result: dict, *, chat_id, user_id) -> dict:
    return {
        "chat_id": chat_id, "user_id": user_id,
        "question": str(result.get("question") or "")[:1000],
        "sql": result.get("sql"), "raw_sql": (result.get("raw_sql") or "")[:2000] or None,
        "row_count": result.get("row_count") or 0, "ok": bool(result.get("ok")),
        "error": (result.get("error") or None), "answer": (result.get("answer") or None),
        "ms": result.get("ms") or 0,
    }
