"""A burst of photos must not lose bills (Jakel, 2 Oct: 13 photos, 6 lost).

The bot used python-telegram-bot's default request (one pooled connection,
1s pool timeout); the "reading..." reply timed out and killed the handler
before the photo was downloaded. These tests pin the three fixes: a real
pool, retries on transient errors, and best-effort status messages with a
re-queue for photos whose download keeps failing.
"""
import asyncio
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut  # noqa: E402

import telegram_io  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Flaky:
    """Fails with ``errors`` in order, then returns ``result``."""

    def __init__(self, errors, result="ok"):
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


class WithRetryTests(unittest.TestCase):
    def setUp(self):
        self.slept = []

    async def _sleep(self, seconds):
        self.slept.append(seconds)

    def test_transient_errors_are_retried_with_backoff(self):
        call = Flaky([TimedOut("pool"), NetworkError("reset")])
        result = run(telegram_io.with_retry(call, what="t", base_delay=1, sleep=self._sleep))
        self.assertEqual(result, "ok")
        self.assertEqual(call.calls, 3)
        self.assertEqual(self.slept, [1, 2])

    def test_flood_control_waits_as_long_as_telegram_asks(self):
        call = Flaky([RetryAfter(7)])
        run(telegram_io.with_retry(call, what="t", sleep=self._sleep))
        self.assertEqual(self.slept, [7.0])

    def test_bad_request_is_not_retried(self):
        call = Flaky([BadRequest("message to reply not found")])
        with self.assertRaises(BadRequest):
            run(telegram_io.with_retry(call, what="t", sleep=self._sleep))
        self.assertEqual(call.calls, 1)

    def test_gives_up_after_the_last_attempt(self):
        call = Flaky([TimedOut("pool")] * 10)
        with self.assertRaises(TimedOut):
            run(telegram_io.with_retry(call, what="t", attempts=3, sleep=self._sleep))
        self.assertEqual(call.calls, 3)

    def test_best_effort_never_raises(self):
        call = Flaky([TimedOut("pool")] * 10)
        self.assertIsNone(run(telegram_io.best_effort(call, what="t", attempts=2,
                                                      sleep=self._sleep)))

    def test_retry_after_accepts_timedelta(self):
        exc = RetryAfter(3)
        with mock.patch.object(exc, "retry_after", timedelta(seconds=4), create=True):
            self.assertEqual(telegram_io._retry_after_seconds(exc), 4.0)


class RequestTests(unittest.TestCase):
    def test_bot_requests_get_a_real_pool(self):
        request = telegram_io.build_request()
        pool = request._client._transport._pool
        self.assertGreaterEqual(pool._max_connections, 4)
        self.assertGreaterEqual(request._client.timeout.pool, 10)

    def test_updates_request_is_separate(self):
        self.assertIsNot(telegram_io.build_request(), telegram_io.build_updates_request())


# ---------------------------------------------------------------- bot flow --

_DUMMY_ENV = {"TELEGRAM_BOT_TOKEN": "123:test", "ZAI_API_KEY": "test",
              "SUPABASE_URL": "http://localhost", "SUPABASE_KEY": "test",
              "ALERT_CHAT_ID": "1"}
# Dummy values only while bot.py imports (it reads them at import time);
# the environment is restored so no other test sees them.
try:
    with mock.patch.dict(os.environ, {k: os.environ.get(k, v) for k, v in _DUMMY_ENV.items()}):
        import bot  # noqa: E402
except Exception:  # pragma: no cover - depends on installed packages
    bot = None


class FakeFile:
    def __init__(self, owner):
        self.owner = owner

    async def download_as_bytearray(self):
        return bytearray(b"jpeg")


class FakeBot:
    def __init__(self, download_errors=None):
        self.download_errors = list(download_errors or [])
        self.downloads = 0

    async def get_file(self, file_id):
        self.downloads += 1
        if self.download_errors:
            raise self.download_errors.pop(0)
        return FakeFile(self)

    async def set_message_reaction(self, **kwargs):
        raise TimedOut("Pool timeout: All connections in the connection pool are occupied.")


class FakeMessage:
    """A photo in a non-outlet chat whose replies always hit the pool timeout
    — exactly what killed six Jakel handlers."""

    def __init__(self, message_id, reply_errors=True):
        self.chat_id = -999
        self.message_id = message_id
        self.chat = mock.Mock(title="Test chat")
        self.photo = [mock.Mock(file_id=f"file-{message_id}")]
        self.replies = []
        self.reply_errors = reply_errors

    async def reply_text(self, text, *args, **kwargs):
        self.replies.append(text)
        if self.reply_errors:
            raise TimedOut("Pool timeout: All connections in the connection pool are occupied.")


def _update(message):
    return mock.Mock(effective_message=message, effective_user=None)


@unittest.skipIf(bot is None, "bot.py needs its runtime packages")
class HandlePhotoBurstTests(unittest.TestCase):
    def setUp(self):
        self.ocr_calls = 0
        bot._photo_requeues.clear()

        async def fake_ocr(image_bytes):
            self.ocr_calls += 1
            raise RuntimeError("stop after OCR")  # nothing is saved in these tests

        patches = [
            mock.patch.object(bot, "extract_with_glm_chat", fake_ocr),
            mock.patch.object(bot, "ZAI_OCR_PROVIDER", "glm-4.6v-flash"),
            mock.patch.object(bot, "IMAGE_RESIZE_ENABLED", False),
            mock.patch.object(bot, "upload_receipt_image", lambda b: None),
            mock.patch.object(bot.cashier_names, "outlet_for_chat", lambda chat_id: None),
            mock.patch.object(bot.cashier_names, "is_outlet_group", lambda chat_id: False),
            mock.patch.object(telegram_io, "BASE_DELAY", 0),
            mock.patch.object(telegram_io, "REQUEUE_DELAYS", (0, 0)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_burst_with_failing_status_replies_still_reads_every_photo(self):
        fake_bot = FakeBot()
        context = mock.Mock(bot=fake_bot)
        messages = [FakeMessage(54227 + i) for i in range(13)]

        async def burst():
            await asyncio.gather(*(bot.handle_photo(_update(m), context) for m in messages))

        run(burst())
        self.assertEqual(fake_bot.downloads, 13)
        self.assertEqual(self.ocr_calls, 13)

    def test_a_transient_download_failure_is_retried(self):
        fake_bot = FakeBot([TimedOut("pool"), NetworkError("reset")])
        context = mock.Mock(bot=fake_bot)
        run(bot.handle_photo(_update(FakeMessage(1)), context))
        self.assertEqual(fake_bot.downloads, 3)
        self.assertEqual(self.ocr_calls, 1)
        self.assertEqual(bot._photo_requeues, {})

    def test_a_photo_that_keeps_failing_is_requeued_then_resend_is_asked(self):
        fake_bot = FakeBot([TimedOut("pool")] * 100)
        context = mock.Mock(bot=fake_bot)
        message = FakeMessage(2, reply_errors=False)

        async def go():
            await bot.handle_photo(_update(message), context)
            while bot._photo_requeue_tasks:
                await asyncio.gather(*list(bot._photo_requeue_tasks))

        run(go())
        # 1 first pass + 2 re-queues, 4 attempts each.
        self.assertEqual(fake_bot.downloads, 12)
        self.assertEqual(self.ocr_calls, 0)
        self.assertIn(bot._receipt_text(-999, "resend"), message.replies)
        # The "reading" note is sent once, not on every re-queue.
        self.assertEqual(message.replies.count(bot._receipt_text(-999, "reading")), 1)
        self.assertEqual(bot._photo_requeues, {})

    def test_requeued_photo_that_recovers_is_read(self):
        fake_bot = FakeBot([TimedOut("pool")] * 4)  # the whole first pass fails
        context = mock.Mock(bot=fake_bot)

        async def go():
            await bot.handle_photo(_update(FakeMessage(3, reply_errors=False)), context)
            while bot._photo_requeue_tasks:
                await asyncio.gather(*list(bot._photo_requeue_tasks))

        run(go())
        self.assertEqual(self.ocr_calls, 1)
        self.assertEqual(bot._photo_requeues, {})


if __name__ == "__main__":
    unittest.main()
