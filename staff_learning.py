"""Learning loop: the wordings that got the fastest replies become examples.

Every check-in already records when it was asked and when it was answered
(``staff_chat_thread.asked_at / answered_at``) and, since migrations/0054,
whether the reply reader understood the answer first time
(``reply_clear``). Once a week (Monday 08:00) ``pick`` looks at the last
7 days per language and check-in: for each distinct wording it takes the
median minutes-to-reply and the share of clear replies, and the three
fastest wordings (at least ``MIN_SAMPLES`` replies each) are stored in
``phrasing_examples`` (migrations/0056).

When the AI is asked to word a check-in, those three go in the prompt as
few-shot examples of "what got answered quickly" — but only as many as
fit a token budget: 30% of the current average prompt size
(``budget``), so the prompt never grows past the average plus 30%.

Pure: no database. bot.py reads the threads and writes the table.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

import staff_live

TABLE = "phrasing_examples"
TOP = 3
MIN_SAMPLES = 2
BUDGET_PCT = 0.30
DEFAULT_AVG_TOKENS = 900
MAX_EXAMPLE_CHARS = 300


def _ts(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def reply_minutes(thread: dict) -> float | None:
    asked, answered = _ts(thread.get("asked_at")), _ts(thread.get("answered_at"))
    if asked is None or answered is None:
        return None
    return max(0.0, (answered - asked).total_seconds() / 60)


def week_start(day: date) -> date:
    """The Monday of the week ``day`` is in."""
    return day - timedelta(days=day.weekday())


def pick(threads: list[dict], *, week: date, top: int = TOP,
         min_samples: int = MIN_SAMPLES) -> list[dict]:
    """``phrasing_examples`` rows from a week of answered threads: per
    language and check-in, the ``top`` wordings with the fastest median
    reply (ties: the clearer one), each backed by ``min_samples`` replies."""
    groups: dict[tuple, dict] = {}
    for t in threads or []:
        if t.get("status") != staff_live.ANSWERED:
            continue
        text = str(t.get("question_text") or "").strip()
        language, slot = t.get("language"), t.get("slot")
        minutes = reply_minutes(t)
        if not text or not language or not slot or minutes is None:
            continue
        g = groups.setdefault((language, slot, text), {"minutes": [], "clear": []})
        g["minutes"].append(minutes)
        clear = t.get("reply_clear")
        g["clear"].append(1.0 if clear else 0.0 if clear is not None else 1.0)
    per_pair: dict[tuple, list[dict]] = {}
    for (language, slot, text), g in groups.items():
        if len(g["minutes"]) < min_samples:
            continue
        per_pair.setdefault((language, slot), []).append({
            "week_start": week.isoformat(), "language": language, "slot": slot,
            "text": text[:MAX_EXAMPLE_CHARS],
            "median_minutes": round(statistics.median(g["minutes"]), 1),
            "samples": len(g["minutes"]),
            "clear_rate": round(sum(g["clear"]) / len(g["clear"]), 2),
        })
    rows: list[dict] = []
    for key in sorted(per_pair):
        best = sorted(per_pair[key], key=lambda r: (r["median_minutes"], -r["clear_rate"],
                                                    -r["samples"], r["text"]))[:top]
        for rank, r in enumerate(best, 1):
            rows.append({**r, "rank": rank})
    return rows


def est_tokens(text: str) -> int:
    """Rough token count: ~4 Latin characters per token, and a token per
    character for Tamil or other non-Latin script (tokenised heavily)."""
    latin = sum(1 for c in text if ord(c) < 128)
    other = len(text) - latin
    return max(1, latin // 4 + other)


def budget(avg_tokens_in: float | None, pct: float = BUDGET_PCT) -> int:
    """Tokens the examples may add: ``pct`` of the current average prompt."""
    avg = float(avg_tokens_in) if avg_tokens_in else DEFAULT_AVG_TOKENS
    return max(0, int(avg * pct))


def select(rows: list[dict], language: str, slot: str, token_budget: int) -> list[str]:
    """The example texts for one language and check-in, in rank order, as
    many as fit the budget."""
    out, used = [], 0
    for r in sorted((r for r in rows or [] if r.get("language") == language
                     and r.get("slot") == slot), key=lambda r: int(r.get("rank") or 99)):
        text = str(r.get("text") or "")
        cost = est_tokens(text)
        if not text or used + cost > token_budget:
            continue
        out.append(text)
        used += cost
        if len(out) >= TOP:
            break
    return out


def by_pair(rows: list[dict]) -> dict:
    """``{(language, slot): [rows]}`` for quick lookup."""
    out: dict = {}
    for r in rows or []:
        out.setdefault((r.get("language"), r.get("slot")), []).append(r)
    return out


def format_report(rows: list[dict], label=str) -> str:
    """The director's Monday note: what got answered fastest."""
    if not rows:
        return "📚 Phrasing examples: no wording had enough replies last week."
    lines = [f"📚 Phrasing examples updated — {len(rows)} wording(s) kept:"]
    for r in rows:
        if r.get("rank") == 1:
            lines.append(f"• {r['language']} {r['slot']}: {r['median_minutes']} min "
                         f"({r['samples']} replies, {int(r['clear_rate'] * 100)}% clear) — "
                         f"{r['text'][:80]}")
    return "\n".join(lines)
