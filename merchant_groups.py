"""Group raw merchant spellings under their canonical supplier name.

``receipts.merchant`` is the OCR'd spelling ("AYAM BERLIAN SDN BHD",
"AYAM BERLIAN SDN. BHD.", "AYAM BERLIAN SON BHD"), and
``receipts.merchant_canonical_id`` is not populated, so the supplier
scorecard and the cash forecast would score and project each spelling as a
separate supplier. This maps every raw spelling to the ``merchant_canonical``
display name through ``merchant_resolver.match_merchant`` (the same exact /
case-insensitive / normalised / substring / fuzzy tiers the bot uses), and
leaves a spelling that matches nothing as itself.

Pure: ``name_map`` takes the alias and canonical rows; ``load`` fetches them
and never raises (an empty snapshot means "no grouping").
"""
from __future__ import annotations

import logging

from merchant_resolver import load_snapshot, match_merchant

logger = logging.getLogger(__name__)


def _upper(value) -> str:
    return " ".join(str(value or "").split()).upper()


def name_map(raw_names, aliases, canonicals) -> dict[str, str]:
    """``{raw merchant upper-cased: canonical display name upper-cased}``;
    a raw name that resolves to nothing maps to itself."""
    display = {c.get("id"): _upper(c.get("display_name")) for c in canonicals or []}
    out: dict[str, str] = {}
    for raw in raw_names or []:
        key = _upper(raw)
        if not key or key in out:
            continue
        canonical_id, _conf = match_merchant(key, aliases or [], canonicals or [])
        out[key] = display.get(canonical_id) or key
    return out


def load(client) -> tuple[list, list]:
    """``(aliases, canonicals)`` from the live tables; ``([], [])`` on any
    failure so the callers simply fall back to raw spellings."""
    try:
        return load_snapshot(client)
    except Exception:
        logger.warning("merchant_groups: could not load the merchant snapshot", exc_info=True)
        return [], []
