"""Source-level checks that bot.py wires outlet-group delivery.

bot.py can't be imported in tests (Telegram/Supabase clients and env vars),
so, like the other *_wiring tests, these read it as text."""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _block(src, marker):
    start = src.index(marker)
    end = src.find("\nasync def ", start + 1)
    return src[start:end if end != -1 else len(src)]


class OutletGroupsWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_bot_client_prefixes_names_and_cache_is_loaded_first(self):
        run = _block(self.src, "async def run_bot(")
        self.assertIn(".bot(OutletGroupBot(token=TELEGRAM_BOT_TOKEN))", run)
        self.assertNotIn(".token(TELEGRAM_BOT_TOKEN)", run)
        self.assertLess(run.index("cashier_names.configure(supabase)"),
                        run.index("Application.builder()"))

    def test_money_reports_gated_out_of_groups(self):
        for marker, key in (
            ("async def post_weekly_manager_reports(", "FOOD_COST"),
            ("async def post_bill_analysis(", "BILL_ANALYSIS"),
            ("async def post_weekly_praise(", "PRAISE"),
            ("async def post_overbuy_checks(", "OVERBUY"),
        ):
            body = _block(self.src, marker)
            self.assertIn(f"group_reports.blocked(", body, marker)
            self.assertIn(f"group_reports.{key}", body, marker)

    def test_task_reports_are_not_gated(self):
        for marker in ("async def post_key_stock_checks(",
                       "async def post_slow_item_checks(",
                       "async def post_cook_plans(",
                       "async def post_missing_bill_checks(",
                       "async def post_order_drafts("):
            self.assertNotIn("group_reports.blocked(", _block(self.src, marker))

    def test_spike_question_goes_back_to_the_upload_group(self):
        self.assertIn("cashier_names.outlet_for_chat(message.chat_id)", self.src)

    def test_director_commands_registered_and_gated(self):
        for name, fn in (("cashier", "cashier_command"),
                         ("ping_managers", "ping_managers_command")):
            self.assertIn(f'CommandHandler("{name}", {fn})', self.src)
            body = _block(self.src, f"async def {fn}(")
            self.assertIn("is_reviewer(_command_owner_id(update))", body)


if __name__ == "__main__":
    unittest.main()
