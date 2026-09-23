"""Source-level checks that bot.py wires the plain-language search correctly.

Like ``test_bill_analysis_wiring.py``, these read bot.py as text — importing
it needs telegram/apscheduler/supabase and env vars that aren't present in
CI. The executable behaviour lives in ``test_director_ask.py``; pinned here
is only what can't be unit-tested without Telegram: the handler
registration ORDER (a greedy text handler would silently eat the review
conversation and the audit replies) and the access gate.
"""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _block(src: str, start_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index("\nasync def ", start + 1)
    return src[start:end]


class DirectorAskWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_module_imported_and_commands_registered(self):
        self.assertIn("import director_ask\n", self.src)
        for name in ("ask", "tanya", "cari", "search"):
            self.assertIn(f'CommandHandler("{name}", ask_command)', self.src)

    def test_documented_in_help_and_the_command_menu(self):
        help_text = self.src[self.src.index("HELP_TEXT = ("):]
        self.assertIn("/ask <question>", help_text)
        self.assertIn("beras beli kat mana", help_text)
        self.assertIn('BotCommand("ask"', self.src)

    def test_command_is_gated_like_shop_prices(self):
        block = _block(self.src, "def _ask_allowed(")
        self.assertIn("message.chat_id == ALERT_CHAT_ID", block)
        self.assertIn("is_reviewer(_command_owner_id(update))", block)
        self.assertIn("_ask_allowed(update)", _block(self.src, "async def ask_command("))

    def test_free_text_handler_registered_after_the_audit_reply_handler(self):
        # PTB stops at the first matching handler in a group, so a
        # ~COMMAND text handler registered too early would swallow the
        # review-edit conversation's input and every audit reply.
        conversation = self.src.index("app.add_handler(build_review_edit_conversation())")
        audit = self.src.index("handle_audit_reply)")
        ask = self.src.index("handle_ask_text)")
        self.assertLess(conversation, ask)
        self.assertLess(audit, ask)
        registration = self.src[self.src.index("~filters.REPLY, handle_ask_text"):]
        self.assertTrue(registration.startswith("~filters.REPLY, handle_ask_text"))

    def test_free_text_handler_ignores_replies_and_commands(self):
        idx = self.src.index("handle_ask_text)")
        registration = self.src[idx - 200:idx + 20]
        self.assertIn("filters.TEXT", registration)
        self.assertIn("~filters.COMMAND", registration)
        self.assertIn("~filters.REPLY", registration)

    def test_free_text_handler_is_gated_and_quiet(self):
        block = _block(self.src, "async def handle_ask_text(")
        # Un-prompted answers only in the alert group or a reviewer's DM —
        # never dumped into an outlet group because the owner is in it.
        self.assertIn("private_reviewer = ", block)
        self.assertIn("not private_reviewer and message.chat_id != ALERT_CHAT_ID", block)
        # Group chatter needs the high bar; a reviewer's DM needs only an item.
        self.assertIn('parsed.get("confident")', block)
        self.assertIn('parsed.get("canonical")', block)
        # Nothing that misses the bar gets a reply.
        self.assertIn("if not interesting:\n        return", block)

    def test_shop_prices_defaults_to_the_item_level_report(self):
        # Grouping by item_variant fragmented one item into dozens of
        # one-shop blocks and hid the real suppliers behind "+N more
        # type(s)". The default view lists every shop; the per-cut
        # comparison is still reachable with "cuts".
        block = _block(self.src, "async def shop_prices_command(")
        self.assertIn("director_ask.build_item_report", block)
        self.assertIn('("cuts", "cut", "variants")', block)
        self.assertIn("shop_price_comparison.build_shop_price_report", block)
        # The full listing is longer than one Telegram message.
        self.assertIn("_reply_chunked(message, text)", block)

    def test_answers_go_off_thread_and_chunked(self):
        block = _block(self.src, "async def _send_answer(")
        self.assertIn("asyncio.to_thread(", block)
        self.assertIn("director_ask.answer_question", block)
        self.assertIn("_reply_chunked(message, text)", block)


if __name__ == "__main__":
    unittest.main()
