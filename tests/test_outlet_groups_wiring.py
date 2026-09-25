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


class StaffChatWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_commands_registered_and_director_only(self):
        for name, fn in (("lang", "lang_command"),
                         ("staff_preview", "staff_preview_command")):
            self.assertIn(f'CommandHandler("{name}", {fn})', self.src)
            self.assertIn("is_reviewer(_command_owner_id(update))",
                          _block(self.src, f"async def {fn}("))

    def test_scheduled_checkins_only_run_in_preview(self):
        self.assertIn("for _slot, (_shift, _time, _purpose) in staff_chat.SLOTS.items():", self.src)
        body = _block(self.src, "async def run_staff_preview(")
        self.assertIn("staff_chat.style() != staff_chat.PREVIEW", body)
        # Preview goes to the director chat, never a group.
        self.assertIn("application, ALERT_CHAT_ID, staff_chat.format_preview(slot, preview_rows)", body)
        self.assertNotIn("decision.target_chat_id", body)


class StaffLiveWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_reply_handler_runs_alongside_others(self):
        self.assertIn("handle_staff_reply),\n        group=1,", self.src)

    def test_jobs_registered(self):
        self.assertIn('id="staff_live_tick"', self.src)
        self.assertIn('id="staff_morning_summary"', self.src)

    def test_live_outlets_skip_classic_duplicates(self):
        self.assertIn("_staff_live_now(entry.get(\"outlet_code\"))",
                      _block(self.src, "async def post_cook_plans("))
        self.assertIn("_staff_live_now(cashier_names.outlet_for_chat(",
                      _block(self.src, "async def post_missing_bill_checks("))
        # The 20:00 order draft and 10:30 key-stock check would duplicate
        # the 20:05 order and 10:35 stock check-ins.
        self.assertIn("_staff_live_now(cashier_names.outlet_for_chat(msg[\"target\"]))",
                      _block(self.src, "async def post_order_drafts("))
        self.assertIn("_staff_live_now(cashier_names.outlet_for_chat(decision.target_chat_id))",
                      _block(self.src, "async def post_key_stock_checks("))

    def test_honest_answer_and_order_learning_wired(self):
        body = _block(self.src, "async def handle_staff_reply(")
        self.assertIn("staff_live.asks_if_bot(message.text)", body)
        self.assertIn('parsed.get("asks_if_bot")', body)
        self.assertIn("staff_orders.rows_for_reply(thread, parsed[\"items\"]", body)
        # The honesty check comes before the open-question lookup, so it
        # works when nothing was asked.
        self.assertLess(body.index("asks_if_bot(message.text)"),
                        body.index("_active_thread"))

    def test_buttons_wired(self):
        self.assertIn('CallbackQueryHandler(handle_staff_button, pattern=r"^sc:\\d+:\\w+$")',
                      self.src)
        send = _block(self.src, "async def _live_send_or_queue(")
        self.assertIn("reply_markup=_markup(thread_id", send)
        # Questions run side by side now (staff_ops): nothing is closed early.
        self.assertNotIn('{"status": staff_live.NO_REPLY}', send)
        tap = _block(self.src, "async def handle_staff_button(")
        self.assertIn("staff_live.tap_outcome(", tap)
        self.assertIn("_record_handin", tap)

    def test_paused_check_ins_do_not_run(self):
        self.assertIn("staff_live.slot_enabled(slot)",
                      _block(self.src, "async def run_staff_preview("))

    def test_handed_in_bills_not_re_asked(self):
        self.assertIn("_recent_handins(today)", _block(self.src, "def _build_staff_preview("))

    def test_sales_poll_has_its_own_client(self):
        self.assertIn("run_ingest_once, _sales_supabase()",
                      _block(self.src, "async def poll_sales_emails("))

    def test_samples_command_director_only(self):
        self.assertIn('CommandHandler("staff_samples", staff_samples_command)', self.src)
        self.assertIn("is_reviewer(_command_owner_id(update))",
                      _block(self.src, "async def staff_samples_command("))


class StaffOpsWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_jobs_and_command(self):
        for slot in ("leftover", "sales", "wastage", "afternoon", "praise"):
            self.assertIn(f'("{slot}", {{', self.src)
        self.assertIn('id=f"staff_ops_{_slot}"', self.src)
        self.assertIn('CommandHandler("draft", draft_command)', self.src)

    def test_upload_hooks(self):
        photo = _block(self.src, "async def handle_photo(")
        self.assertIn("supplier=False)", photo)      # mini market, any receipt type
        self.assertIn("supplier=True)", photo)       # invoice check, after item_prices
        self.assertLess(photo.index("supplier=False)"), photo.index("if receipt_type == ReceiptType.STAFF_ADVANCE:"))
        self.assertLess(photo.index("save_item_prices,"), photo.index("supplier=True)"))

    def test_daily_limit_and_reaction(self):
        send = _block(self.src, "async def _ops_send(")
        self.assertIn("staff_ops.may_send(", send)
        self.assertIn('"not_asked": True', send)
        upload = _block(self.src, "async def staff_ops_on_upload(")
        self.assertIn('reaction="👌"', upload)
        self.assertIn("_already_asked", upload)


class LeftoverSkipWiring(unittest.TestCase):
    def test_leftover_skips_groups_that_filled_the_0200_form(self):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            src = f.read()
        run = _block(src, "async def run_staff_ops(")
        self.assertIn("_left_form_filled, db, today - timedelta(days=1)", run)
        self.assertIn("if chat_id in filled:", run)
        helper = src[src.index("def _left_form_filled("):]
        helper = helper[:helper.index("\ndef ")]
        self.assertIn("kitchen_usage.PHASE_LEFT", helper)
        self.assertIn('"submitted"', helper)
