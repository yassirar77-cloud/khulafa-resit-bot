"""The bot client prefixes outlet-group messages with the cashier on shift.

Needs python-telegram-bot (skipped where it isn't installed)."""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from telegram.ext import ExtBot
    import outlet_group_bot
except Exception:  # pragma: no cover - depends on the environment
    ExtBot = None

import cashier_names as cn
from tests.fake_supabase import FakeSupabase

GROUP = -5043287182


@unittest.skipIf(ExtBot is None, "python-telegram-bot not installed")
class OutletGroupBotTests(unittest.TestCase):
    def setUp(self):
        sb = FakeSupabase()
        sb.table(cn.MANAGERS_TABLE).insert(
            [{"outlet_code": "SEK20", "manager_name": None, "chat_id": GROUP}]
        ).execute()
        cn.refresh(sb)
        self.bot = outlet_group_bot.OutletGroupBot(token="123:abc")

    def tearDown(self):
        cn.reset_cache()

    def _send(self, *args, **kwargs):
        with mock.patch.object(
            ExtBot, "send_message", new=mock.AsyncMock(return_value="sent")
        ) as parent, mock.patch.object(cn, "shift_at", return_value=(cn.MORNING, None)):
            result = asyncio.run(self.bot.send_message(*args, **kwargs))
        self.assertEqual(result, "sent")
        return parent.await_args

    def _send_with(self, side_effect):
        from telegram.error import RetryAfter  # noqa: F401
        with mock.patch.object(
            ExtBot, "send_message", new=mock.AsyncMock(side_effect=side_effect)
        ) as parent, mock.patch.object(cn, "shift_at", return_value=(cn.MORNING, None)), \
                mock.patch.object(outlet_group_bot.asyncio, "sleep", new=mock.AsyncMock()) as nap:
            try:
                result = asyncio.run(self.bot.send_message(chat_id=GROUP, text="Stok?"))
            except Exception as exc:
                result = exc
        return result, parent.await_count, nap.await_count

    def test_flood_control_waits_and_sends_again(self):
        from telegram.error import RetryAfter
        result, sends, naps = self._send_with([RetryAfter(13), "sent"])
        self.assertEqual((result, sends, naps), ("sent", 2, 1))

    def test_flood_control_gives_up_after_retries(self):
        from telegram.error import RetryAfter
        result, sends, _ = self._send_with([RetryAfter(1)] * 5)
        self.assertIsInstance(result, RetryAfter)
        self.assertEqual(sends, outlet_group_bot.FLOOD_RETRIES + 1)

    def test_very_long_flood_wait_is_not_slept_through(self):
        from telegram.error import RetryAfter
        result, sends, naps = self._send_with([RetryAfter(900), "sent"])
        self.assertIsInstance(result, RetryAfter)
        self.assertEqual((sends, naps), (1, 0))

    def test_group_message_gets_cashier_prefix(self):
        call = self._send(chat_id=GROUP, text="Stok?")
        self.assertEqual(call.args[1], "Cashier,\nStok?")
        self.assertEqual(call.args[0], GROUP)

    def test_other_chats_pass_through_with_all_arguments(self):
        call = self._send(99, "hi", "HTML", reply_markup="kb")
        self.assertEqual(call.args[:3], (99, "hi", "HTML"))
        self.assertEqual(call.kwargs, {"reply_markup": "kb"})


if __name__ == "__main__":
    unittest.main()
