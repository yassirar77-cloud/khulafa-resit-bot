"""Source-level checks that bot.py wires the monthly close.

Like ``test_bot_review_flow``, these read bot.py as text — it has runtime deps
(apscheduler, telegram, supabase) and required env vars absent in CI. The
executable behaviour lives in the monthly_* test modules.
"""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BotMonthlyWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_commands_are_registered(self):
        for line in (
            'app.add_handler(CommandHandler("monthly_close", monthly_close_command))',
            'app.add_handler(CommandHandler("monthly_ingest_now", monthly_ingest_now_command))',
        ):
            self.assertIn(line, self.src, line)

    def test_commands_are_reviewer_gated(self):
        for name in ("monthly_close_command", "monthly_ingest_now_command"):
            index = self.src.index(f"async def {name}(")
            self.assertIn("is_reviewer(_command_owner_id(update))",
                          self.src[index:index + 900], name)

    def test_close_validates_its_month_argument(self):
        index = self.src.index("async def monthly_close_command(")
        block = self.src[index:index + 1600]
        self.assertIn("Usage: /monthly_close <outlet> <YYYY-MM>", block)
        self.assertIn("parse_month(period) is None", block)

    def test_close_sends_both_the_digest_and_the_workbook(self):
        index = self.src.index("async def monthly_close_command(")
        block = self.src[index: self.src.index("async def monthly_ingest_now_command(")]
        self.assertIn("format_monthly_digest", block)
        self.assertIn("send_document(", block)

    def test_missing_month_is_a_distinct_message_not_a_crash(self):
        index = self.src.index("def _run_monthly_close(")
        block = self.src[index: index + 2200]
        self.assertIn("raise LookupError", block)
        self.assertIn("no monthly report ingested", block)
        close = self.src[self.src.index("async def monthly_close_command("):]
        self.assertIn("except LookupError as exc:", close)

    def test_period_specific_config_wins_over_the_standing_row(self):
        index = self.src.index("def _run_monthly_close(")
        block = self.src[index: index + 2600]
        self.assertIn('c.get("period") == period', block)

    def test_commands_are_documented(self):
        self.assertIn("/monthly_close <outlet> <YYYY-MM>", self.src)
        self.assertIn("/monthly_ingest_now", self.src)
        self.assertIn('BotCommand("monthly_close"', self.src)

    def test_supplier_master_is_shared_not_duplicated(self):
        # The monthly reclassification and the receipt classifier must agree on
        # what a supplier is; two copies of the list would let them diverge.
        self.assertIn("from supplier_master import KNOWN_SUPPLIERS, is_known_supplier",
                      self.src)
        self.assertNotIn("KNOWN_SUPPLIERS = [", self.src)


class MigrationShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "migrations", "0039_monthly_close.sql")) as f:
            cls.sql = f.read()

    def test_all_six_tables_plus_outlet_config(self):
        for table in ("monthly_report", "monthly_daily_sales", "monthly_payouts",
                      "monthly_itemwise", "monthly_staff_sales",
                      "monthly_staff_advance", "outlet_config"):
            self.assertIn(f"public.{table}", self.sql, table)

    def test_everything_is_keyed_by_outlet_and_period(self):
        for table in ("monthly_daily_sales", "monthly_payouts", "monthly_itemwise",
                      "monthly_staff_sales", "monthly_staff_advance"):
            start = self.sql.index(f"CREATE TABLE IF NOT EXISTS public.{table}")
            block = self.sql[start: self.sql.index(");", start)]
            self.assertIn("outlet             text NOT NULL", block, table)
            self.assertIn("period             text NOT NULL", block, table)

    def test_payout_block_is_constrained_to_the_printed_blocks(self):
        for block in ("'purchase'", "'pinjam'", "'salary_block'", "'cheque'",
                      "'staff_meals'"):
            self.assertIn(block, self.sql, block)

    def test_the_summary_control_totals_are_stored(self):
        # total_salary is what the supplier block is reconciled against; without
        # it the silent-zero guard has nothing to check.
        for column in ("total_salary", "payout_purchase", "payout_expense"):
            self.assertIn(column, self.sql, column)

    def test_outlet_config_carries_every_bank_paid_line(self):
        for column in ("rent", "tnb", "air_selangor", "unifi", "central_payroll",
                       "kwsp_perkeso"):
            self.assertIn(column, self.sql, column)

    def test_rls_enabled_on_every_table(self):
        for table in ("monthly_report", "monthly_daily_sales", "monthly_payouts",
                      "monthly_itemwise", "monthly_staff_sales",
                      "monthly_staff_advance", "outlet_config"):
            self.assertIn(f"ALTER TABLE public.{table}", self.sql, table)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FOR ALL TO service_role", self.sql)

    def test_itemwise_documents_why_names_are_verbatim(self):
        self.assertIn("VERBATIM", self.sql)


if __name__ == "__main__":
    unittest.main()
