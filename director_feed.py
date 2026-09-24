"""Director feed — which live, per-receipt alerts reach the director chat.

The director's alert group (``ALERT_CHAT_ID``) used to get a message for
EVERY receipt from EVERY shop ("New receipt logged" + the full item list)
and a separate message for EVERY item that came in 10% over its average.
With several outlets uploading all day, the few messages that need the
director got buried under ones nobody acts on.

Nothing is lost by cutting them — the same facts arrive already sorted:

* every receipt is in the 23:59 daily summary and the 23:00 digest;
* every price increase, small ones included, is in the 21:30 bill
  analysis, next to who sells it cheaper;
* the shop's own manager still gets the receipt confirmation and the
  Tamil spike question in real time.

``DIRECTOR_FEED`` picks the mode:

* ``focus`` (default) — live messages only for things worth acting on now:
  price jumps of ``DIRECTOR_SPIKE_MIN_PCT`` (default 20%) or more, receipts
  the bot could not classify, and managers' answers to audit questions.
* ``full`` — the old behaviour: every receipt and every 10% spike.

Pure functions, no I/O — the env is read on each call so the owner can flip
the mode on Render without a code change.
"""
from __future__ import annotations

import os

FOCUS = "focus"
FULL = "full"

DEFAULT_SPIKE_MIN_PCT = 20.0


def mode() -> str:
    """Effective feed mode; anything unrecognised falls back to ``focus``."""
    raw = (os.environ.get("DIRECTOR_FEED") or "").strip().lower()
    return FULL if raw == FULL else FOCUS


def spike_min_pct() -> float:
    """Smallest price jump (in %) that is pushed live to the director."""
    raw = (os.environ.get("DIRECTOR_SPIKE_MIN_PCT") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SPIKE_MIN_PCT
    return value if value >= 0 else DEFAULT_SPIKE_MIN_PCT


def wants_receipt_feed() -> bool:
    """True when every logged receipt should be echoed to the director."""
    return mode() == FULL


def wants_spike(spike) -> bool:
    """True when this spike should be pushed live to the director.

    Malformed spikes are let through — a message the director doesn't need
    is cheaper than a real jump dropped on a parsing quirk."""
    if mode() == FULL:
        return True
    try:
        pct = float(spike.get("percent_increase"))
    except (AttributeError, TypeError, ValueError):
        return True
    return pct >= spike_min_pct()
