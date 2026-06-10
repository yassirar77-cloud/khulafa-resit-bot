"""Source-level checks that bot.py wires the order-draft send path correctly.

Like ``test_bot_review_flow.py``, these read bot.py as text rather than importing
it — bot.py needs telegram/apscheduler/supabase and env vars that aren't present
in CI/dev. The executable behaviour (chunking, failure_alert) is covered against
the pure helpers in ``test_order_draft.py`` / ``test_order_generator.py``; here we
only assert the wiring that can't be unit-tested without Telegram.
"""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OrderDraftsWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_sends_per_chunk_not_single_message(self):
        # Each outlet is sent as a list of Telegram-safe chunks, prefix on #0.
        self.assertIn('for i, chunk in enumerate(o["messages"]):', self.src)
        self.assertIn('(decision.prefix + chunk) if i == 0 else chunk', self.src)
        # The old single unbounded send must be gone.
        self.assertNotIn('"text": decision.prefix + o["message"],', self.src)

    def test_outlet_display_falls_back_to_alias(self):
        # The header resolves the live registry name first, then the internal
        # code alias (so "D" -> "D.U"), never a bare code.
        self.assertIn("outlet_mapping.outlet_display_name(code)", self.src)
        self.assertIn("display_by_code.get(code)", self.src)

    def test_gather_failure_alerts_owner(self):
        # A build crash notifies ALERT_CHAT_ID (not only notify_chat_id) — the
        # silent-cron bug. failure_alert builds the text.
        post_idx = self.src.index("async def post_order_drafts(")
        next_def = self.src.index("\nasync def ", post_idx + 1)
        block = self.src[post_idx:next_def]
        self.assertIn("order_generator.failure_alert(gather_error=", block)
        self.assertIn("{ALERT_CHAT_ID, notify_chat_id} - {None}", block)

    def test_send_failures_counted_and_alerted(self):
        post_idx = self.src.index("async def post_order_drafts(")
        next_def = self.src.index("\nasync def ", post_idx + 1)
        block = self.src[post_idx:next_def]
        # Per-message failures are counted, not swallowed without a tally.
        self.assertIn("failed += 1", block)
        self.assertIn("hq_failed = True", block)
        # And a nonzero failure count is surfaced to the owner.
        self.assertIn("order_generator.failure_alert(", block)
        self.assertIn("total_messages=total, failed_messages=failed, hq_failed=hq_failed",
                      block)
        self.assertIn("chat_id=ALERT_CHAT_ID, text=alert", block)


if __name__ == "__main__":
    unittest.main()
