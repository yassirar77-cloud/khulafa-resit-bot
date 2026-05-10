"""Defensive normalization of OCR ``items`` lists.

The receipt OCR prompt asks for items in dict form
(``[{"name": ..., "qty": ..., "price": ...}, ...]``), but glm-4.6v-flash
occasionally returns plain strings for terse receipts. A real production
crash came from an EVEREST AISVARAM ice receipt where the model returned
``["Tube Ice", "Crush Ice", "Block Ice"]``; every downstream consumer
calls ``.get(...)`` on each entry and blew up with ``AttributeError:
'str' object has no attribute 'get'``.

``normalize_items`` is the safety net that runs once after OCR (and on
verifier corrections) so the rest of the pipeline only ever sees a list
of dicts.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def normalize_items(raw_items: Any) -> list[dict[str, Any]]:
    """Coerce ``raw_items`` into a list of ``{"name": str, ...}`` dicts.

    Behaviour:
      * ``None`` or non-list input -> ``[]``
      * String entry -> ``{"name": str, "qty": None, "price": None}``
        (empty / whitespace-only strings are dropped)
      * Dict entry -> kept as-is
      * Anything else (int, list, ``None``, ...) -> dropped with a warning
    """
    if not isinstance(raw_items, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw_items:
        if isinstance(entry, dict):
            out.append(entry)
        elif isinstance(entry, str):
            name = entry.strip()
            if name:
                out.append({"name": name, "qty": None, "price": None})
        else:
            logger.warning(
                "normalize_items: skipping non-string non-dict entry: %r", entry
            )
    return out
