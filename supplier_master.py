"""The supplier master: which names are suppliers rather than people.

Extracted from ``bot.py`` (which still imports from here) because the monthly
close needs the same list without pulling in the Telegram/Supabase runtime.
There must be exactly one answer to "is this a supplier?" — the monthly P&L
reclassifies a whole block of spend on it (see ``monthly_close``: the POS prints
supplier payments under a heading that says SALARY), so a name that counts as a
supplier in one module and payroll in another would move real money between
COGS and wages depending on which code path ran.
"""
from __future__ import annotations

KNOWN_SUPPLIERS = [
    # Spices & dry goods
    'BABAS', 'SAIDA', 'BALAJI', 'SHREE MAP JAYA',
    # Rice
    'JASMINE', 'BERAS',
    # Dairy
    'MEWAH', 'F&N', 'DUTCH LADY',
    # Meat & frozen
    'HANEE', 'BS FROZEN', 'BESTARI FARM', 'BESTARI',
    # Tea & coffee
    'CAMELLIAA', 'CAMELLIA', 'BOH',
    # Eggs
    'JY RESOURCES', 'JUTA RIA',
    # Plastics & packaging
    'REZA PLASTIC', 'REZA', 'HAMEED PLASTICS', 'HAMEED',
    # Vegetables
    'SAYUR', 'PASAR BORONG',
    # Drinks & wholesale
    'BESTARI WHOLESALE',
    # Seafood
    'FOOK LEONG', 'QUIWAVE OCEANIC', 'QUIWAVE',
    # Daily consumables
    'DAILY PAY',
    # Ice
    'EVEREST AISVARAM', 'EVEREST',
    # Catering
    'CATERERS AT TANJUNG', 'MYMOON',
    # Convenience
    'KK SUPERMART', 'KK MART',
    # Utility/common chains
    '99 SPEEDMART', '99', 'TESCO', 'LOTUSS', 'GIANT',
]


def is_known_supplier(merchant) -> bool:
    """Check if merchant matches any known supplier (substring, case-insensitive)."""
    if not merchant:
        return False
    m = merchant.upper().strip()
    for known in KNOWN_SUPPLIERS:
        if known in m or m in known:
            return True
    return False


# Short entries whose bidirectional substring match is too loose to decide a
# person-vs-supplier question on its own. '99' matches the staff name "MAT 99";
# 'BOH' matches "BOHARI"; 'BERAS' is a commodity word. They stay in
# KNOWN_SUPPLIERS (the receipt classifier wants them — a receipt saying "99" is
# 99 Speedmart) but the monthly reclassification treats a match on one of these
# ALONE as undecided and flags it rather than moving the money.
AMBIGUOUS_SUPPLIER_TOKENS = frozenset({'99', 'BOH', 'BERAS', 'REZA', 'HAMEED', 'BESTARI'})


def matched_supplier_tokens(name) -> list[str]:
    """Every KNOWN_SUPPLIERS entry that matches ``name``, longest first.

    Returns ``[]`` when nothing matches. Exposed so the monthly reclassification
    can see WHICH token matched and decide whether that match is strong enough
    to act on (see :func:`supplier_match_strength`).
    """
    if not name:
        return []
    m = str(name).upper().strip()
    hits = [k for k in KNOWN_SUPPLIERS if k in m or m in k]
    return sorted(set(hits), key=len, reverse=True)


def supplier_match_strength(name) -> str:
    """Classify a name against the supplier master.

    Returns:
      * ``'strong'``    — matched a supplier token that is not ambiguous
      * ``'ambiguous'`` — matched only short/loose tokens (see
        :data:`AMBIGUOUS_SUPPLIER_TOKENS`); a human must decide
      * ``'none'``      — no match at all

    The monthly close moves 'strong' matches into COGS and flags the rest. It
    never guesses: an ambiguous match is reported with the token that caused it.
    """
    hits = matched_supplier_tokens(name)
    if not hits:
        return "none"
    if any(h not in AMBIGUOUS_SUPPLIER_TOKENS for h in hits):
        return "strong"
    return "ambiguous"
