"""The bot's Telegram client, with the cashier's name on outlet-group messages.

Every message the bot sends goes through ``send_message`` — scheduled jobs
call it directly and ``Message.reply_text`` calls it under the hood — so
this one override is where "every message to an outlet group starts with
the cashier on shift" is enforced. Director chat and DMs pass through
untouched (see ``cashier_names.with_address``).
"""
from __future__ import annotations

import asyncio

from telegram.ext import ExtBot

import cashier_names


class OutletGroupBot(ExtBot):
    async def send_message(self, chat_id, text, *args, **kwargs):
        if cashier_names.is_stale():
            await asyncio.to_thread(cashier_names.refresh)
        parse_mode = kwargs.get("parse_mode", args[0] if args else None)
        text = cashier_names.with_address(chat_id, text, parse_mode=parse_mode)
        return await super().send_message(chat_id, text, *args, **kwargs)
