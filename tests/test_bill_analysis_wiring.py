"""Source-level checks that bot.py wires the nightly bill analysis correctly.

Like ``test_order_drafts_wiring.py``, these read bot.py as text — importing it
needs telegram/apscheduler/supabase and env vars that aren't present in CI.
The executable behaviour is covered in ``test_bill_analysis.py``; here we pin
only the wiring that can't be unit-tested without Telegram.
"""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _block(src: str, start_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index("\nasync def ", start + 1)
    return src[start:end]


class BillAnalysisWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_module_imported_and_scheduled_nightly(self):
        self.assertIn("import bill_analysis\n", self.src)
        idx = self.src.index('id="bill_analysis"')
        sched = self.src[idx - 400:idx]
        self.assertIn("post_bill_analysis,", sched)
        self.assertIn("hour=21,", sched)
        self.assertIn("minute=30,", sched)

    def test_commands_registered_and_documented(self):
        self.assertIn('CommandHandler("bill_analysis_now", bill_analysis_now_command)', self.src)
        self.assertIn('CommandHandler("outlet_prices", outlet_prices_command)', self.src)
        self.assertIn('CommandHandler("branch_prices", outlet_prices_command)', self.src)
        self.assertIn("/bill_analysis_now", self.src[self.src.index("HELP_TEXT = ("):])
        self.assertIn("/outlet_prices [item]", self.src[self.src.index("HELP_TEXT = ("):])

    def test_on_demand_commands_are_gated(self):
        now_block = _block(self.src, "async def bill_analysis_now_command(")
        self.assertIn("is_reviewer(_command_owner_id(update))", now_block)
        prices_block = _block(self.src, "async def outlet_prices_command(")
        self.assertIn("message.chat_id == ALERT_CHAT_ID", prices_block)
        self.assertIn("is_reviewer(_command_owner_id(update))", prices_block)
        # Long every-item tables are chunked, never a single oversized send.
        self.assertIn("_reply_chunked(message, text)", prices_block)

    def test_owners_always_get_both_reports_chunked(self):
        block = _block(self.src, "async def post_bill_analysis(")
        self.assertIn("bill_analysis.format_owner_price_report(bundle)", block)
        self.assertIn("bill_analysis.format_owner_outlet_report(bundle)", block)
        self.assertIn("_send_chunked_to(application, ALERT_CHAT_ID, price_report)", block)
        self.assertIn("_send_chunked_to(application, ALERT_CHAT_ID, outlet_report)", block)

    def test_manager_notes_ride_the_delivery_gate(self):
        block = _block(self.src, "async def post_bill_analysis(")
        self.assertIn("wmr.route_message(", block)
        self.assertIn("bill_analysis.entries_for_outlet(bundle, code)", block)
        self.assertIn("bill_analysis.format_manager_note(code, slice_)", block)
        self.assertIn("supervisor.with_reply_footer(text)", block)
        self.assertIn("human_touch.personalise(", block)
        # The question is only logged when it reached the real manager.
        self.assertIn('if decision.reason == "manager" and first_id is not None:', block)
        self.assertIn('"bill_analysis", text,', block)
        # The owner sees who got what, and the live/test banner.
        self.assertIn("bill_analysis.format_owner_delivery_summary(delivered, enabled)", block)

    def test_gather_failure_alerts_owner(self):
        block = _block(self.src, "async def post_bill_analysis(")
        self.assertIn("{ALERT_CHAT_ID, notify_chat_id} - {None}", block)
        self.assertIn("Bill analysis failed to run", block)

    def test_outlet_code_bridges_to_registry_code(self):
        # item_prices codes ("D", "BISTRO7") must find the outlet_canonical
        # registration code so the right manager is looked up.
        self.assertIn("def _registry_code_for_outlet(outlet_code, registry_outlets)", self.src)
        block = self.src[self.src.index("def _registry_code_for_outlet("):]
        block = block[:block.index("\ndef _gather_bill_analysis(")]
        self.assertIn("canonical_outlet(code)", block)
        self.assertIn("o.canonical == canonical", block)
        gather = self.src[self.src.index("def _gather_bill_analysis("):]
        gather = gather[:gather.index("\nasync def ")]
        self.assertIn("_registry_code_for_outlet(code, outlets)", gather)
        self.assertIn("wmr.delivery_enabled()", gather)


if __name__ == "__main__":
    unittest.main()
