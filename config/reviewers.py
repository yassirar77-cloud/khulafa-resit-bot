"""Authorised manual-review reviewers (PR #29b).

Chat IDs are pulled from environment variables, never hardcoded. For v1
only Yassir is wired up; set ``ARIFFIN_CHAT_ID`` later to add him without a
code change. Datuk Wahith intentionally gets the nightly digest only, not
the review buttons.

Environment variables:
  YASSIR_CHAT_ID   -- enables review-queue DMs to Yassir.
  ARIFFIN_CHAT_ID  -- optional; add once the flow is proven.
"""

import logging
import os

logger = logging.getLogger(__name__)

_REVIEWER_ENV_VARS = ("YASSIR_CHAT_ID", "ARIFFIN_CHAT_ID")


def _load_reviewer_ids() -> frozenset:
    ids = set()
    for var in _REVIEWER_ENV_VARS:
        raw = os.environ.get(var)
        if not raw or not raw.strip():
            continue
        try:
            ids.add(int(raw.strip()))
        except ValueError:
            logger.warning("Reviewer env var %s is set but not a numeric chat id — ignored", var)
            continue
    if not ids:
        # Silent-lockout guard: with no reviewers resolved, every admin command
        # and review button is ignored with NO feedback — the bot looks dead to
        # the owner. Make the misconfiguration loud at import time.
        logger.warning(
            "No reviewer chat ids resolved (set YASSIR_CHAT_ID / ARIFFIN_CHAT_ID) — "
            "ALL admin commands and review buttons will be silently ignored."
        )
    return frozenset(ids)


REVIEWER_CHAT_IDS = _load_reviewer_ids()


def is_reviewer(chat_id) -> bool:
    """True if ``chat_id`` is an authorised reviewer. Non-reviewers (and
    malformed ids) get ``False`` so their button taps are silently ignored."""
    try:
        return int(chat_id) in REVIEWER_CHAT_IDS
    except (TypeError, ValueError):
        return False
