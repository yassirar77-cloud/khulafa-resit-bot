"""The bot's Telegram client, with the cashier's name on outlet-group messages.

Every message the bot sends goes through ``send_message`` — scheduled jobs
call it directly and ``Message.reply_text`` calls it under the hood — so
this one override is where "every message to an outlet group starts with
the cashier on shift" is enforced. Director chat and DMs pass through
untouched (see ``cashier_names.with_address``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from telegram.error import RetryAfter
from telegram.ext import ExtBot

import cashier_names

logger = logging.getLogger(__name__)

# Flood control ("Retry in 13 seconds") means Telegram did NOT send the
# message, so waiting and sending again is safe. A burst of receipts used to
# lose the review alert and the outside-purchase reply this way.
FLOOD_RETRIES = 2
FLOOD_MAX_WAIT = 60.0


def _flood_wait(exc: RetryAfter) -> float:
    value = getattr(exc, "retry_after", 1)
    if isinstance(value, timedelta):
        value = value.total_seconds()
    try:
        return max(float(value), 0.0) + 1.0
    except (TypeError, ValueError):
        return 2.0


class OutletGroupBot(ExtBot):
    async def send_message(self, chat_id, text, *args, **kwargs):
        if cashier_names.is_stale():
            await asyncio.to_thread(cashier_names.refresh)
        parse_mode = kwargs.get("parse_mode", args[0] if args else None)
        text = cashier_names.with_address(chat_id, text, parse_mode=parse_mode)
        for attempt in range(FLOOD_RETRIES + 1):
            try:
                return await super().send_message(chat_id, text, *args, **kwargs)
            except RetryAfter as exc:
                wait = _flood_wait(exc)
                if attempt == FLOOD_RETRIES or wait > FLOOD_MAX_WAIT:
                    raise
                logger.warning("telegram flood control: waiting %.0fs before sending again", wait)
                await asyncio.sleep(wait)
