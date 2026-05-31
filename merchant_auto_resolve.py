"""Risk-weighted auto-resolution of merchant canonicalisation (PR #68).

Builds ON TOP of the PR #30 merchant_canonical / merchant_alias tables, the
PR #31 receipt_classifier, and the PR #37 reconciliation_service — it does not
replace any of them. The job: clear the long tail of receipts whose ``merchant``
text never resolved to a canonical, WITHOUT silently mis-tagging a high-value
receipt.

The pipeline is four pure stages plus a thin DB layer:

  1. ``normalize_merchant_name``  — uppercase, strip SDN BHD variants (incl the
     truncated "SDN. BH"), punctuation, whitespace. Pure, idempotent.
  2. ``match_confidence``         — token-based similarity + a substring anchor,
     returns a confidence in [0, 1]. Stdlib only (``difflib``); see NOTE below.
  3. ``decide``                   — risk = (1 - confidence) x RM_at_stake, then
        conf >= AUTO_RESOLVE_CONF_CUTOFF        -> auto-resolve (any RM)
        conf <  cutoff and risk <  threshold    -> defer (silent long tail)
        conf <  cutoff and risk >= threshold    -> escalate to owner
  4. DB layer (``resolve_all`` / ``apply_auto_resolution`` / ``undo_resolution``)
     writes a ``merchant_alias`` row + a ``merchant_resolution_log`` row, tags
     the affected receipts, and re-runs reconciliation for every business date a
     tagged receipt touches so food cost updates across history. Every write is
     REVERSIBLE (``undo_resolution`` / ``/merchant_undo``) and the pass is
     RE-RUNNABLE (only NULL-canonical receipts are candidates).

NOTE on the fuzzy backend: we deliberately use the stdlib ``difflib`` rather
than adding ``rapidfuzz``. The backlog is ~136 distinct merchant strings scored
against ~56 canonicals once per pass — well under any size where rapidfuzz's
speed matters — and avoiding a new C-extension dependency keeps the Render
deploy reproducible. If the candidate set grows by orders of magnitude, swap
``_pair_confidence``'s ``difflib.SequenceMatcher`` for ``rapidfuzz.fuzz`` behind
the same signature.
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone

from date_utils import clamp_business_date
from merchant_resolver import ALIAS_TABLE, CANONICAL_TABLE, load_snapshot

logger = logging.getLogger(__name__)

RECEIPTS_TABLE = "receipts"
RESOLUTION_LOG_TABLE = "merchant_resolution_log"

# === Part 3 constants (the CORE). Exposed by name so the policy is auditable
# and tunable from one place. =================================================

# At or above this confidence we auto-resolve regardless of RM at stake: a clean
# match is a clean match whether the receipt is RM5 or RM5,000.
AUTO_RESOLVE_CONF_CUTOFF = 0.90

# risk = (1 - confidence) * RM_at_stake. Below the cutoff, a sub-threshold risk
# is deferred (silent — the long tail isn't worth owner attention); at/above it
# the merchant is escalated to the owner queue.
#
# Tuned against production: the initial conservative RM50 escalated 107
# merchants — far past the "handful per week" the queue is designed for. The
# risk distribution showed the money concentrates at the top (24 merchants at
# risk >= 200 carry RM32,781 of the RM50k+ at stake), so RM200 keeps that
# high-value tail in the owner queue while letting the 83 lower-risk merchants
# auto-resolve or defer. Lower it again only if too much real spend slips
# through silently.
ESCALATION_RISK_THRESHOLD = 200.0  # Ringgit

# Below this confidence we don't attach a best-guess canonical at all (the
# overlap is incidental). The merchant is still logged and risk-weighted, just
# without a suggested canonical for the owner to anchor on.
MIN_CANDIDATE_CONFIDENCE = 0.35

DECISION_AUTO = "auto_resolved"
DECISION_ESCALATE = "escalated"
DECISION_DEFER = "deferred"

STATUS_ACTIVE = "active"
STATUS_UNDONE = "undone"

ALIAS_CREATED_VIA = "auto_resolved"

# Sanity bound mirroring reconciliation_service: a single receipt above this is
# almost always an OCR phantom, so it shouldn't dominate a merchant's RM at
# stake (and thus its escalation decision).
MAX_RECEIPT_AMOUNT = 5000.0


# === Part 1: normalisation (pure) ===========================================

# Match the legal-suffix tail in any of its OCR forms, including the truncated
# "SDN. BH" (the OCR clips the final D). Punctuation has already been spaced out
# by the time this runs, so we match on the de-punctuated tokens.
_SDN_BHD_RE = re.compile(
    r"\b(?:SDN\s*BHD|SDN\s*BH|SENDIRIAN\s+BERHAD|BERHAD|BHD)\b"
)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_merchant_name(value) -> str:
    """Canonical form of a raw merchant string: UPPERCASE, SDN BHD variants
    removed (including the truncated "SDN. BH"), punctuation dropped, whitespace
    collapsed. Pure and idempotent — ``f(f(x)) == f(x)`` and it never mutates
    its input."""
    if value is None:
        return ""
    text = str(value)
    if not text.strip():
        return ""
    text = text.upper()
    # Drop punctuation first so "SDN. BHD." collapses to the tokens "SDN BHD"
    # the suffix regex expects, then strip the suffix, then re-collapse.
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    text = _SDN_BHD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


# === Part 2: confidence scorer (token-based + substring anchor, pure) ========

def _tokens(norm: str) -> list[str]:
    return [t for t in norm.split() if t]


def _phrase_contained(haystack: str, needle: str) -> bool:
    """True if ``needle`` appears in ``haystack`` on whole-word boundaries.
    Both must already be normalised. "EVEREST" matches inside "EVEREST AISVARAM"
    but not inside "EVERESTX"."""
    if not needle:
        return False
    return re.search(r"(?<!\S)" + re.escape(needle) + r"(?!\S)", haystack) is not None


# Fuzzy (non-anchored) matches are capped strictly below the auto-resolve
# cutoff: ONLY an exact normalised match or a clean word-bounded containment is
# ever confident enough to auto-resolve. Everything else — typos, partial token
# overlap — tops out here and must clear the risk gate.
_FUZZY_CONFIDENCE_CEILING = 0.85


def _pair_confidence(m: str, c: str) -> float:
    """Confidence in [0, 1] that normalised merchant ``m`` is the same entity as
    normalised candidate ``c``.

    Policy, strongest first:
      * exact normalised equality                     -> 1.0
      * substring anchor: one phrase contained in the
        other on whole-word boundaries                -> 0.90..1.0 (high band)
      * fuzzy: token overlap / difflib ratio          -> capped at the ceiling

    Doubled-letter OCR typos ("MEWAHH GROUPP") score high on a raw difflib ratio
    but are NOT a word-bounded containment, so the ceiling keeps them out of the
    auto-resolve band and routes them through the risk model."""
    if not m or not c:
        return 0.0
    if m == c:
        return 1.0
    m_tok, c_tok = set(_tokens(m)), set(_tokens(c))
    if not m_tok or not c_tok:
        return 0.0
    inter = m_tok & c_tok
    jaccard = len(inter) / len(m_tok | c_tok)
    coverage = len(inter) / len(c_tok)  # share of the candidate that is present
    ratio = difflib.SequenceMatcher(None, m, c).ratio()
    if _phrase_contained(m, c) or _phrase_contained(c, m):
        # Clean containment: lift into the high band, scaled by how much of the
        # candidate the merchant actually covers.
        return min(1.0, 0.90 + 0.10 * coverage)
    return min(max(jaccard, coverage, ratio), _FUZZY_CONFIDENCE_CEILING)


def match_confidence(raw, canonical_name, aliases=()) -> float:
    """Best confidence in [0, 1] that ``raw`` refers to a canonical, considering
    its display name and any known alias texts."""
    m = normalize_merchant_name(raw)
    if not m:
        return 0.0
    best = 0.0
    for cand in (canonical_name, *aliases):
        c = normalize_merchant_name(cand)
        if not c:
            continue
        best = max(best, _pair_confidence(m, c))
        if best >= 1.0:
            break
    return best


def best_canonical(raw, aliases, canonicals):
    """Pick the most likely canonical for ``raw`` from in-memory snapshots.

    ``aliases``: rows with ``alias_text`` + ``canonical_id``.
    ``canonicals``: rows with ``id`` + ``display_name``.

    Returns ``(canonical_id | None, confidence)``. ``canonical_id`` is None when
    no canonical clears ``MIN_CANDIDATE_CONFIDENCE`` — we don't attach a
    best-guess canonical to incidental character overlap, so an escalation on a
    genuinely unknown vendor reads as "no match" rather than a misleading
    suggestion. ``confidence`` still reflects the strongest signal found so the
    risk model can act on it."""
    alias_by_canon: dict = {}
    for a in aliases:
        alias_by_canon.setdefault(a.get("canonical_id"), []).append(a.get("alias_text") or "")
    best_cid, best_conf = None, 0.0
    for c in canonicals:
        cid = c.get("id")
        conf = match_confidence(raw, c.get("display_name") or "", alias_by_canon.get(cid, ()))
        if conf > best_conf:
            best_cid, best_conf = cid, conf
    if best_conf < MIN_CANDIDATE_CONFIDENCE:
        return None, best_conf
    return best_cid, best_conf


# === Part 3: risk-weighted decision (pure) ===================================

def risk_score(confidence, rm_at_stake) -> float:
    """risk = (1 - confidence) x RM at stake. The expected Ringgit mis-tagged if
    this match is wrong."""
    return (1.0 - float(confidence)) * float(rm_at_stake)


def decide(confidence, rm_at_stake):
    """Return ``(decision, risk)`` for a candidate match.

      - confidence >= cutoff                 -> auto-resolve (any RM)
      - else risk >= escalation threshold    -> escalate to owner
      - else                                 -> defer (silent long tail)
    """
    risk = risk_score(confidence, rm_at_stake)
    if float(confidence) >= AUTO_RESOLVE_CONF_CUTOFF:
        return DECISION_AUTO, risk
    if risk >= ESCALATION_RISK_THRESHOLD:
        return DECISION_ESCALATE, risk
    return DECISION_DEFER, risk


def plan_resolution(raw, rm_at_stake, receipts, aliases, canonicals) -> dict:
    """Pure per-merchant plan. ``receipts`` is a list of ``{id, business_date}``
    dicts. Returns the row that will (after a DB write) become a
    merchant_resolution_log entry."""
    cid, conf = best_canonical(raw, aliases, canonicals)
    decision, risk = decide(conf, rm_at_stake)
    return {
        "raw_merchant": raw,
        "canonical_id": cid,
        "confidence": round(conf, 4),
        "rm_at_stake": round(float(rm_at_stake), 2),
        "risk": round(risk, 4),
        "decision": decision,
        "receipt_ids": [r["id"] for r in receipts],
        "affected_dates": sorted({r["business_date"] for r in receipts if r.get("business_date")}),
    }


# === Part 4/6: DB layer ======================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value):
    try:
        return 0.0 if value is None else float(value)
    except (TypeError, ValueError):
        return 0.0


_CANDIDATE_COLUMNS = "id, merchant, total, receipt_date, created_at"


def fetch_unresolved_merchants(client) -> dict:
    """Group every NULL-canonical receipt by its raw merchant text.

    Returns ``{merchant_text: {"rm": float, "receipts": [{id, business_date}]}}``.
    Receipts with no merchant text are unrecoverable and skipped. This is the
    idempotency gate — once a receipt is tagged it drops out of the backlog, so
    the pass is safely re-runnable."""
    rows = (
        client.table(RECEIPTS_TABLE)
        .select(_CANDIDATE_COLUMNS)
        .is_("merchant_canonical_id", "null")
        .execute()
        .data
        or []
    )
    backlog: dict = {}
    for r in rows:
        name = (r.get("merchant") or "").strip()
        if not name:
            continue
        amount = _to_float(r.get("total"))
        if amount > MAX_RECEIPT_AMOUNT:
            amount = 0.0  # OCR phantom — don't let it dominate the RM at stake
        eff, _clamped = clamp_business_date(r.get("receipt_date"), r.get("created_at"))
        info = backlog.setdefault(name, {"rm": 0.0, "receipts": []})
        info["rm"] += amount
        info["receipts"].append({
            "id": r.get("id"),
            "business_date": eff.isoformat() if eff else None,
        })
    return backlog


def _default_reconcile(client, dates):
    """Re-run reconciliation_service for each affected business date so food
    cost recomputes across history. Imported lazily so the pure stages have no
    Supabase/runtime dependency."""
    import reconciliation_service
    for d in dates:
        try:
            reconciliation_service.run_reconciliation(client, d)
        except Exception:
            logger.warning("merchant_auto_resolve: reconcile failed for %s", d, exc_info=True)


def _insert_alias(client, raw_merchant, canonical_id, confidence):
    """Insert (or re-find) the alias mapping the raw OCR string to the canonical.
    Returns the alias id, or None if it could not be written/found."""
    payload = {
        "alias_text": raw_merchant,
        "canonical_id": canonical_id,
        "match_confidence": int(round(float(confidence) * 100)),
        "created_via": ALIAS_CREATED_VIA,
    }
    try:
        rows = client.table(ALIAS_TABLE).insert(payload).execute().data or []
        if rows and rows[0].get("id") is not None:
            return rows[0]["id"]
    except Exception:
        # UNIQUE(alias_text) — already mapped on a prior run. Re-find it so the
        # log row still references the alias and undo can remove it.
        logger.info("merchant_auto_resolve: alias %r exists; re-finding", raw_merchant)
    existing = (
        client.table(ALIAS_TABLE).select("id").eq("alias_text", raw_merchant).limit(1).execute().data
        or []
    )
    return existing[0]["id"] if existing else None


def record_log(client, plan, status, *, alias_id=None, actor=None, now=None) -> dict:
    """Insert one merchant_resolution_log row and return it (with id)."""
    row = {
        "raw_merchant": plan["raw_merchant"],
        "canonical_id": plan["canonical_id"],
        "alias_id": alias_id,
        "confidence": plan["confidence"],
        "rm_at_stake": plan["rm_at_stake"],
        "risk": plan["risk"],
        "decision": plan["decision"],
        "status": status,
        "receipt_ids": plan["receipt_ids"],
        "affected_dates": plan["affected_dates"],
        "created_by": actor,
        "created_at": now or _now_iso(),
    }
    inserted = client.table(RESOLUTION_LOG_TABLE).insert(row).execute().data or []
    return inserted[0] if inserted else row


def apply_auto_resolution(client, plan, *, actor=None, now=None, reconcile_fn=None):
    """Persist one auto-resolution: write the alias, tag the receipts, log it,
    and re-reconcile every affected business date. Returns the stored log row."""
    alias_id = _insert_alias(client, plan["raw_merchant"], plan["canonical_id"], plan["confidence"])
    for rid in plan["receipt_ids"]:
        client.table(RECEIPTS_TABLE).update(
            {"merchant_canonical_id": plan["canonical_id"]}
        ).eq("id", rid).execute()
    log_row = record_log(client, plan, STATUS_ACTIVE, alias_id=alias_id, actor=actor, now=now)
    if plan["affected_dates"]:
        (reconcile_fn or _default_reconcile)(client, plan["affected_dates"])
    return log_row


def empty_stats() -> dict:
    return {
        "auto_resolved": 0,
        "escalated": 0,
        "deferred": 0,
        "receipts_tagged": 0,
        "affected_dates": [],
    }


def resolve_all(client, *, actor=None, now=None, reconcile_fn=None) -> dict:
    """One-pass backfill (Part 6). Score every backlog merchant, act on the
    decision, and re-reconcile affected dates ONCE at the end (so a date touched
    by several merchants is reconciled a single time). Returns counts."""
    now = now or _now_iso()
    aliases, canonicals = load_snapshot(client)
    backlog = fetch_unresolved_merchants(client)

    stats = empty_stats()
    affected: set = set()
    for raw, info in backlog.items():
        plan = plan_resolution(raw, info["rm"], info["receipts"], aliases, canonicals)
        if plan["decision"] == DECISION_AUTO and plan["canonical_id"] is not None:
            # Tag + alias + log, but defer reconciliation to one batch below.
            apply_auto_resolution(client, plan, actor=actor, now=now,
                                  reconcile_fn=lambda *_a, **_k: None)
            affected |= set(plan["affected_dates"])
            stats["auto_resolved"] += 1
            stats["receipts_tagged"] += len(plan["receipt_ids"])
        elif plan["decision"] == DECISION_ESCALATE:
            record_log(client, plan, STATUS_ACTIVE, actor=actor, now=now)
            stats["escalated"] += 1
        else:
            record_log(client, plan, STATUS_ACTIVE, actor=actor, now=now)
            stats["deferred"] += 1

    dates = sorted(affected)
    if dates:
        (reconcile_fn or _default_reconcile)(client, dates)
    stats["affected_dates"] = dates
    return stats


def fetch_log(client, log_id) -> dict | None:
    rows = (
        client.table(RESOLUTION_LOG_TABLE).select("*").eq("id", log_id).limit(1).execute().data
        or []
    )
    return rows[0] if rows else None


def undo_resolution(client, log_id, *, now=None, reconcile_fn=None) -> dict | None:
    """Reverse one auto-resolution (Part 4, ``/merchant_undo``): untag the
    receipts, delete the alias, mark the log row undone, and re-reconcile the
    affected dates so food cost reverts. No-op (returns None) if the log row is
    missing, not an auto-resolution, or already undone."""
    row = fetch_log(client, log_id)
    if not row or row.get("decision") != DECISION_AUTO or row.get("status") != STATUS_ACTIVE:
        return None
    for rid in row.get("receipt_ids") or []:
        client.table(RECEIPTS_TABLE).update(
            {"merchant_canonical_id": None}
        ).eq("id", rid).execute()
    if row.get("alias_id") is not None:
        client.table(ALIAS_TABLE).delete().eq("id", row["alias_id"]).execute()
    client.table(RESOLUTION_LOG_TABLE).update(
        {"status": STATUS_UNDONE, "undone_at": now or _now_iso()}
    ).eq("id", log_id).execute()
    dates = row.get("affected_dates") or []
    if dates:
        (reconcile_fn or _default_reconcile)(client, dates)
    return row


# === Part 5: owner review queue + digest line (pure) =========================

def fetch_review_queue(client) -> list:
    """Active escalations, highest RM at stake first."""
    rows = (
        client.table(RESOLUTION_LOG_TABLE)
        .select("*")
        .eq("decision", DECISION_ESCALATE)
        .eq("status", STATUS_ACTIVE)
        .execute()
        .data
        or []
    )
    return rank_review_queue(rows)


def rank_review_queue(rows) -> list:
    """Pure: order review rows by RM at stake descending (ties broken by risk
    then name so the order is stable)."""
    return sorted(
        rows,
        key=lambda r: (-_to_float(r.get("rm_at_stake")), -_to_float(r.get("risk")),
                       r.get("raw_merchant") or ""),
    )


def _conf_pct(value) -> str:
    return f"{_to_float(value) * 100:.0f}%"


def format_review_queue(queue, canonicals_by_id=None) -> str:
    if not queue:
        return "Merchant review queue is empty — nothing escalated."
    canonicals_by_id = canonicals_by_id or {}
    lines = [f"Merchant review queue ({len(queue)}) — highest RM at stake first:"]
    for r in queue:
        cid = r.get("canonical_id")
        guess = canonicals_by_id.get(cid) or (f"#{cid}" if cid is not None else "no match")
        lines.append(
            f"  [{r.get('id')}] {r.get('raw_merchant')!r}  RM{_to_float(r.get('rm_at_stake')):,.2f}"
            f"  (conf {_conf_pct(r.get('confidence'))}, risk {_to_float(r.get('risk')):.0f})"
            f"  -> best guess: {guess}"
        )
    lines.append("")
    lines.append("Confirm with /merchant_add_alias <canonical_id> <raw text>, "
                 "then /merchant_resolve_now.")
    return "\n".join(lines)


def format_review_digest_line(queue) -> str | None:
    """One-line digest nudge, or None when the queue is empty. Emitted from the
    nightly digest only, which is itself once-per-day — so this is inherently
    no-spam (one line a day, and only when something is actually waiting)."""
    if not queue:
        return None
    total = sum(_to_float(r.get("rm_at_stake")) for r in queue)
    top = queue[0]
    return (
        f"🧾 Merchant review: {len(queue)} merchant(s), RM{total:,.0f} at stake — "
        f"top {top.get('raw_merchant')!r} (RM{_to_float(top.get('rm_at_stake')):,.0f}). "
        "See /merchant_review"
    )


def format_resolve_report(stats) -> str:
    dates = stats.get("affected_dates") or []
    lines = [
        "Merchant auto-resolve pass complete:",
        f"  Auto-resolved:        {stats.get('auto_resolved', 0)}",
        f"  Escalated to review:  {stats.get('escalated', 0)}",
        f"  Deferred (long tail): {stats.get('deferred', 0)}",
        f"  Receipts tagged:      {stats.get('receipts_tagged', 0)}",
    ]
    if dates:
        span = dates[0] if len(dates) == 1 else f"{dates[0]} .. {dates[-1]}"
        lines.append(f"  Re-reconciled {len(dates)} business date(s): {span}")
    lines.append("")
    lines.append(
        f"  Policy: auto-resolve at conf >= {AUTO_RESOLVE_CONF_CUTOFF:.0%}; "
        f"below that, escalate when risk >= RM{ESCALATION_RISK_THRESHOLD:.0f} else defer."
    )
    if stats.get("escalated"):
        lines.append("  Next: /merchant_review to clear the escalation queue.")
    return "\n".join(lines)
