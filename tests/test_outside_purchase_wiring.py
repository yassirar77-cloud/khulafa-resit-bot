"""Source-level checks that bot.py wires the outside-purchase strike system.

bot.py can't be imported in tests (Telegram/Supabase clients and env vars),
so, like the other *_wiring tests, these read it as text. The executable
behaviour lives in ``test_outside_purchase.py`` against the pure module."""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _block(src, marker):
    start = src.index(marker)
    end = src.find("\nasync def ", start + 1)
    return src[start:end if end != -1 else len(src)]


class OutsidePurchaseWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()
        with open(os.path.join(REPO_ROOT, "migrations", "0058_outside_purchases.sql")) as f:
            cls.sql = f.read()

    def test_hook_runs_after_the_receipt_is_saved_for_purchases_only(self):
        self.assertIn("import outside_purchase\n", self.src)
        photo = _block(self.src, "async def handle_photo(")
        self.assertIn("if receipt_type in _OUTSIDE_RECEIPT_TYPES:", photo)
        self.assertIn("await outside_purchase_on_upload(context, stored, message)", photo)
        self.assertIn("_OUTSIDE_RECEIPT_TYPES = (ReceiptType.SUPPLIER_PURCHASE, ReceiptType.UNKNOWN)", self.src)
        # After the save, before the non-purchase side tables return early.
        save = photo.index("stored = await asyncio.to_thread(store_receipt, record)")
        hook = photo.index("await outside_purchase_on_upload(")
        advance = photo.index("if receipt_type == ReceiptType.STAFF_ADVANCE:")
        self.assertLess(save, hook)
        self.assertLess(hook, advance)

    def test_no_new_ocr_and_failures_never_break_the_pipeline(self):
        hook = _block(self.src, "async def outside_purchase_on_upload(")
        self.assertIn("outside_purchase.process_receipt, supabase, stored, group_code=group_code", hook)
        self.assertIn("except Exception:", hook)
        self.assertNotIn("extract_with_glm", hook)
        self.assertNotIn("zai_client", hook)
        self.assertIn("cashier_names.outlet_for_chat(message.chat_id)", hook)

    def test_grey_zone_goes_to_the_director_with_buttons(self):
        hook = _block(self.src, "async def outside_purchase_on_upload(")
        self.assertIn('if result["action"] == "pending":', hook)
        self.assertIn("chat_id=ALERT_CHAT_ID", hook)
        self.assertIn("reply_markup=_outside_review_keyboard(row.get(\"id\"))", hook)
        self.assertIn('InlineKeyboardButton("Beli Luar ✅", callback_data=f"ob:{purchase_id}:yes")', self.src)
        self.assertIn('InlineKeyboardButton("Supplier Kita ❌", callback_data=f"ob:{purchase_id}:no")', self.src)
        self.assertIn(r'CallbackQueryHandler(handle_outside_review, pattern=r"^ob:\d+:(yes|no)$")', self.src)
        review = _block(self.src, "async def handle_outside_review(")
        self.assertIn("outside_purchase.confirm_outside", review)
        self.assertIn("outside_purchase.mark_false_positive", review)
        self.assertIn("is_reviewer(reviewer)", review)

    def test_strike_reply_goes_under_the_receipt_and_scold_reports_to_management(self):
        send = _block(self.src, "async def _outside_send_strike(")
        self.assertIn("reply_to_message_id=reply_to_message_id", send)
        self.assertIn('outside_purchase.group_message(row, shown_no, shown_history, "bm_tamil")', send)
        # Live rows only: the tier the cashier hears starts counting at the switch.
        self.assertIn("outside_purchase.live_rows(history)", send)
        self.assertIn("if not outside_purchase.is_live():", send)
        self.assertIn("if strike_no and strike_no >= outside_purchase.scold_threshold():", send)
        self.assertIn("outside_purchase.management_report(row, strike_no, history)", send)
        self.assertIn("chat_id=ALERT_CHAT_ID", send)
        # SCOLD_CHANNEL=dm needs a linked account and falls back to the group.
        self.assertIn('outside_purchase.scold_channel() == "dm"', send)
        self.assertIn("if not sent_dm and chat_id is not None:", send)

    def test_commands_registered_gated_and_documented(self):
        for name, fn in (("beli_luar", "beli_luar_command"),
                         ("beli_luar_cashier", "beli_luar_cashier_command"),
                         ("izin", "izin_command"),
                         ("bukan_beli_luar", "bukan_beli_luar_command"),
                         ("tambah_supplier", "tambah_supplier_command"),
                         ("daftar_cashier", "daftar_cashier_command")):
            self.assertIn(f'CommandHandler("{name}", {fn})', self.src)
            self.assertIn(f"/{name}", self.src[self.src.index("HELP_TEXT = ("):])
        for fn in ("beli_luar_command", "beli_luar_cashier_command", "izin_command",
                   "bukan_beli_luar_command", "tambah_supplier_command"):
            self.assertIn("_outside_admin(update)", _block(self.src, f"async def {fn}("))
        admin = self.src[self.src.index("def _outside_admin("):]
        self.assertIn("message.chat_id == ALERT_CHAT_ID or is_reviewer(_command_owner_id(update))", admin)
        # /daftar_cashier is for the cashiers themselves — not admin-gated.
        self.assertNotIn("_outside_admin", _block(self.src, "async def daftar_cashier_command("))
        self.assertIn(r'CallbackQueryHandler(handle_daftar_callback, pattern=r"^dc:\d+:")', self.src)
        self.assertIn('if tapper != parsed["user_id"]:', _block(self.src, "async def handle_daftar_callback("))

    def test_monthly_close_gets_the_outside_section(self):
        self.assertIn("async def _with_outside_section(", self.src)
        self.assertIn("outside_purchase.monthly_section(rows, year, month)", self.src)
        self.assertIn("text = await _with_outside_section(text, year, month)",
                      _block(self.src, "async def monthly_kg_command("))
        self.assertIn("text = await _with_outside_section(text, year, month)",
                      _block(self.src, "async def post_monthly_kg_report("))

    def test_migration_has_rls_on_every_new_table_and_the_view(self):
        for table in ("approved_suppliers", "allowed_outside_items", "cashier_roster", "outside_purchases"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS public.{table}", self.sql)
            self.assertIn(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;", self.sql)
            self.assertIn(f"CREATE POLICY {table}_service ON public.{table}", self.sql)
        self.assertIn("CREATE OR REPLACE VIEW public.cashier_strikes", self.sql)
        self.assertIn("WHERE status = 'counted'", self.sql)
        for name in ("BABAS", "SAIDA", "JASMINE", "MEWAH", "HANEE", "CAMELLIAA", "JY RESOURCES", "JUTA RIA",
                     "BS FROZEN FOOD", "REZA PLASTIC", "BALAJI", "SAYUR", "BESTARI FARM (M) SDN BHD",
                     "BESTARI WHOLESALE SDN BHD", "M/S BESTARI KHIDMAT", "FOOK LEONG", "MYSOOR", "MD HANI"):
            self.assertIn(f"('{name}',", self.sql, name)
        for outlet, names in (("Bistro", ("Rahim", "Saddam")), ("Jakel", ("Latip", "Sumon")),
                              ("Signature", ("Jaffar", "Danang")), ("One Bistro", ("Sheik", "Kanagaraj")),
                              ("SEK-20", ("Syed", "Ismath")), ("SEK-6", ("Imdadul", "Mahadir", "Pandi")),
                              ("Vista", ("Buhari", "Samsudeen")), ("D.U", ("Yusof", "Yelumalai")),
                              ("Klang B.Emas", ("Vasiullah", "Bright")), ("SBESI", ("Hari", "Kalai"))):
            for name in names:
                self.assertRegex(self.sql, rf"\('{outlet}',\s+'(morning|night)',\s+'{name}'\)")

    def test_config_documented(self):
        with open(os.path.join(REPO_ROOT, ".env.example")) as f:
            env = f.read()
        for key in ("STRIKE_WINDOW_DAYS=30", "SCOLD_THRESHOLD=5", "SCOLD_CHANNEL=group", "OUTSIDE_MIN_CONFIDENCE=80"):
            self.assertIn(key, env)


if __name__ == "__main__":
    unittest.main()
