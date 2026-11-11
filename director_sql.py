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
    DEFINER function owned by a role with SELECT only, which also sets the
    transaction read-only and a 5-second statement timeout;
  * every question, SQL and row count is logged (``director_sql_log``).

The result is a short answer the provider writes from the rows, in the
language the question was asked in, plus a table when there is more than one
row. A number in that answer that is not in the rows (or in the totals the
CODE computed from them) drops it. Off unless ``DIRECTOR_QA=on``. Only the
director chat and the reviewers' private chats ever reach this.

It is meant to feel like asking a person who knows the books:

  * it remembers the conversation — "and last month?", "sek 14 pula?",
    "ikut outlet" lean on the last few questions of that chat (kept in
    memory for ``MEMORY_TTL_S``, never written anywhere);
  * a query the database refuses (a wrong column, a rejected statement) is
    handed back to the provider once with the error, and the corrected one
    is run — instead of telling the director to "try asking differently";
  * when a wrong guess would mislead it asks ONE short question back and
    reads the next message as the answer to it;
  * totals, averages, lowest and highest are computed here, in code, and
    handed to the provider, so "RM 1,240 in total" is allowed and correct;
  * "hi", "thanks", "what can you do" get one friendly line (no figures);
  * the SQL stays out of the reply unless ``DIRECTOR_QA_SHOW_SQL=on`` or
    the director types "sql" after an answer.

None of this loosens a rule above: every statement still goes through
``sanitize`` and the read-only function, and every answer through the
number check.

Pure except for the two callables handed in: ``complete`` (the provider)
and ``run_sql`` (the database). bot.py owns both.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
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

# The conversation: the last few questions of a chat, so a follow-up can
# lean on them. In memory only — a restart forgets, which is fine.
MEMORY_TURNS = 4
MEMORY_TTL_S = 30 * 60
# How long after an answer a bare "and sek 14?" still reads as a follow-up
# in the director GROUP (where everything else is the owners' own chatter).
FOLLOWUP_S = 5 * 60
MAX_ANSWER_CHARS = 400
MAX_ASIDE_CHARS = 240

KIND_ANSWER = "answer"
KIND_CLARIFY = "clarify"
KIND_CHAT = "chat"
KIND_FAILED = "failed"

CAPABILITIES = ("Ask me anything about the bills, prices, orders, POS sales, kitchen usage "
                "or the staff check-ins — in English or Malay. For example: "
                "\"sales semalam ikut outlet\", \"which outlet had the most no-replies this "
                "week\", \"bil Bestari bulan ni\". You can follow up with \"and last month?\".")
CLARIFY_FALLBACK = "Can you be more specific — which outlet, item or period do you mean?"

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
    "- 'this week' = since the last Monday; 'last week' = the Monday-to-Sunday "
    "week before this one; 'last month' = the previous calendar month; 'today' "
    "= current_date.\n"
    "- If the question cannot be answered from these tables, return sql null "
    "with a short reason.\n"
    "- Questions come in English, Malay or a mix: 'semalam' = yesterday, "
    "'minggu ni' = this week, 'minggu lepas' = last week, 'bulan ni' = this "
    "month, 'bulan lepas' = last month, 'berapa' = how many / how much, "
    "'ikut outlet' = per outlet, 'bil' / 'resit' = receipts, 'jualan' = sales.\n"
    "- When a bare number would say little ('how are sales today'), return the "
    "useful breakdown in the same query (per outlet, or per day) instead.\n"
    "- 'conversation' (when given) lists the earlier questions in this chat, "
    "oldest first, with the SQL that answered them. When the new question "
    "leans on them ('and last month?', 'sek 14 pula', 'ikut outlet', 'why?'), "
    "keep the same subject, filters and period and change only what the new "
    "question changes. An entry with 'you_asked' means you asked the director "
    "that question: the new message is the reply — combine it with that "
    "entry's question. If the new question is unrelated, ignore the conversation.\n"
    "- 'failed_attempt' (when given) is your earlier SQL for this question and "
    "the error it got. Write a corrected query: only listed columns, one SELECT.\n"
    "- Prefer a sensible default (all outlets, the last 30 days) over asking. "
    "Only when a wrong guess would mislead (no way to tell which item, outlet "
    "or what is meant at all), return sql null and 'clarify': ONE short "
    "question in the director's language.\n"
    "- If the message is a greeting, a thank-you or asks what you can do, "
    "return sql null and 'chat': one short friendly line in their language, "
    "with no figures in it.\n"
    f"{OUTLET_NOTE}\n"
    "Tables:\n" + schema_text() + "\n"
    'Reply with JSON only: {"sql": "<one SELECT or null>", "note": "<what it returns, '
    'one short line>", "clarify": "<a question back, or null>", "chat": "<a friendly '
    'line, or null>"}'
)

ANSWER_PROMPT = (
    "A restaurant director asked a question; a database query returned the rows "
    "given (JSON). Answer the way a sharp operations manager would text the boss: "
    "the direct answer first, then at most ONE more short sentence with the most "
    "useful thing in the rows (the highest, the lowest, an odd one out) if there "
    "is one. No greeting, no filler, no advice.\n"
    "Write in the language of the question: English for English, casual Malay "
    "for Malay or a Malay-English mix.\n"
    "Every number you write must appear exactly in the rows or in 'stats' "
    "('stats', when given, holds the sum, avg, min and max of each numeric "
    "column over ALL the returned rows — use those for totals and averages; "
    "never compute a number yourself). No percentages, no guesses. 'row_count' "
    "is how many rows came back; you see only the first few. 'conversation' "
    "(when given) is what was asked just before, for context only. If there are "
    "no rows, say that nothing matched.\n"
    'Reply with JSON only: {"answer": "<one or two short sentences>"}'
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
        r"avg|compare|give me|any)\b", t)
        or re.search(r"\b(berapa|how many|how much|macam mana)\b", t))


_FOLLOWUP_HEAD = re.compile(
    r"^(and|dan|how about|what about|macam mana dengan|kalau|same for|sama untuk|why|"
    r"kenapa|by outlet|ikut outlet|per outlet)\b")
_FOLLOWUP_TAIL = re.compile(r"\b(pula|pulak|instead)\W*$")


def looks_like_followup(text) -> bool:
    """Short and shaped like a continuation: "and last month?", "sek 14
    pula", "ikut outlet". Only meaningful right after an answer."""
    t = str(text or "").strip().lower()
    if not t or len(t) > 60:
        return False
    return bool(_FOLLOWUP_HEAD.match(t) or _FOLLOWUP_TAIL.search(t))


# --- the conversation ---------------------------------------------------------

_memory: dict = {}
_memory_lock = threading.Lock()


def forget(chat_id=None) -> None:
    with _memory_lock:
        if chat_id is None:
            _memory.clear()
        else:
            _memory.pop(chat_id, None)


def recall(chat_id, now: float | None = None) -> list[dict]:
    """The chat's recent turns, oldest first; expired ones are dropped."""
    if chat_id is None:
        return []
    now = time.time() if now is None else now
    with _memory_lock:
        turns = [t for t in _memory.get(chat_id, []) if now - t["at"] <= MEMORY_TTL_S]
        if turns:
            _memory[chat_id] = turns
        else:
            _memory.pop(chat_id, None)
        return list(turns)


def remember(chat_id, result: dict, *, user_id=None, now: float | None = None) -> None:
    """Keep an answered question (or a question we asked back). Failures and
    pleasantries teach the next question nothing, so they are not kept."""
    if chat_id is None or result.get("kind") not in (KIND_ANSWER, KIND_CLARIFY):
        return
    now = time.time() if now is None else now
    question = str(result.get("question") or "")[:300]
    with _memory_lock:
        turns = _memory.setdefault(chat_id, [])
        if result["kind"] == KIND_CLARIFY and turns and turns[-1]["kind"] == KIND_CLARIFY:
            # A second question back on the same thread: keep the original ask.
            question = f"{turns[-1]['question']} — {question}"[:300]
            turns.pop()
        turns.append({
            "at": now, "user_id": user_id, "kind": result["kind"], "question": question,
            "sql": result.get("sql"), "note": str(result.get("note") or "")[:200],
            "answer": str(result.get("answer") or "")[:300],
        })
        del turns[:-MEMORY_TURNS]


def expects_followup(chat_id, user_id, text, now: float | None = None) -> bool:
    """Is this message the next line of a conversation with the bot? True
    when the same person was just answered and the text is shaped like a
    continuation, or when the bot asked them a question and this is the
    reply. The group's ordinary chatter never qualifies."""
    now = time.time() if now is None else now
    turns = recall(chat_id, now)
    if not turns:
        return False
    last = turns[-1]
    if now - last["at"] > FOLLOWUP_S:
        return False
    if last["user_id"] is not None and user_id is not None and last["user_id"] != user_id:
        return False
    return last["kind"] == KIND_CLARIFY or looks_like_followup(text)


def _conversation(history: list[dict]) -> list[dict]:
    out = []
    for t in history or []:
        if t.get("kind") == KIND_CLARIFY:
            out.append({"question": t.get("question"), "you_asked": t.get("answer")})
        else:
            out.append({"question": t.get("question"), "sql": t.get("sql"),
                        "answer": t.get("answer")})
    return out


_SHOW_SQL_ASK = re.compile(r"^(show |tunjuk |the )?(sql|query)\W*$", re.IGNORECASE)


def show_sql() -> bool:
    return (os.environ.get("DIRECTOR_QA_SHOW_SQL") or "").strip().lower() in (
        "on", "1", "true", "yes")


def _aside(text, question: str, fallback: str) -> str:
    """A line the provider wrote that is not backed by rows (a question back,
    a pleasantry): short, no money, and no number the director did not type."""
    line = " ".join(str(text or "").split())
    if not line:
        return ""
    asked = {staff_chat._norm_num(n) for n in _NUM.findall(str(question or ""))}
    if (len(line) > MAX_ASIDE_CHARS or staff_chat._MONEY.search(line)
            or any(staff_chat._norm_num(n) not in asked for n in _NUM.findall(line))):
        return fallback
    return line


def generate(question: str, complete, history: list[dict] | None = None,
             failed: dict | None = None) -> dict:
    """Ask the provider for the SELECT. ``{sql, note, raw, error, clarify,
    chat}``. ``history`` is the chat's recent turns (``recall``); ``failed``
    is ``{sql, error}`` from an attempt the guard or the database refused."""
    payload: dict = {"question": question}
    if history:
        payload["conversation"] = _conversation(history)
    if failed:
        payload["failed_attempt"] = failed
    try:
        result = complete(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        logger.exception("director sql: provider failed")
        result = None
    out = {"sql": None, "note": "", "raw": None, "error": "", "clarify": "", "chat": ""}
    if not result:
        out["error"] = "ai unavailable"
        return out
    data = result.get("data") or {}
    out["note"] = str(data.get("note") or "")
    raw = data.get("sql")
    if not raw:
        out["clarify"] = _aside(data.get("clarify"), question, CLARIFY_FALLBACK)
        if not out["clarify"]:
            out["chat"] = _aside(data.get("chat"), question, CAPABILITIES)
        out["error"] = str(data.get("note") or data.get("reason")
                           or "cannot answer from the tables")
        return out
    out["raw"] = str(raw)
    safe, reason = sanitize(raw)
    if safe is None:
        out["error"] = f"rejected: {reason}"
        return out
    out["sql"] = safe
    return out


_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


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


def column_stats(rows: list[dict]) -> dict:
    """``{column: {sum, avg, min, max}}`` for the numeric columns, computed
    here so the provider never does arithmetic. Empty for a single row, and
    for a result that hit ``MAX_ROWS`` (it may be cut off — a total of part
    of the data would be a wrong total). Id columns are skipped."""
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if not (2 <= len(rows) < MAX_ROWS):
        return {}
    stats: dict = {}
    for col in rows[0].keys():
        name = str(col).lower()
        if name == "id" or name.endswith("_id"):
            continue
        values = [r.get(col) for r in rows if r.get(col) is not None]
        if len(values) < 2 or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            continue
        total = float(sum(values))
        stats[str(col)] = {"sum": round(total, 2), "avg": round(total / len(values), 2),
                           "min": min(values), "max": max(values)}
        if len(stats) >= 6:
            break
    return stats


def check_answer(answer: str, rows: list[dict], question: str = "",
                 stats: dict | None = None) -> list[str]:
    """Numbers in the sentence that are not in the rows, the row count, the
    code-computed ``stats`` or the question itself ("Sek 7", "last 30 days")."""
    allowed = _numbers_in_rows(rows) | {str(len(rows or []))}
    allowed |= _numbers_in_rows(list((stats or {}).values()))
    allowed |= {staff_chat._norm_num(n) for n in _NUM.findall(_THOUSANDS.sub("", str(question or "")))}
    bad = []
    # "12,250.75" is one number, not "12,250" and "75".
    for n in _NUM.findall(_THOUSANDS.sub("", str(answer or ""))):
        norm = staff_chat._norm_num(n)
        try:
            alt = {norm, f"{float(n.replace(',', '.')):.2f}", f"{round(float(n.replace(',', '.'))):g}"}
        except ValueError:
            alt = {norm}
        if not (alt & allowed):
            bad.append(n)
    return bad


def answer_line(question: str, rows: list[dict], complete,
                history: list[dict] | None = None) -> tuple[str, list[str]]:
    """The short answer written from the rows, or a plain count when the
    provider fails or invents a number."""
    plain = ("Nothing matched." if not rows else
             f"{len(rows)} row{'s' if len(rows) != 1 else ''} matched.")
    stats = column_stats(rows)
    payload: dict = {"question": question, "rows": rows[:SHOW_ROWS], "row_count": len(rows)}
    if stats:
        payload["stats"] = stats
    if history:
        payload["conversation"] = [{"question": t.get("question"), "answer": t.get("answer")}
                                   for t in history[-2:]]
    try:
        result = complete(ANSWER_PROMPT, json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        logger.exception("director sql: answer failed")
        result = None
    text = " ".join(str(((result or {}).get("data") or {}).get("answer") or "").split())
    if not text:
        return plain, ["ai unavailable"]
    bad = check_answer(text, rows, question, stats)
    if bad or len(text) > MAX_ANSWER_CHARS or staff_chat._MONEY.search(text.replace("RM", "")):
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


def _fetch(run_sql, sql: str) -> list[dict]:
    rows = run_sql(sql) or []
    if isinstance(rows, dict):
        rows = [rows]
    return [r for r in rows if isinstance(r, dict)][:MAX_ROWS]


def run(question: str, *, complete=None, run_sql, chat_id=None, user_id=None,
        now: float | None = None) -> dict:
    """The whole answer: ``{text, kind, sql, row_count, ok, error, ms, answer,
    retried}``. ``run_sql(sql) -> list[dict]`` is the read-only executor.
    With a ``chat_id`` the chat's recent questions are used as context and
    this one is remembered. Never raises."""
    complete = complete or staff_ai.complete_json
    started = time.monotonic()
    history = recall(chat_id, now)
    out = {"question": question, "sql": None, "raw_sql": None, "row_count": 0, "ok": False,
           "error": "", "answer": "", "note": "", "ms": 0, "text": "", "kind": KIND_FAILED,
           "retried": False}

    def done() -> dict:
        out["ms"] = int((time.monotonic() - started) * 1000)
        try:
            remember(chat_id, out, user_id=user_id, now=now)
        except Exception:
            logger.exception("director sql: remember failed")
        return out

    # "sql" after an answer shows the query behind it — no provider call.
    if _SHOW_SQL_ASK.match(question.strip()):
        last = next((t for t in reversed(history) if t.get("sql")), None)
        out.update(kind=KIND_CHAT, error=KIND_CHAT,
                   text=(f"SQL: {last['sql']}" if last else "There is no query to show yet."))
        return done()

    gen = generate(question, complete, history)
    rows, db_error, tried = None, "", None
    for attempt in (1, 2):
        if gen["sql"]:
            if gen["sql"] == tried:
                break                       # the same statement would fail the same way
            try:
                rows = _fetch(run_sql, gen["sql"])
                break
            except Exception as exc:
                logger.warning("director sql: query failed (attempt %d): %s", attempt, exc)
                db_error = f"query failed: {str(exc)[:160]}"
                tried = gen["sql"]
                failed = {"sql": gen["sql"], "error": str(exc)[:300]}
        elif gen["error"].startswith("rejected:") and gen["raw"]:
            failed = {"sql": gen["raw"][:MAX_SQL_CHARS], "error": gen["error"]}
        else:
            break
        if attempt == 2:
            break
        # One repair: hand the provider its statement and what went wrong.
        gen = generate(question, complete, history, failed)
        out["retried"] = True

    out.update(sql=gen["sql"], raw_sql=gen["raw"], note=gen["note"])
    if rows is None:
        if not gen["sql"] and gen["clarify"]:
            out.update(kind=KIND_CLARIFY, error=KIND_CLARIFY, answer=gen["clarify"],
                       text=gen["clarify"])
        elif not gen["sql"] and gen["chat"]:
            out.update(kind=KIND_CHAT, error=KIND_CHAT, answer=gen["chat"], text=gen["chat"])
        elif db_error:
            out.update(error=db_error, text=(
                "I tried that two ways and the query failed both times (a timeout or data "
                "I don't have). Can you narrow it — one outlet, or a shorter period?"))
        elif gen["error"] == "ai unavailable":
            out.update(error=gen["error"],
                       text="I can't reach the AI service right now. Try again in a minute.")
        elif gen["error"].startswith("rejected:"):
            out.update(error=gen["error"],
                       text=f"I couldn't turn that into a safe query ({gen['error']}).")
        else:
            out.update(error=gen["error"], text=(
                f"I can't answer that from the data I have ({gen['error']})."
                if gen["error"] else "I couldn't turn that into a query."))
        return done()

    answer, problems = answer_line(question, rows, complete, history)
    parts = [answer]
    # One row the sentence already covers needs no table under it.
    if rows and (len(rows) > 1 or problems):
        parts.append(format_rows(rows))
    if not rows and gen["note"]:
        parts.append(f"(I looked for: {gen['note'][:200]})")
    if show_sql():
        parts.append(f"SQL: {gen['sql'][:300]}")
    out.update(ok=True, kind=KIND_ANSWER, error="", row_count=len(rows), answer=answer,
               problems=problems, text="\n\n".join(parts))
    return done()


def log_row(result: dict, *, chat_id, user_id) -> dict:
    return {
        "chat_id": chat_id, "user_id": user_id,
        "question": str(result.get("question") or "")[:1000],
        "sql": result.get("sql"), "raw_sql": (result.get("raw_sql") or "")[:2000] or None,
        "row_count": result.get("row_count") or 0, "ok": bool(result.get("ok")),
        "error": (result.get("error") or None), "answer": (result.get("answer") or None),
        "ms": result.get("ms") or 0,
    }
