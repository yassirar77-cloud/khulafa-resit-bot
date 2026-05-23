"""Deterministic receipt-type classifier (PR #24).

Runs immediately after OCR/parsing and before any downstream logic
(price aggregation, anomaly checks, supplier ledger updates). Routes
each receipt into one of six buckets so that price anomaly alerts only
fire on real supplier purchases — not on cash advances, utility bills,
licence fees, or petty-cash petrol receipts.

Pure regex + keyword matching. No LLM calls. Speed matters: runs on
every receipt.

Keyword lists are module-level constants — edit them in place to tune
classification without touching the matching logic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


class ReceiptType(str, Enum):
    SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"
    STAFF_ADVANCE = "STAFF_ADVANCE"
    UTILITY = "UTILITY"
    RENT_LICENSE = "RENT_LICENSE"
    PETTY_CASH = "PETTY_CASH"
    UNKNOWN = "UNKNOWN"


@dataclass
class ClassificationResult:
    receipt_type: ReceiptType
    confidence: float
    matched_keywords: list[str] = field(default_factory=list)
    extracted_staff_name: Optional[str] = None
    extracted_vendor: Optional[str] = None


# --- Keyword tables -------------------------------------------------------
# Substring matches on the upper-cased combined OCR text. Order within each
# list doesn't matter; classifier priority order is enforced by
# `classify_receipt`.

STAFF_ADVANCE_KEYWORDS = [
    "PAYOUT",
    "PINJAM",
    "PINJAMAN",
    "ADVANCE",
    "ADVANS",
    "PENDAHULUAN",
    "LOAN",
]

# Secondary signals that strengthen a STAFF_ADVANCE match when combined
# with a `TO <NAME>` or `BY CASH` line. Kept separate so a TNB bill that
# happens to mention "BY CASH" doesn't get misclassified.
STAFF_ADVANCE_BY_CASH = "BY CASH"

UTILITY_KEYWORDS = [
    "TNB",
    "TENAGA NASIONAL",
    "SYABAS",
    "AIR SELANGOR",
    "INDAH WATER",
    "IWK",
    "UNIFI",
    "MAXIS",
    "CELCOM",
    "DIGI",
    "TIME",
]

RENT_LICENSE_KEYWORDS = [
    "SEWA",
    "RENTAL",
    "LESEN",
    "LICENSE",
    "LICENCE",
    "MAJLIS",
    "MBSA",
    "MPSJ",
    "DBKL",
    "KWSP",
    "PERKESO",
    "SOCSO",
    "LHDN",
]

PETTY_CASH_KEYWORDS = [
    "RUNNER",
    "TAMBANG",
    "TOL",
    "PARKING",
    "MINYAK KERETA",
    "PETROL",
    "SHELL",
    "PETRONAS",
    "CALTEX",
]
PETTY_CASH_MAX_TOTAL = 200.0

# Strict whitelist for SUPPLIER_PURCHASE — case-insensitive substring
# match on the merchant header or combined OCR text. This is the ONLY
# way a receipt becomes SUPPLIER_PURCHASE; there is no itemised-SKU
# fallback (the previous fallback misclassified MYMOON'S KITCHEN-style
# dine-in receipts as supplier invoices).
#
# Substrings are intentionally short to survive OCR noise, e.g. "MOON"
# catches all observed misreads of MYMOON'S KITCHEN (MYMOOH'S, MTMOON'S,
# MYMOOK'S, MYROOK'S, MTMOOK'S, MYNOOK'S, MYMCON'S, MIMOON'S, MY MOON'S).
# "BESTARI" covers both BESTARI FARM and BESTARI WHOLESALE.
#
# New suppliers always start as UNKNOWN until added to this list — that
# is by design. Better to drop a receipt than crash the pipeline or
# bill the wrong category.
SUPPLIER_WHITELIST = [
    "BABAS",
    "SAIDA",
    "JASMINE",
    "MEWAH",
    "HANEE",
    "CAMELLIAA",
    "JY RESOURCES",
    "JUTA RIA",
    "BS FROZEN",
    "REZA",
    "BALAJI",
    "BESTARI",
    "FOOK LEONG",
    "DAILY PAY",
    "SHREE MAP",
    "QUIWAVE",
    "EVEREST",
    # MYMOON'S KITCHEN OCR variants. Both substrings are kept (MOON
    # listed first so the canonical "MYMOON'S" matches the shorter, more
    # generic token). Coverage: 6 of 10 observed variants — the four
    # MYROOK / MTMOOK / MYNOOK / MYMCON misreads need fuzzy matching.
    "MOON",
    "MYMOO",
]


# --- Helpers --------------------------------------------------------------

def _build_combined_text(
    ocr_text: str,
    parsed_items: Iterable[Any],
    merchant: str = "",
) -> str:
    """Build the upper-cased haystack used for keyword matching.

    Includes the merchant header (when available), the raw OCR text, AND
    a flattened view of parsed item names. The merchant must be folded
    in explicitly: some OCR providers return a clean `merchant` field
    but a sparse `raw_text` that omits the header, which would otherwise
    cause whitelist matches to silently fail.
    """
    parts: list[str] = []
    if merchant:
        parts.append(str(merchant))
    if ocr_text:
        parts.append(ocr_text)
    if parsed_items:
        for it in parsed_items:
            if isinstance(it, dict):
                name = it.get("name")
                if name:
                    parts.append(str(name))
            elif it:
                parts.append(str(it))
    return " ".join(parts).upper()


def _find_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    return [kw for kw in keywords if kw in text]


# Patterns for `TO PINJAM TO <NAME>`, `PINJAM <NAME>`, `ADVANCE <NAME>`,
# `PAYOUT ... TO <NAME>`. Capture a single English/Malay name token
# (letters only, 2-30 chars). Multi-language scripts are out of scope per
# the brief.
_NAME_PATTERNS = [
    re.compile(r"TO\s+PINJAM\s+TO\s+([A-Z][A-Z]{1,29})", re.IGNORECASE),
    re.compile(r"PINJAM\s+TO\s+([A-Z][A-Z]{1,29})", re.IGNORECASE),
    re.compile(r"PINJAM\s+([A-Z][A-Z]{1,29})", re.IGNORECASE),
    re.compile(r"ADVANCE\s+(?:TO\s+)?([A-Z][A-Z]{1,29})", re.IGNORECASE),
    re.compile(r"PENDAHULUAN\s+([A-Z][A-Z]{1,29})", re.IGNORECASE),
    re.compile(r"LOAN\s+(?:TO\s+)?([A-Z][A-Z]{1,29})", re.IGNORECASE),
    re.compile(r"PAYOUT.{0,60}?TO\s+([A-Z][A-Z]{1,29})", re.IGNORECASE | re.DOTALL),
]

# Words that look like names but aren't — filter these out so we don't
# return "CASH" or "PINJAM" as a staff name.
_NAME_STOPWORDS = {
    "CASH", "PINJAM", "PAYOUT", "ADVANCE", "ADVANS", "PENDAHULUAN",
    "LOAN", "TO", "BY", "FROM", "FOR", "AND", "OR", "THE", "ADMIN",
    "ISSUED", "RM", "MYR",
}


def extract_staff_name(text: str) -> Optional[str]:
    """Best-effort name extraction from a STAFF_ADVANCE receipt body.

    Returns title-cased name (e.g. "Dina") or None if nothing matches.
    """
    if not text:
        return None
    for pat in _NAME_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        candidate = m.group(1).strip().upper()
        if candidate in _NAME_STOPWORDS or len(candidate) < 2:
            continue
        return candidate.title()
    return None


# `ADMIN ISSUED BY <NAME>` for the `issued_by` field on staff_advances.
_ISSUED_BY_PATTERN = re.compile(
    r"(?:ADMIN\s+)?ISSUED\s+BY\s+([A-Z][A-Z\s]{1,40}?)(?:\n|$|[\.,;])",
    re.IGNORECASE,
)


def extract_issued_by(text: str) -> Optional[str]:
    if not text:
        return None
    m = _ISSUED_BY_PATTERN.search(text)
    if not m:
        return None
    candidate = m.group(1).strip()
    if not candidate:
        return None
    return candidate.title()


def _match_supplier_whitelist(text: str) -> Optional[str]:
    for supplier in SUPPLIER_WHITELIST:
        if supplier in text:
            return supplier
    return None


def _match_keywords_in_merchant(
    merchant: Optional[str], keywords: Iterable[str]
) -> Optional[str]:
    """Substring match a keyword list against ONLY the merchant string.

    Used by the priority-2 tier so a clean merchant header
    (EVEREST / TENAGA NASIONAL / KWSP / ...) is treated as
    authoritative, independent of whatever happens to appear in the
    receipt body. Returns the first matching keyword, or None.

    Matching only the merchant field — not the combined haystack —
    keeps the haystack-level rules (priority 5+) from misfiring on
    addresses, timestamps, or product names that happen to contain a
    keyword. Example: a real TNB bill whose footer says "JALAN MOON"
    still classifies as UTILITY because the merchant is TNB, not a
    whitelisted supplier.
    """
    if not merchant:
        return None
    upper = str(merchant).upper()
    for kw in keywords:
        if kw in upper:
            return kw
    return None


def _match_supplier_whitelist_in_merchant(merchant: Optional[str]) -> Optional[str]:
    return _match_keywords_in_merchant(merchant, SUPPLIER_WHITELIST)


def _match_utility_vendor(text: str) -> Optional[str]:
    for kw in UTILITY_KEYWORDS:
        if kw in text:
            return kw
    return None


def _match_rent_license_vendor(text: str) -> Optional[str]:
    for kw in RENT_LICENSE_KEYWORDS:
        if kw in text:
            return kw
    return None


# --- Main entry point -----------------------------------------------------

def classify_receipt(
    ocr_text: str,
    parsed_items: Optional[list[dict]] = None,
    total: Optional[float] = None,
    merchant: Optional[str] = None,
) -> ClassificationResult:
    """Classify a receipt into one of the ReceiptType buckets.

    Priority order — first match wins:

      Tier A (haystack-based, very specific):
        1. STAFF_ADVANCE

      Tier B (merchant-field-only checks; merchant is authoritative):
        2. SUPPLIER_PURCHASE  (merchant matches SUPPLIER_WHITELIST)
        3. UTILITY            (merchant matches UTILITY_KEYWORDS)
        4. RENT_LICENSE       (merchant matches RENT_LICENSE_KEYWORDS)

      Tier C (haystack fallbacks; for sparse-OCR scenarios where the
              merchant arrives None and the supplier/vendor name only
              appears inside raw_text):
        5. UTILITY            (haystack)
        6. RENT_LICENSE       (haystack)
        7. PETTY_CASH         (haystack, with total cap)
        8. SUPPLIER_PURCHASE  (haystack)
        9. UNKNOWN

    STAFF_ADVANCE runs first because the Khulafa POS prints "PAYOUT" as a
    line item, which would otherwise be misclassified as a purchase SKU.

    Tier B exists so that a clean merchant header — EVEREST, TENAGA
    NASIONAL, KWSP — is treated as authoritative even when the receipt
    body contains substring-noise that would otherwise trip another
    rule. The original bug was supplier receipts being routed to
    UTILITY because the word "TIME" in a `Time: HH:MM` stamp matched
    UTILITY_KEYWORDS (PR #28b). The same defence applies to UTILITY
    and RENT_LICENSE receipts: receipt 1548 (TENAGA NASIONAL,
    RM4,894.75) classified as UNKNOWN in production because the
    pre-PR-28 build never saw the merchant header and the bill body
    had no UTILITY keyword in it. With merchant=TENAGA NASIONAL passed
    in, the priority-3 check fires and routes to UTILITY directly.

    Tier C preserves the PR #28 sparse-OCR safety net: if the OCR
    provider returns the merchant inside `raw_text` rather than as a
    separate field, the haystack-level checks still catch it. New
    merchants always start as UNKNOWN.

    The `merchant` kwarg is folded into the haystack used by Tier A
    and Tier C. Pass it from the caller whenever the OCR response
    includes a merchant field — without it, sparse raw_text payloads
    cause classification to silently fall back to UNKNOWN (the bug
    that produced 132+ EVEREST receipts mis-classified — PR #28).
    """
    parsed_items = parsed_items or []
    text = _build_combined_text(ocr_text or "", parsed_items, merchant or "")
    result: ClassificationResult

    # --- 1. STAFF_ADVANCE ---
    matched = _find_keywords(text, STAFF_ADVANCE_KEYWORDS)
    if matched:
        staff_name = extract_staff_name(text)
        issued_by = extract_issued_by(text)
        result = ClassificationResult(
            receipt_type=ReceiptType.STAFF_ADVANCE,
            confidence=0.95 if staff_name else 0.75,
            matched_keywords=matched,
            extracted_staff_name=staff_name,
            extracted_vendor=issued_by,
        )

    # --- 2. SUPPLIER_PURCHASE (merchant-field whitelist) ---
    elif (supplier := _match_keywords_in_merchant(merchant, SUPPLIER_WHITELIST)):
        result = ClassificationResult(
            receipt_type=ReceiptType.SUPPLIER_PURCHASE,
            confidence=0.95,
            matched_keywords=[supplier],
            extracted_vendor=supplier,
        )

    # --- 3. UTILITY (merchant-field) ---
    elif (kw := _match_keywords_in_merchant(merchant, UTILITY_KEYWORDS)):
        result = ClassificationResult(
            receipt_type=ReceiptType.UTILITY,
            confidence=0.95,
            matched_keywords=[kw],
            extracted_vendor=kw,
        )

    # --- 4. RENT_LICENSE (merchant-field) ---
    elif (kw := _match_keywords_in_merchant(merchant, RENT_LICENSE_KEYWORDS)):
        result = ClassificationResult(
            receipt_type=ReceiptType.RENT_LICENSE,
            confidence=0.95,
            matched_keywords=[kw],
            extracted_vendor=kw,
        )

    # --- 5. UTILITY (haystack fallback) ---
    elif (matched := _find_keywords(text, UTILITY_KEYWORDS)):
        result = ClassificationResult(
            receipt_type=ReceiptType.UTILITY,
            confidence=0.95,
            matched_keywords=matched,
            extracted_vendor=matched[0],
        )

    # --- 6. RENT_LICENSE (haystack fallback) ---
    elif (matched := _find_keywords(text, RENT_LICENSE_KEYWORDS)):
        result = ClassificationResult(
            receipt_type=ReceiptType.RENT_LICENSE,
            confidence=0.95,
            matched_keywords=matched,
            extracted_vendor=matched[0],
        )

    # --- 7. PETTY_CASH ---
    elif (
        (matched := _find_keywords(text, PETTY_CASH_KEYWORDS))
        and (total is None or total < PETTY_CASH_MAX_TOTAL)
    ):
        result = ClassificationResult(
            receipt_type=ReceiptType.PETTY_CASH,
            confidence=0.90,
            matched_keywords=matched,
            extracted_vendor=matched[0],
        )

    # --- 8. SUPPLIER_PURCHASE (combined-haystack whitelist fallback) ---
    #
    # Catches receipts whose OCR provider returned the merchant inside
    # raw_text rather than as a separate field — the merchant-field
    # check at priority 2 missed those. Runs after the haystack
    # UTILITY/RENT/PETTY rules so those still win when the receipt
    # body legitimately matches them and the whitelist token only
    # appears incidentally.
    elif (supplier := _match_supplier_whitelist(text)):
        result = ClassificationResult(
            receipt_type=ReceiptType.SUPPLIER_PURCHASE,
            confidence=0.95,
            matched_keywords=[supplier],
            extracted_vendor=supplier,
        )

    # --- 9. UNKNOWN ---
    else:
        result = ClassificationResult(
            receipt_type=ReceiptType.UNKNOWN,
            confidence=0.0,
            matched_keywords=[],
        )

    # Single decision-summary log line for every classification so Render
    # logs show merchant + type + vendor consistently. The presence of
    # merchant here is the canary for the production gating bug — if it
    # logs as None or empty, the caller forgot to pass it.
    logger.info(
        "classify_receipt: merchant=%r type=%s vendor=%r staff=%r "
        "total=%s items=%d matched=%s",
        merchant,
        result.receipt_type.value,
        result.extracted_vendor,
        result.extracted_staff_name,
        total,
        len(parsed_items),
        result.matched_keywords,
    )
    return result
