"""Known merchants per outlet — only a NEW shop name is a pin target.

Months of bills show that every outlet buys from a steady set of shops, many
of them not on the ``approved_suppliers`` list (the ice man, the santan
supplier, the caterer). Flagging all of those as "outside purchases" would
bury the real signal, so a merchant is only pinned when it is neither an
approved supplier nor known for THAT outlet.

``outlet_known_merchants`` (migrations/0059) holds, per outlet, the shops
that appeared in >= ``MIN_BILLS`` purchase bills over the last
``LOOKBACK_DAYS`` days when the baseline was built, plus every approved
supplier (source ``approved``) and anything the director adds by hand
(``manual``). Matching reuses ``outside_purchase.match_supplier`` — exact,
alias, word-bounded phrase, clear OCR drift; a grey-zone score is still a
review, never a silent match — so "BESTARI FARN" is BESTARI FARM and
BESTARI MINIMART is nobody.

The nightly refresh streams receipts in pages (never the whole table in
memory on a 512 MB box), updates bill counts and last-seen dates of the
merchants already known, and adds rows for approved suppliers. It NEVER
promotes a new shop just because cashiers kept using it: that takes
``/tambah_supplier`` (approved everywhere) or a manual row. ``/buang_merchant``
deactivates a row, and a deactivated row stays out.

Pure functions take plain rows; the ``db`` functions take the Supabase
client first, like the rest of the repo.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from merchant_resolver import normalise_text
from outlet_resolver import canonical_outlet

logger = logging.getLogger(__name__)

MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

TABLE = "outlet_known_merchants"
RECEIPTS_TABLE = "receipts"
ROSTER_TABLE = "cashier_roster"

MIN_BILLS = 3
LOOKBACK_DAYS = 90
PAGE_SIZE = 1000

SOURCE_HISTORY, SOURCE_APPROVED, SOURCE_MANUAL = "history", "approved", "manual"

_NON_PURCHASE_TYPES = frozenset({
    "STAFF_ADVANCE", "UTILITY", "RENT_LICENSE", "PETTY_CASH", "INTERNAL_TRANSFER",
})
# Khulafa's own outlets as OCR reads them (stock transfers are not shops).
_OWN_OUTLET_RE = re.compile(r"(khula|khalifa|kulapa|kehulafia|sharfud+in)", re.IGNORECASE)


def _display_outlet(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return canonical_outlet(str(value)) or str(value).strip()


def active_rows(rows, outlet: Any = None) -> list[dict]:
    """Active known-merchant rows, optionally for one outlet."""
    target = _display_outlet(outlet) if outlet else None
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("active") is False or not r.get("canonical_merchant"):
            continue
        if target is not None and _display_outlet(r.get("outlet")) != target:
            continue
        out.append(r)
    return out


def as_suppliers(rows, outlet: Any) -> list[dict]:
    """Known rows for ``outlet`` in the shape ``match_supplier`` reads
    (``canonical_name`` / ``aliases`` / ``active``)."""
    return [{"canonical_name": r["canonical_merchant"], "aliases": list(r.get("aliases") or []),
             "active": True, "known_id": r.get("id"), "source": r.get("source")}
            for r in active_rows(rows, outlet)]


def is_own_outlet(merchant: Any) -> bool:
    return bool(_OWN_OUTLET_RE.search(str(merchant or "")))


# --- baseline from receipts (pure) -----------------------------------------------------

def _to_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _upload_day(created_at) -> date | None:
    from date_utils import receipt_day

    return receipt_day(None, created_at)


def aggregate_receipts(rows, agg: dict | None = None, *, group_codes: dict | None = None,
                       since: date | None = None, until: date | None = None) -> dict:
    """Fold a page of receipt rows into ``{(outlet, MERCHANT): {bills, first_seen,
    last_seen}}``. Only purchase bills with a readable merchant count; the
    outlet comes from the registered group (``group_codes``: chat_id -> code)
    first, then ``receipts.outlet``. Call once per page with the same ``agg``."""
    agg = agg if agg is not None else {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("receipt_type") or "UNKNOWN").upper() in _NON_PURCHASE_TYPES:
            continue
        merchant = " ".join(str(r.get("merchant") or "").split()).upper()
        if not merchant or merchant == "UNKNOWN" or is_own_outlet(merchant):
            continue
        from outside_purchase import is_staff_payment

        if is_staff_payment(merchant):
            continue
        # Undated bills count on their upload day.
        d = _to_date(r.get("receipt_date")) or _upload_day(r.get("created_at"))
        if d is None or (since and d < since) or (until and d > until):
            continue
        code = None
        if group_codes:
            try:
                code = group_codes.get(int(r.get("chat_id")))
            except (TypeError, ValueError):
                code = None
        outlet = _display_outlet(code) or _display_outlet(r.get("outlet"))
        if not outlet:
            continue
        bucket = agg.setdefault((outlet, merchant), {"bills": 0, "first_seen": d, "last_seen": d})
        bucket["bills"] += 1
        bucket["first_seen"] = min(bucket["first_seen"], d)
        bucket["last_seen"] = max(bucket["last_seen"], d)
    return agg


def build_baseline(agg: dict, suppliers, min_bills: int = MIN_BILLS) -> list[dict]:
    """Known-merchant rows from an aggregate: per outlet, OCR variants of the
    same shop are folded together (most frequent spelling is the canonical,
    the rest aliases), approved suppliers are left out (they are known
    everywhere already), and only shops with >= ``min_bills`` bills survive."""
    from outside_purchase import APPROVED, match_supplier

    per_outlet: dict[str, list[tuple[str, dict]]] = {}
    for (outlet, merchant), stats in agg.items():
        per_outlet.setdefault(outlet, []).append((merchant, stats))
    out: list[dict] = []
    for outlet, entries in sorted(per_outlet.items()):
        entries.sort(key=lambda e: (-e[1]["bills"], e[0]))
        clusters: list[dict] = []
        for merchant, stats in entries:
            if match_supplier(merchant, suppliers)["decision"] == APPROVED:
                continue
            hit = None
            for c in clusters:
                if match_supplier(merchant, [c])["decision"] == APPROVED:
                    hit = c
                    break
            if hit is None:
                clusters.append({"canonical_name": merchant, "aliases": [], "active": True,
                                 "bills": stats["bills"], "first_seen": stats["first_seen"],
                                 "last_seen": stats["last_seen"]})
                continue
            if merchant not in hit["aliases"]:
                hit["aliases"].append(merchant)
            hit["bills"] += stats["bills"]
            hit["first_seen"] = min(hit["first_seen"], stats["first_seen"])
            hit["last_seen"] = max(hit["last_seen"], stats["last_seen"])
        for c in clusters:
            if c["bills"] >= min_bills:
                out.append({"outlet": outlet, "canonical_merchant": c["canonical_name"],
                            "aliases": c["aliases"], "bill_count": c["bills"],
                            "first_seen": c["first_seen"].isoformat(),
                            "last_seen": c["last_seen"].isoformat(),
                            "source": SOURCE_HISTORY, "active": True})
    return out


def recount(known_rows, agg: dict) -> list[dict]:
    """For each active known row, the fresh ``{id, bill_count, first_seen,
    last_seen}`` from the aggregate (variants matched fuzzily). Only rows whose
    numbers changed are returned. Never adds a merchant."""
    from outside_purchase import APPROVED, match_supplier

    by_outlet: dict[str, list[tuple[str, dict]]] = {}
    for (outlet, merchant), stats in agg.items():
        by_outlet.setdefault(outlet, []).append((merchant, stats))
    updates = []
    for row in active_rows(known_rows):
        outlet = _display_outlet(row.get("outlet"))
        probe = [{"canonical_name": row["canonical_merchant"], "aliases": row.get("aliases") or [],
                  "active": True}]
        bills, first, last = 0, None, None
        for merchant, stats in by_outlet.get(outlet, []):
            if match_supplier(merchant, probe)["decision"] != APPROVED:
                continue
            bills += stats["bills"]
            first = stats["first_seen"] if first is None else min(first, stats["first_seen"])
            last = stats["last_seen"] if last is None else max(last, stats["last_seen"])
        fields = {"bill_count": bills,
                  "first_seen": first.isoformat() if first else row.get("first_seen"),
                  "last_seen": last.isoformat() if last else row.get("last_seen")}
        if any(str(row.get(k)) != str(v) for k, v in fields.items()):
            updates.append({"id": row.get("id"), **fields})
    return updates


# --- database -----------------------------------------------------------------------------

def load(db, outlet: Any = None) -> list[dict]:
    try:
        q = db.table(TABLE).select("*")
        if outlet:
            q = q.eq("outlet", _display_outlet(outlet))
        return q.order("id", desc=False).limit(5000).execute().data or []
    except Exception:
        logger.exception("known merchants: load failed")
        return []


def stream_receipt_aggregates(db, since: date, until: date, group_codes: dict | None,
                              page_size: int = PAGE_SIZE) -> dict:
    """Aggregate the receipts in ``[since, until]`` one page at a time — the
    aggregate holds one entry per (outlet, merchant), never the receipts."""
    from date_utils import upload_window

    agg: dict = {}
    start = 0
    while True:
        page = (db.table(RECEIPTS_TABLE)
                .select("id, outlet, merchant, chat_id, receipt_date, receipt_type")
                .gte("receipt_date", since.isoformat()).lte("receipt_date", until.isoformat())
                .order("id", desc=False).range(start, start + page_size - 1)
                .execute().data or [])
        aggregate_receipts(page, agg, group_codes=group_codes, since=since, until=until)
        if len(page) < page_size:
            break
        start += page_size
    # Undated bills, by upload day, so a supplier billed without dates still
    # becomes known (and is not flagged as an outside purchase).
    try:
        gte, lte = upload_window(since, until)
        start = 0
        while True:
            page = (db.table(RECEIPTS_TABLE)
                    .select("id, outlet, merchant, chat_id, receipt_date, receipt_type, created_at")
                    .is_("receipt_date", "null")
                    .gte("created_at", gte).lte("created_at", lte)
                    .order("id", desc=False).range(start, start + page_size - 1)
                    .execute().data or [])
            aggregate_receipts(page, agg, group_codes=group_codes, since=since, until=until)
            if len(page) < page_size:
                break
            start += page_size
    except Exception:
        logger.warning("known merchants: undated-receipt fallback read failed", exc_info=True)
    return agg


def _roster_outlets(db) -> list[str]:
    try:
        rows = db.table(ROSTER_TABLE).select("outlet").execute().data or []
    except Exception:
        return []
    return sorted({_display_outlet(r.get("outlet")) for r in rows if r.get("outlet")})


def _invalidate() -> None:
    try:
        import outside_purchase
        outside_purchase.invalidate_config_cache()
    except Exception:  # pragma: no cover - defensive
        pass


def refresh(db, suppliers, *, group_codes: dict | None = None, today: date | None = None,
            seed_if_empty: bool = True) -> dict:
    """Nightly: recount the known merchants from the last ``LOOKBACK_DAYS``
    days, add approved suppliers everywhere, seed the baseline the very first
    time (empty table). Returns a summary dict. Never raises."""
    _invalidate()
    today = today or datetime.now(MY_TZ).date()
    summary = {"seeded": 0, "updated": 0, "approved_added": 0, "pairs": 0, "error": None}
    try:
        known = load(db)
        agg = stream_receipt_aggregates(db, today - timedelta(days=LOOKBACK_DAYS), today, group_codes)
        summary["pairs"] = len(agg)
        history_rows = [r for r in known if r.get("source") == SOURCE_HISTORY]
        if not history_rows and seed_if_empty:
            rows = build_baseline(agg, suppliers)
            if rows:
                db.table(TABLE).insert(rows).execute()
            summary["seeded"] = len(rows)
            known = load(db)
        else:
            for upd in recount(known, agg):
                fields = {k: v for k, v in upd.items() if k != "id"}
                fields["updated_at"] = datetime.now(MY_TZ).isoformat()
                db.table(TABLE).update(fields).eq("id", upd["id"]).execute()
                summary["updated"] += 1
        outlets = sorted({_display_outlet(r.get("outlet")) for r in known if r.get("outlet")}
                         | set(_roster_outlets(db)))
        have = {(_display_outlet(r.get("outlet")), normalise_text(r.get("canonical_merchant") or ""))
                for r in known}
        additions = []
        for outlet in outlets:
            for s in suppliers or []:
                if not isinstance(s, dict) or s.get("active") is False or not s.get("canonical_name"):
                    continue
                if (outlet, normalise_text(s["canonical_name"])) in have:
                    continue
                additions.append({"outlet": outlet, "canonical_merchant": s["canonical_name"],
                                  "aliases": list(s.get("aliases") or []), "source": SOURCE_APPROVED,
                                  "active": True, "bill_count": 0})
        if additions:
            db.table(TABLE).insert(additions).execute()
        summary["approved_added"] = len(additions)
    except Exception as exc:
        logger.exception("known merchants: refresh failed")
        summary["error"] = str(exc)
    return summary


def remove(db, outlet: Any, name: str, removed_by=None) -> dict | None:
    """/buang_merchant: deactivate the known row that matches ``name`` at
    ``outlet`` (exact, alias or clear fuzzy match). Returns the row or None."""
    _invalidate()
    from outside_purchase import APPROVED, match_supplier

    rows = active_rows(load(db, outlet), outlet)
    target = None
    for r in rows:
        if normalise_text(r.get("canonical_merchant") or "") == normalise_text(name):
            target = r
            break
    if target is None:
        for r in rows:
            probe = [{"canonical_name": r["canonical_merchant"], "aliases": r.get("aliases") or [],
                      "active": True}]
            if match_supplier(name, probe)["decision"] == APPROVED:
                target = r
                break
    if target is None:
        return None
    fields = {"active": False, "removed_by": removed_by,
              "removed_at": datetime.now(MY_TZ).isoformat(),
              "updated_at": datetime.now(MY_TZ).isoformat()}
    try:
        db.table(TABLE).update(fields).eq("id", target["id"]).execute()
    except Exception:
        logger.exception("known merchants: remove failed")
        return None
    return {**target, **fields}


# --- text -------------------------------------------------------------------------------------

def _is_minimarket(name: Any) -> bool:
    try:
        from staff_ops import is_minimarket

        return is_minimarket(name)
    except Exception:
        return False


def format_known(rows, outlet: Any) -> str:
    """/merchant_known <outlet>."""
    label = _display_outlet(outlet) or str(outlet)
    mine = sorted(active_rows(rows, outlet),
                  key=lambda r: (r.get("source") != SOURCE_HISTORY, -(r.get("bill_count") or 0),
                                 str(r.get("canonical_merchant"))))
    if not mine:
        return f"Tiada merchant dikenali untuk {label}. Jalankan refresh malam atau tambah dengan /tambah_supplier."
    lines = [f"🏪 Merchant dikenali — {label} ({len(mine)})"]
    for r in mine:
        src = {SOURCE_HISTORY: "sejarah", SOURCE_APPROVED: "supplier rasmi", SOURCE_MANUAL: "manual"}.get(
            r.get("source"), str(r.get("source")))
        flag = " ⚠️ mini market" if _is_minimarket(r.get("canonical_merchant")) else ""
        last = f" · terakhir {str(r.get('last_seen'))[:10]}" if r.get("last_seen") else ""
        lines.append(f"• {r.get('canonical_merchant')} — {r.get('bill_count') or 0} bil ({src}){last}{flag}")
    lines.append(f"\nBuang: /buang_merchant {label} <nama>")
    return "\n".join(lines)


def format_report(rows) -> str:
    """Every outlet's known merchants with bill counts (history + manual rows;
    approved suppliers are summarised in one line). Mini markets flagged."""
    outlets: dict[str, list[dict]] = {}
    approved = 0
    for r in active_rows(rows):
        if r.get("source") == SOURCE_APPROVED:
            approved += 1
            continue
        outlets.setdefault(_display_outlet(r.get("outlet")) or "?", []).append(r)
    lines = ["🏪 Known merchants per outlet (history baseline) — ⚠️ = looks like a mini market"]
    for outlet in sorted(outlets):
        group = sorted(outlets[outlet], key=lambda r: (-(r.get("bill_count") or 0), str(r.get("canonical_merchant"))))
        lines.append(f"\n{outlet} ({len(group)}):")
        for r in group:
            flag = " ⚠️" if _is_minimarket(r.get("canonical_merchant")) else ""
            lines.append(f"  {r.get('bill_count') or 0:>3}  {r.get('canonical_merchant')}{flag}")
    lines.append(f"\n+ {approved} approved-supplier rows (known at every outlet).")
    lines.append("Remove one: /buang_merchant <outlet> <name>")
    return "\n".join(lines)
