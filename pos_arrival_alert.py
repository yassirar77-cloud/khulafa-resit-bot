"""Owner-only "POS belum terima" alert (09:00 MYT, to ALERT_CHAT_ID).

Daily: for every ACTIVE outlet (outlet_canonical.active), yesterday's business
day must have its usual D-files (sales_daily_summary) and S-files (sales_daily).
"Usual" is learned from the last LOOKBACK_DAYS: a file type the outlet sent on
fewer than MIN_DAYS_SEEN of them is not expected (some POS never send D-files),
and the expected count per day is the most common one (a 24h outlet closes
twice). Fewer than expected -> one line "POS belum terima: SEK14 2026-10-11
(D-file 1/2)".

Monthly: on the MONTHLY_DUE_DAY (3rd), every active outlet must have emailed
last month's "MONTHLY REPORT-<OUTLET> ON <Mon> -<YYYY>". The report is not
ingested, but each one is logged in sales_ingest_log (skipped_report), which
is what this checks.

Pure helpers take plain rows so they are testable without a database.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 14
MIN_DAYS_SEEN = 3
MONTHLY_DUE_DAY = 3

_PREFIX_RE = re.compile(r"^[sd]-", re.IGNORECASE)
_MONTHLY_RE = re.compile(
    r"^\s*MONTHLY\s+REPORT\s*-\s*(.+?)\s+ON\s+([A-Za-z]{3})\s*-\s*(\d{4})", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def bare_code(code) -> str:
    """"S-SEK14" / "D-SEK14" / "sek14" -> "SEK14"."""
    return _PREFIX_RE.sub("", str(code or "").strip()).strip().upper()


def active_outlet_codes(canonical_rows) -> list[str]:
    """Bare codes of the active S-coded outlets in outlet_canonical."""
    out = []
    for r in canonical_rows or []:
        code = str(r.get("code") or "")
        if r.get("active") and code.upper().startswith("S-"):
            out.append(bare_code(code))
    return sorted(set(out))


def _daily_counts(rows, date_key) -> dict:
    """{(outlet, iso_date): n_files}."""
    counts: Counter = Counter()
    for r in rows or []:
        d = r.get(date_key)
        if d:
            counts[(bare_code(r.get("outlet_code")), str(d)[:10])] += 1
    return counts


def expected_per_day(counts, outlet, target: date) -> int:
    """How many files of one type ``outlet`` normally sends per business day,
    learned from the LOOKBACK_DAYS before ``target``. 0 = not expected."""
    seen = []
    for i in range(1, LOOKBACK_DAYS + 1):
        n = counts.get((outlet, (target - timedelta(days=i)).isoformat()), 0)
        if n:
            seen.append(n)
    if len(seen) < MIN_DAYS_SEEN:
        return 0
    freq = Counter(seen)
    best = max(freq.values())
    return max(n for n, c in freq.items() if c == best)


def missing_daily(outlets, d_rows, s_rows, target: date) -> list[str]:
    """Alert lines for ``target`` (a business date). d_rows: sales_daily_summary
    {outlet_code, business_date}; s_rows: sales_daily {outlet_code,
    shift_business_date}; both covering target-LOOKBACK_DAYS .. target."""
    kinds = (("D-file", _daily_counts(d_rows, "business_date")),
             ("S-file", _daily_counts(s_rows, "shift_business_date")))
    day = target.isoformat()
    lines = []
    for outlet in outlets:
        short = []
        for label, counts in kinds:
            want = expected_per_day(counts, outlet, target)
            got = counts.get((outlet, day), 0)
            if want and got < want:
                short.append(f"{label} {got}/{want}")
        if short:
            lines.append(f"POS belum terima: {outlet} {day} ({', '.join(short)})")
    return lines


def previous_month(today: date) -> tuple[int, int]:
    first = today.replace(day=1)
    last = first - timedelta(days=1)
    return last.year, last.month


def monthly_reports_seen(subjects, year: int, month: int) -> set[str]:
    """Bare outlet codes with a MONTHLY REPORT subject for year/month."""
    want = _MONTHS[month - 1]
    seen = set()
    for s in subjects or []:
        m = _MONTHLY_RE.match(str(s or ""))
        if m and m.group(2).lower() == want and int(m.group(3)) == year:
            seen.add(bare_code(m.group(1)))
    return seen


def missing_monthly(outlets, subjects, today: date) -> list[str]:
    """Alert lines for last month's monthly reports — only on MONTHLY_DUE_DAY."""
    if today.day != MONTHLY_DUE_DAY:
        return []
    year, month = previous_month(today)
    seen = monthly_reports_seen(subjects, year, month)
    label = f"{_MONTHS[month - 1].capitalize()} {year}"
    return [f"POS belum terima: {o} laporan bulanan {label}"
            for o in outlets if o not in seen]


# --- I/O ------------------------------------------------------------------------

def _fetch(client, table, columns, date_col, since, until):
    from db_pagination import fetch_all_pages
    return fetch_all_pages(
        lambda: client.table(table).select(columns)
        .gte(date_col, since).lte(date_col, until).order("id", desc=False))


def build_alert(client, now_my: datetime) -> list[str]:
    """All alert lines due at ``now_my`` (09:00 MYT): yesterday's business day,
    plus last month's monthly reports on the 3rd."""
    today = now_my.date()
    target = today - timedelta(days=1)
    since = (target - timedelta(days=LOOKBACK_DAYS)).isoformat()
    canon = client.table("outlet_canonical").select("code, active").execute().data or []
    outlets = active_outlet_codes(canon)
    d_rows = _fetch(client, "sales_daily_summary", "id, outlet_code, business_date",
                    "business_date", since, target.isoformat())
    s_rows = _fetch(client, "sales_daily", "id, outlet_code, shift_business_date",
                    "shift_business_date", since, target.isoformat())
    lines = missing_daily(outlets, d_rows, s_rows, target)
    if today.day == MONTHLY_DUE_DAY:
        # Monthly reports arrive on the 1st; look back to the 25th to be safe.
        log_since = (today.replace(day=1) - timedelta(days=6)).isoformat()
        log_rows = client.table("sales_ingest_log").select("source_subject") \
            .ilike("source_subject", "monthly%").gte("ran_at", log_since) \
            .execute().data or []
        lines += missing_monthly(outlets, [r.get("source_subject") for r in log_rows], today)
    return lines
