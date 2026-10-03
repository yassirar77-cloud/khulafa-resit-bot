"""Telegram I/O that survives a burst of photos.

Before this, the bot ran on python-telegram-bot's default ``HTTPXRequest()``:
ONE pooled connection with a 1-second pool timeout, shared by every reply,
reaction and file download. When a cashier sent 13 photos at once
(Jakel, 2 Oct 19:22 UTC) the 👀 reactions and "reading..." replies queued
for that one connection, timed out after a second, and the exception killed
six handlers before the photo was even downloaded — six bills never saved.

Three layers now:
- ``build_request``: a real connection pool and a patient pool timeout;
- ``with_retry``: transient Telegram failures (pool/read timeouts, network
  blips, flood control) are retried with backoff;
- ``best_effort``: status replies and reactions can fail without taking the
  bill down with them.
``bot.handle_photo`` adds the last layer: a photo whose download still fails
is re-queued a few times, and only then is the cashier asked to resend.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any, Awaitable, Callable

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)

POOL_SIZE = max(4, int(os.environ.get("TELEGRAM_POOL_SIZE", "32")))
POOL_TIMEOUT = float(os.environ.get("TELEGRAM_POOL_TIMEOUT", "30"))

# First retry pause; it doubles each attempt (1s, 2s, 4s).
BASE_DELAY = float(os.environ.get("TELEGRAM_RETRY_BASE_DELAY", "1"))

# Seconds between whole-photo re-queues after with_retry gives up.
REQUEUE_DELAYS = (30, 90, 300)


def build_request(*, pool_size: int | None = None) -> HTTPXRequest:
    """The request object for bot API calls (replies, reactions, downloads)."""
    return HTTPXRequest(
        connection_pool_size=pool_size or POOL_SIZE,
        pool_timeout=POOL_TIMEOUT,
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=30.0,
        media_write_timeout=60.0,
    )


def build_updates_request() -> HTTPXRequest:
    """getUpdates long-polls on its own connection so it never competes
    with the replies of a burst."""
    return HTTPXRequest(connection_pool_size=1, pool_timeout=POOL_TIMEOUT,
                        read_timeout=30.0, connect_timeout=10.0)


def is_transient(exc: BaseException) -> bool:
    """A failure worth retrying: timeouts, dropped connections, flood
    control. BadRequest/Forbidden subclass NetworkError in PTB but retrying
    them can never help."""
    if isinstance(exc, (BadRequest, Forbidden)):
        return False
    return isinstance(exc, (TimedOut, NetworkError, RetryAfter))


def _retry_after_seconds(exc: RetryAfter) -> float:
    value = getattr(exc, "retry_after", 1)
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


async def with_retry(call: Callable[[], Awaitable[Any]], *, what: str,
                     attempts: int = 4, base_delay: float | None = None,
                     sleep: Callable[[float], Awaitable[Any]] | None = None) -> Any:
    """Await ``call()``; on a transient Telegram error wait 1s, 2s, 4s...
    (or what flood control asks) and try again. The last error is raised."""
    base_delay = BASE_DELAY if base_delay is None else base_delay
    sleep = sleep or asyncio.sleep
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:
            if not is_transient(exc) or attempt == attempts:
                raise
            delay = (_retry_after_seconds(exc) if isinstance(exc, RetryAfter)
                     else base_delay * (2 ** (attempt - 1)))
            logger.warning("telegram: %s failed (%s), retry %d/%d in %.0fs",
                           what, type(exc).__name__, attempt, attempts - 1, delay)
            await sleep(delay)


async def best_effort(call: Callable[[], Awaitable[Any]], *, what: str, **kwargs) -> Any:
    """``with_retry`` that never raises: a status message must not lose a bill."""
    try:
        return await with_retry(call, what=what, **kwargs)
    except Exception:
        logger.warning("telegram: %s gave up", what, exc_info=True)
        return None
