"""Nightly director digest (23:30): the day's staff replies in plain English.

Everything the outlets told us — or didn't — today, in one message ordered
by how much it needs the director's attention:

  1. outlets that never replied to a check-in (and the unanswered ones)
  2. supplier bills asked about and still not uploaded
  3. orders the cashier changed from the draft
  4. anything flagged as an issue in a reply (staff_issues)
  5. one line per outlet where everything was normal

The facts come from the database only (``gather``). The AI provider
(``staff_ai``) writes the English; every line it writes is then checked
against those facts — an outlet, item, number or supplier that is not in
them drops THAT line, not the digest — and it is capped at ``MAX_LINES``.
If the provider fails or every line is dropped, the plain digest that the
code writes (``plain``) goes instead. Logged to ``staff_chat_log`` with
``kind = 'digest'``.

Pure: no Telegram, no database. bot.py gathers the rows and sends.
"""
from __future__ import annotations

import json
import logging

import staff_ai
import staff_chat
import staff_live

logger = logging.getLogger(__name__)

KIND = "digest"
MAX_LINES = 12
SENT = (staff_live.ANSWERED, staff_live.NO_REPLY, staff_live.OPEN, staff_live.REMINDED)
BILL_RESOLVED = ("ok", staff_live.HANDED_IN)


def gather(threads: list[dict], outlets: dict, issues: list[dict] | None = None,
           day=None) -> dict:
    """The digest's facts from today's ``staff_chat_thread`` rows.
    ``outlets``: ``{code: label}`` of the live outlets. ``issues``: open
    ``staff_issues`` rows (dicts with outlet_code, type, summary_en,
    urgent). Pure."""
    label = lambda code: outlets.get(code) or str(code)   # noqa: E731
    per: dict = {code: {"asked": 0, "answered": 0, "unanswered": 0, "waiting": 0}
                 for code in outlets}
    bills_open, orders_changed, other_answers = [], [], []
    for t in threads or []:
        code = t.get("outlet_code")
        if t.get("status") not in SENT or not t.get("asked_at"):
            continue
        s = per.setdefault(code, {"asked": 0, "answered": 0, "unanswered": 0, "waiting": 0})
        s["asked"] += 1
        status = t.get("status")
        if status == staff_live.ANSWERED:
            s["answered"] += 1
        elif status == staff_live.NO_REPLY:
            s["unanswered"] += 1
        else:
            s["waiting"] += 1
        f = t.get("facts") or {}
        if t.get("slot") == "bills" and not (
                status == staff_live.ANSWERED and t.get("reply_status") in BILL_RESOLVED):
            bills_open.append({"outlet": label(code), "supplier": f.get("supplier") or "supplier",
                               "days": str(f.get("days") or ""),
                               "answer": (t.get("reply_en") or "no reply") if status == staff_live.ANSWERED
                               else "no reply"})
        if (t.get("slot") == "order" and status == staff_live.ANSWERED
                and t.get("reply_status") == "order"):
            orders_changed.append({"outlet": label(code),
                                   "change": t.get("reply_en") or t.get("reply_text") or "changed"})
        elif (status == staff_live.ANSWERED and t.get("reply_status") in ("short", "finished", "problem")
              and t.get("slot") != "bills"):
            other_answers.append({"outlet": label(code), "checkin": t.get("slot"),
                                  "said": t.get("reply_en") or t.get("reply_text") or ""})
    silent, normal = [], []
    flagged = ({b["outlet"] for b in bills_open} | {o["outlet"] for o in orders_changed}
               | {i.get("outlet") for i in (issues or [])} | {a["outlet"] for a in other_answers})
    for code in sorted(per, key=lambda c: (-per[c]["unanswered"], label(c))):
        s = per[code]
        if not s["asked"]:
            continue
        if s["unanswered"] or (s["waiting"] and not s["answered"]):
            silent.append({"outlet": label(code), "asked": str(s["asked"]),
                           "unanswered": str(s["unanswered"] + s["waiting"]),
                           "never_replied": s["answered"] == 0})
        elif label(code) not in flagged and not s["waiting"]:
            normal.append({"outlet": label(code), "answered": str(s["answered"])})
    return {
        "day": day.isoformat() if day else None,
        "silent": silent,
        "bills_open": bills_open,
        "orders_changed": orders_changed,
        "issues": [{"outlet": i.get("outlet"), "type": i.get("type") or "other",
                    "summary": i.get("summary_en") or "", "urgent": bool(i.get("urgent"))}
                   for i in (issues or [])],
        "other_answers": other_answers,
        "normal": normal,
        "outlets_asked": str(sum(1 for s in per.values() if s["asked"])),
    }


def plain(facts: dict) -> str:
    """The digest the code writes itself — the fallback and the reference."""
    lines: list[str] = []
    for s in facts.get("silent") or []:
        if s.get("never_replied"):
            lines.append(f"❌ {s['outlet']}: no reply to any of {s['asked']} check-ins")
        else:
            lines.append(f"⚠️ {s['outlet']}: {s['unanswered']} of {s['asked']} check-ins unanswered")
    for b in facts.get("bills_open") or []:
        days = f" ({b['days']} days)" if b.get("days") else ""
        lines.append(f"🧾 {b['outlet']}: {b['supplier']} bill still not uploaded{days} — {b['answer']}")
    for o in facts.get("orders_changed") or []:
        lines.append(f"✏️ {o['outlet']}: order changed — {o['change']}")
    for i in facts.get("issues") or []:
        mark = "🚨" if i.get("urgent") else "🔧"
        lines.append(f"{mark} {i['outlet']}: {i['type']} — {i['summary']}")
    for a in facts.get("other_answers") or []:
        lines.append(f"💬 {a['outlet']} ({a['checkin']}): {a['said']}")
    for n in facts.get("normal") or []:
        lines.append(f"✅ {n['outlet']}: all {n['answered']} replies in, no changes")
    if not lines:
        return ""
    return "\n".join(["🌙 Staff digest — today"] + lines[:MAX_LINES])


SYSTEM_PROMPT = (
    "You write the nightly staff digest for the director of Khulafa, a "
    "Malaysian restaurant group: what the outlet cashiers replied to the "
    "office's check-ins today, and what they did not. Plain English, short, "
    "for a phone screen.\n"
    "Rules:\n"
    "- At most 12 lines, one point per line, no headers, no bullets other "
    "than a leading emoji, no bold.\n"
    "- Order by concern: first outlets that never replied, then supplier "
    "bills still not uploaded, then orders changed from the draft, then "
    "issues flagged, then one line per outlet where everything was normal.\n"
    "- Use ONLY the facts given. Every outlet name, number, item and supplier "
    "must appear in the facts exactly as given; never add totals, money, "
    "percentages or guesses.\n"
    "- Say the same as the reference digest, in your own words.\n"
    'Reply with JSON only: {"text": "<the digest, lines separated by \\\\n>"}'
)


def check_lines(text: str, facts: dict, *, all_labels=(), vocabulary=None) -> tuple[list[str], list[str]]:
    """``(kept, problems)``: each line of ``text`` against the facts. An
    outlet not in the facts, a number, item or supplier not in them, or a
    money figure drops that line; ``problems`` says why per dropped line."""
    fact_text = " ".join(staff_chat._fact_strings(facts))
    other = [lab for lab in all_labels if lab and lab not in fact_text]
    kept, problems = [], []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        found = staff_chat.fact_check(line, facts, vocabulary=vocabulary, other_names=other,
                                      language="english", slot=None)
        found = [p for p in found if p != "too long"] if len(line) <= 400 else found
        if found:
            problems.append(f"{line[:60]}: {'; '.join(found[:2])}")
        else:
            kept.append(line)
    return kept[:MAX_LINES], problems


def build(facts: dict, *, complete=None, all_labels=(), vocabulary=None) -> dict:
    """Word the digest. ``{text, source, problems, template, ai_text,
    provider, model, tokens_in, tokens_out}``; source is 'ai' when at least
    one AI line survived the fact check, else 'template'. Never raises."""
    template = plain(facts)
    out = {"text": template, "source": "template", "problems": [], "template": template,
           "ai_text": None, "provider": staff_ai.provider(), "model": staff_ai.model(),
           "tokens_in": None, "tokens_out": None, "english": ""}
    if not template:
        out["problems"] = ["nothing to report"]
        return out
    complete = complete or staff_ai.complete_json
    user = json.dumps({"facts": facts, "reference_digest": template}, ensure_ascii=False)
    try:
        result = complete(SYSTEM_PROMPT, user)
    except Exception:
        logger.exception("staff digest: provider call failed")
        result = None
    if not result:
        out["problems"] = ["ai unavailable"]
        return out
    data = result.get("data") or {}
    ai_text = str(data.get("text") or "").strip()
    out.update(ai_text=ai_text, provider=result.get("provider") or out["provider"],
               model=result.get("model") or out["model"],
               tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"))
    kept, problems = check_lines(ai_text, facts, all_labels=all_labels, vocabulary=vocabulary)
    out["problems"] = problems
    if not kept:
        out["problems"] = problems or ["empty"]
        return out
    out.update(text="\n".join(["🌙 Staff digest — today"] + kept), source="ai")
    return out


def log_row(facts: dict, result: dict) -> dict:
    row = staff_chat.log_row(KIND, "ALL", None, None, "english", facts, result, "natural")
    row["kind"] = KIND
    return row
