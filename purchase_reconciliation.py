"""Smart receipt / POS-payout reconciliation (PR #37) — pure functions.

A cashier paying a supplier RM60 cash leaves two traces of the SAME spend: the
POS prints "PAY TO AIS RM60" (-> sales_daily_payouts) AND the supplier hands a
receipt that gets photographed (-> receipts). Summing both double-counts and
inflates food cost %. This module deduplicates them.

Receipt / payout taxonomy:
  A  matched          receipt has a matching POS payout       -> count receipt, skip payout
  B  cash_no_receipt  POS payout with no receipt              -> count payout (cash WAS spent)
  C  account_only     receipt with no POS payout              -> account purchase, count receipt
  D  excluded_staff   PAY [LP] ... staff advance/leave pay    -> not food, excluded
  E  excluded_utility PAY TO GAS / TNB / water ...            -> not food, excluded

No I/O here: the DB glue (fetch rows, UPSERT) lives in reconciliation_service.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# --- value objects -----------------------------------------------------------

@dataclass(frozen=True)
class Receipt:
    id: int | None = None
    amount: float = 0.0
    merchant_canonical: str | None = None   # canonical display name, if resolved
    merchant: str | None = None             # raw merchant string
    receipt_type: str | None = None         # SUPPLIER_PURCHASE | PETTY_CASH | UNKNOWN | ...
    created_at: str | None = None           # upload time, for deterministic tie-breaks


# Classification flag stored per receipt-bearing match-log row, so the digest
# can call out how much food cost came from un-canonicalised merchants.
_CLASSIFICATION = {
    "SUPPLIER_PURCHASE": "classified_supplier",
    "PETTY_CASH": "petty_cash",
    "UNKNOWN": "unknown_included",
}


def classify_receipt_status(receipt_type) -> str:
    """Map a receipt_type to its food-cost classification flag."""
    return _CLASSIFICATION.get(receipt_type or "UNKNOWN", "unknown_included")


@dataclass(frozen=True)
class POSPayout:
    id: int | None = None
    description: str = ""                    # e.g. "PAY TO BABAS"
    vendor_name: str | None = None           # e.g. "BABAS"
    amount: float = 0.0
    created_at: str | None = None            # POS event time, for tie-breaks


@dataclass(frozen=True)
class Match:
    receipt: Receipt
    payout: POSPayout
    confidence: float
    method: str


@dataclass
class ReconciliationResult:
    matched: list[Match] = field(default_factory=list)              # Type A
    cash_no_receipt: list[POSPayout] = field(default_factory=list)  # Type B
    account_only_receipts: list[Receipt] = field(default_factory=list)  # Type C
    excluded_staff: list[POSPayout] = field(default_factory=list)   # Type D
    excluded_utility: list[POSPayout] = field(default_factory=list)  # Type E


# --- description parsing -----------------------------------------------------

# Leading POS verbs/prefixes: "PAY TO BABAS", "PAY [LP] KARUNGARAJ", "PAYOUT TO
# GAS", "BAYAR KEPADA ...". Strip them to recover the bare merchant/payee.
_PREFIX_RE = re.compile(
    r"^\s*(?:pay(?:out)?|bayar)\b\s*(?:\[[^\]]*\]\s*)?(?:to|kepada|kpd)?\s*",
    re.IGNORECASE,
)

# A bracketed tag right after PAY marks a staff/non-supplier payout, e.g.
# "PAY [LP] KARUNGARAJ" (LP = leave pay), "PAY [AD] ..." (advance).
_STAFF_TAG_RE = re.compile(r"^\s*pay(?:out)?\s*\[[^\]]*\]", re.IGNORECASE)

_STAFF_KEYWORDS = (
    "leave pay", "advance", "advans", "gaji", "salary", "elaun",
    "bonus", "wages", "upah", "komisen", "commission",
)

# Utility payees. Matched as whole tokens so "AIS" (ice, a supply) never trips
# "AIR" (water), and "GAS" doesn't match inside an unrelated word.
_UTILITY_TOKENS = frozenset({
    "gas", "tnb", "tenaga", "astro", "unifi", "tm", "telekom", "syabas",
    "water", "air", "elektrik", "electric", "petronas", "shell", "indah",
    "sewerage", "iwk",
})
_UTILITY_PHRASES = ("air selangor", "indah water", "tenaga nasional", "syarikat air")

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_SIGNIFICANT_TOKEN_MIN = 3


def strip_pos_prefix(description) -> str:
    """Recover the payee from a POS payout description: strip a leading
    PAY/PAYOUT/BAYAR verb, an optional ``[LP]``-style tag, and a TO/KEPADA."""
    if not isinstance(description, str):
        return ""
    return _PREFIX_RE.sub("", description).strip()


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def is_staff_advance(description) -> bool:
    """True for staff payouts (leave pay, advances, salary) — not food cost."""
    if not isinstance(description, str) or not description.strip():
        return False
    if _STAFF_TAG_RE.search(description):
        return True
    low = description.lower()
    return any(kw in low for kw in _STAFF_KEYWORDS)


def is_utility(description) -> bool:
    """True for utility payouts (gas, electricity, water, telco) — not food."""
    if not isinstance(description, str) or not description.strip():
        return False
    payee = strip_pos_prefix(description).lower()
    if any(phrase in payee for phrase in _UTILITY_PHRASES):
        return True
    return any(tok in _UTILITY_TOKENS for tok in _tokens(payee))


# --- merchant matching -------------------------------------------------------

def fuzzy_match_merchant(pos_description, canonical_merchants):
    """Resolve a POS payout description to a canonical merchant name.

    Strips the PAY-TO prefix, upper-cases, then matches against the canonical
    list: exact first, else best substring overlap (either direction on whole
    words). Returns ``(best_canonical | None, confidence_0_to_1)``.
    """
    payee = strip_pos_prefix(pos_description).upper().strip()
    if not payee:
        return None, 0.0
    payee_tokens = set(_tokens(payee))
    best = None  # (canonical, score)
    for canon in canonical_merchants:
        c = (canon or "").upper().strip()
        if not c:
            continue
        if c == payee:
            return canon, 1.0
        c_tokens = set(_tokens(c))
        if not c_tokens:
            continue
        # Whole-canonical contained in payee (or vice versa), or shared
        # significant token. Score by token overlap fraction.
        overlap = payee_tokens & c_tokens
        significant = {t for t in overlap if len(t) >= _SIGNIFICANT_TOKEN_MIN}
        if c_tokens <= payee_tokens or payee_tokens <= c_tokens or significant:
            score = len(overlap) / max(len(payee_tokens), len(c_tokens))
            if best is None or score > best[1]:
                best = (canon, score)
    if best is not None:
        return best[0], round(min(max(best[1], 0.1), 0.99), 2)
    return None, 0.0


def _merchant_score(payee: str, payout_canonical, receipt: Receipt) -> int:
    """2 = same canonical merchant; 1 = fuzzy name overlap; 0 = no match."""
    rc_canonical = (receipt.merchant_canonical or "").upper().strip()
    if payout_canonical and rc_canonical and payout_canonical.upper().strip() == rc_canonical:
        return 2
    payee_tokens = set(_tokens(payee))
    if not payee_tokens:
        return 0
    for candidate in (receipt.merchant_canonical, receipt.merchant):
        cand = (candidate or "").upper().strip()
        if not cand:
            continue
        cand_tokens = set(_tokens(cand))
        if not cand_tokens:
            continue
        if payee_tokens <= cand_tokens or cand_tokens <= payee_tokens:
            return 1
        shared = payee_tokens & cand_tokens
        if any(len(t) >= _SIGNIFICANT_TOKEN_MIN for t in shared):
            return 1
    return 0


def amount_diff_within_tolerance(a, b, abs_tol: float, pct_tol: float) -> bool:
    diff = abs((a or 0.0) - (b or 0.0))
    return diff <= abs_tol or diff <= max(abs(a or 0.0), abs(b or 0.0)) * pct_tol


def _amount_score(a, b, abs_tol: float, pct_tol: float) -> int:
    """2 = exact; 1 = within tolerance; 0 = out of tolerance."""
    if abs((a or 0.0) - (b or 0.0)) < 0.005:
        return 2
    return 1 if amount_diff_within_tolerance(a, b, abs_tol, pct_tol) else 0


_CONFIDENCE = {
    (2, 2): (1.0, "exact_amount_exact_merchant"),
    (1, 2): (0.9, "fuzzy_amount_exact_merchant"),
    (2, 1): (0.8, "exact_amount_fuzzy_merchant"),
    (1, 1): (0.7, "fuzzy_amount_fuzzy_merchant"),
}

# Amount-only fallback: ~95% of receipts have no canonical merchant, so a
# merchant-anchored match never fires and the receipt AND its POS payout both
# count -> food cost reads ~2x reality. When the merchant can't be matched we
# fall back to amount alone, on a deliberately tight tolerance so same-amount-
# but-different-spend pairs don't get falsely merged. Tagged so it's auditable.
AMOUNT_ONLY_TOLERANCE_ABS = 2.0
AMOUNT_ONLY_CONFIDENCE = 0.5
AMOUNT_ONLY_METHOD = "amount_only"

# Candidate tiers, lowest assigned first: merchant-anchored matches always win
# over amount-only ones (a receipt prefers its real supplier payout).
_TIER_MERCHANT = 0
_TIER_AMOUNT_ONLY = 1


def _sortable_time(ts) -> str:
    """ISO timestamps sort lexicographically; None sorts first (deterministic)."""
    return "" if ts is None else str(ts)


def _tie_break_key(payout: POSPayout, receipt: Receipt) -> tuple:
    """Earliest-by-time, then id, so equal-distance candidates resolve the same
    way on every run regardless of input ordering."""
    return (
        _sortable_time(payout.created_at), payout.id or 0,
        _sortable_time(receipt.created_at), receipt.id or 0,
    )


def match_receipts_to_payouts(
    receipts,
    pos_payouts,
    canonical_merchants,
    amount_tolerance_abs: float = 5.0,
    amount_tolerance_pct: float = 0.02,
    amount_only_tolerance_abs: float = AMOUNT_ONLY_TOLERANCE_ABS,
) -> ReconciliationResult:
    """The smart-merge core. ``receipts`` are supplier-purchase Receipts;
    ``pos_payouts`` are POSPayouts from sales_daily_payouts (already partitioned
    by outlet + business_date upstream).

    Two-tier greedy assignment:
      1. merchant-anchored: same merchant + amount within ±RM5 (the original
         path), highest confidence / closest amount first;
      2. amount-only fallback: when the merchant is null/unmatched, amount alone
         within ±RM2, closest amount first.
    Merchant matches are always assigned before amount-only ones; each receipt
    and each payout is consumed at most once, and a deterministic tie-break
    (earliest time, then id) keeps the result stable across runs."""
    result = ReconciliationResult()

    food_payouts: list[POSPayout] = []
    for p in pos_payouts:
        if is_staff_advance(p.description):
            result.excluded_staff.append(p)
        elif is_utility(p.description):
            result.excluded_utility.append(p)
        else:
            food_payouts.append(p)

    # Build all viable candidate pairs, then assign greedily.
    candidates = []  # (sort_key, payout_idx, receipt_idx, confidence, method)
    for pi, payout in enumerate(food_payouts):
        payout_canonical, _ = fuzzy_match_merchant(payout.description, canonical_merchants)
        payee = strip_pos_prefix(payout.description) or (payout.vendor_name or "")
        for ri, receipt in enumerate(receipts):
            diff = abs((receipt.amount or 0.0) - (payout.amount or 0.0))
            m_score = _merchant_score(payee, payout_canonical, receipt)
            tie = _tie_break_key(payout, receipt)
            if m_score > 0:
                a_score = _amount_score(
                    receipt.amount, payout.amount, amount_tolerance_abs, amount_tolerance_pct
                )
                if a_score == 0:
                    continue
                confidence, method = _CONFIDENCE[(a_score, m_score)]
                sort_key = (_TIER_MERCHANT, -confidence, diff, tie)
                candidates.append((sort_key, pi, ri, confidence, method))
            elif diff <= amount_only_tolerance_abs:
                # Merchant null/unmatched -> amount-only fallback (±RM2).
                sort_key = (_TIER_AMOUNT_ONLY, -AMOUNT_ONLY_CONFIDENCE, diff, tie)
                candidates.append(
                    (sort_key, pi, ri, AMOUNT_ONLY_CONFIDENCE, AMOUNT_ONLY_METHOD)
                )

    candidates.sort(key=lambda c: c[0])
    used_payouts: set[int] = set()
    used_receipts: set[int] = set()
    for _key, pi, ri, confidence, method in candidates:
        if pi in used_payouts or ri in used_receipts:
            continue
        used_payouts.add(pi)
        used_receipts.add(ri)
        result.matched.append(
            Match(receipt=receipts[ri], payout=food_payouts[pi],
                  confidence=confidence, method=method)
        )

    for pi, payout in enumerate(food_payouts):
        if pi not in used_payouts:
            result.cash_no_receipt.append(payout)
    for ri, receipt in enumerate(receipts):
        if ri not in used_receipts:
            result.account_only_receipts.append(receipt)
    return result


# --- money roll-ups ----------------------------------------------------------

def _sum(items, attr):
    return round(sum((getattr(i, attr) or 0.0) for i in items), 2)


def compute_food_cost_percent(
    matched_value: float,
    account_only_value: float,
    cash_no_receipt_value: float,
    sales_total,
) -> float | None:
    """Food cost % = (Type A + Type C + Type B purchases) / sales x 100.

    Type B (cash paid, no receipt) is counted: the cash WAS spent. Returns
    ``None`` when sales is zero/missing (no division by zero)."""
    s = None
    try:
        s = None if sales_total is None else float(sales_total)
    except (TypeError, ValueError):
        s = None
    if s is None or s <= 0:
        return None
    purchases = (matched_value or 0.0) + (account_only_value or 0.0) + (cash_no_receipt_value or 0.0)
    return round(purchases / s * 100.0, 2)


def summarize(result: ReconciliationResult, outlet_canonical: str, business_date,
              sales_total=None) -> dict:
    """Build the purchase_reconciliation row (no id) for a UPSERT."""
    matched_value = _sum([m.receipt for m in result.matched], "amount")
    account_value = _sum(result.account_only_receipts, "amount")
    cash_value = _sum(result.cash_no_receipt, "amount")
    total_food = round(matched_value + account_value + cash_value, 2)
    total_receipts = len(result.matched) + len(result.account_only_receipts)
    total_payouts = (
        len(result.matched) + len(result.cash_no_receipt)
        + len(result.excluded_staff) + len(result.excluded_utility)
    )
    return {
        "outlet_canonical": outlet_canonical,
        "business_date": str(business_date),
        "total_receipts": total_receipts,
        "total_pos_payouts": total_payouts,
        "matched_count": len(result.matched),
        "unmatched_receipts": len(result.account_only_receipts),
        "unmatched_pos_payouts": len(result.cash_no_receipt),
        "matched_value": matched_value,
        "unmatched_receipt_value": account_value,
        "unmatched_pos_value": cash_value,
        "total_food_purchases": total_food,
        "sales_total": (None if sales_total is None else round(float(sales_total), 2)),
        "food_cost_percent": compute_food_cost_percent(
            matched_value, account_value, cash_value, sales_total
        ),
    }


def build_match_log(result: ReconciliationResult) -> list[dict]:
    """One purchase_match_log row per item (reconciliation_id filled in later)."""
    rows: list[dict] = []
    for m in result.matched:
        rows.append({
            "match_type": "A_matched",
            "receipt_id": m.receipt.id,
            "pos_payout_id": m.payout.id,
            "amount": round(m.receipt.amount or 0.0, 2),
            "merchant_or_description": m.receipt.merchant_canonical or m.payout.description,
            "match_confidence": m.confidence,
            "match_method": m.method,
            "receipt_classification": classify_receipt_status(m.receipt.receipt_type),
        })
    for p in result.cash_no_receipt:
        rows.append({
            "match_type": "B_cash_no_receipt",
            "receipt_id": None,
            "pos_payout_id": p.id,
            "amount": round(p.amount or 0.0, 2),
            "merchant_or_description": p.description,
            "match_confidence": None,
            "match_method": None,
            "receipt_classification": None,
        })
    for r in result.account_only_receipts:
        rows.append({
            "match_type": "C_account_only",
            "receipt_id": r.id,
            "pos_payout_id": None,
            "amount": round(r.amount or 0.0, 2),
            "merchant_or_description": r.merchant_canonical or r.merchant,
            "match_confidence": None,
            "match_method": None,
            "receipt_classification": classify_receipt_status(r.receipt_type),
        })
    for p in result.excluded_staff:
        rows.append({
            "match_type": "D_excluded_staff",
            "receipt_id": None,
            "pos_payout_id": p.id,
            "amount": round(p.amount or 0.0, 2),
            "merchant_or_description": p.description,
            "match_confidence": None,
            "match_method": None,
            "receipt_classification": None,
        })
    for p in result.excluded_utility:
        rows.append({
            "match_type": "E_excluded_utility",
            "receipt_id": None,
            "pos_payout_id": p.id,
            "amount": round(p.amount or 0.0, 2),
            "merchant_or_description": p.description,
            "match_confidence": None,
            "match_method": None,
            "receipt_classification": None,
        })
    return rows
