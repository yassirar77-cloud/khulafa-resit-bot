"""Authorised manual-review reviewers (PR #29b).

Chat IDs are pulled from environment variables, never hardcoded. For v1
only Yassir is wired up; set ``ARIFFIN_CHAT_ID`` later to add him without a
code change. Datuk Wahith intentionally gets the nightly digest only, not
the review buttons.

Environment variables:
  YASSIR_CHAT_ID   -- enables review-queue DMs to Yassir.
  ARIFFIN_CHAT_ID  -- optional; add once the flow is proven.
"""

import os

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
            continue
    return frozenset(ids)


REVIEWER_CHAT_IDS = _load_reviewer_ids()


def is_reviewer(chat_id) -> bool:
    """True if ``chat_id`` is an authorised reviewer. Non-reviewers (and
    malformed ids) get ``False`` so their button taps are silently ignored."""
    try:
        return int(chat_id) in REVIEWER_CHAT_IDS
    except (TypeError, ValueError):
        return False
