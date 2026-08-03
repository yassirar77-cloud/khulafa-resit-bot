"""Source-level checks that bot.py wires the invoice line-items capture.

Like ``test_bot_review_flow``, these read bot.py as text rather than importing
it — bot.py has runtime deps (apscheduler, telegram, supabase) and required env
vars that aren't present in CI/dev. The executable behaviour lives in
``test_receipt_items`` / ``test_uom_conversion`` / ``test_receipt_validation`` /
``test_wastage_export`` against the pure helpers.
"""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BotInvoiceLinesWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_ocr_concurrency_defaults_to_one(self):
        # 512MB Render box: the heavy region now runs two vision calls, so a
        # second concurrent receipt is the OOM case.
        self.assertIn(
            'OCR_MAX_CONCURRENCY = max(1, int(os.environ.get("OCR_MAX_CONCURRENCY", "1")))',
            self.src,
        )

    def test_line_pass_runs_inside_the_ocr_semaphore(self):
        acquire = "await _ocr_semaphore.acquire()"
        call = "invoice_lines = await extract_invoice_lines(image_bytes)"
        release = "_ocr_semaphore.release()"
        for needle in (acquire, call, release):
            self.assertIn(needle, self.src, needle)
        self.assertLess(self.src.index(acquire), self.src.index(call))
        self.assertLess(self.src.index(call), self.src.index(release))

    def test_line_pass_is_gated_on_supplier_purchases_that_will_be_saved(self):
        self.assertIn("INVOICE_LINES_ENABLED", self.src)
        self.assertIn(
            "and classification.receipt_type == ReceiptType.SUPPLIER_PURCHASE",
            self.src,
        )
        self.assertIn('and not should_queue(verification["confidence"])', self.src)

    def test_line_pass_uses_the_shared_ocr_code_path(self):
        # The offline shadow-run (scripts/uom_seeding_worklist.py) calls the
        # same function. If bot.py inlined its own call again, a seeding
        # worklist could be built from a different prompt than production runs.
        self.assertIn("from invoice_ocr import call_line_items_ocr", self.src)
        self.assertIn(
            "call_line_items_ocr, zai_client, image_bytes, model=ZAI_MODEL", self.src
        )
        self.assertNotIn("LINE_ITEMS_PROMPT", self.src)

    def test_line_pass_failure_does_not_abort_the_receipt(self):
        idx = self.src.index("invoice_lines = await extract_invoice_lines(image_bytes)")
        window = self.src[idx: idx + 400]
        self.assertIn("except Exception:", window)
        self.assertIn("invoice_lines = None", window)

    def test_classification_runs_after_verification(self):
        verify = "verification, verify_prefix = await run_verification(image_bytes, parsed)"
        classify = "classification = classify_receipt("
        self.assertIn(verify, self.src)
        self.assertIn(classify, self.src)
        # Corrections are applied by the verifier, so classification must see
        # the corrected merchant/total — not the raw first-pass values.
        self.assertLess(self.src.index(verify), self.src.index(classify))

    def test_classification_is_computed_exactly_once(self):
        self.assertEqual(self.src.count("classification = classify_receipt("), 1)

    def test_lines_are_persisted_only_for_supplier_purchases(self):
        persist = "build_and_save_receipt_items,"
        self.assertIn(persist, self.src)
        # The non-purchase branches all return before the fall-through block.
        fallthrough = "# Fall-through: SUPPLIER_PURCHASE only."
        self.assertLess(self.src.index(fallthrough), self.src.index(persist))

    def test_persistence_failure_is_non_critical(self):
        idx = self.src.index("# === Invoice line items into receipt_items")
        end = self.src.index("# === End invoice line items ===")
        block = self.src[idx:end]
        self.assertIn("except Exception as e:", block)
        self.assertIn("non-critical", block)

    def test_mismatched_invoice_is_posted_back_to_the_outlet_group(self):
        idx = self.src.index("# === Invoice line items into receipt_items")
        end = self.src.index("# === End invoice line items ===")
        block = self.src[idx:end]
        self.assertIn('if subtotal_check.get("needs_review"):', block)
        self.assertIn("format_subtotal_flag_message", block)
        # message.reply_text lands in the chat the photo came from — the outlet
        # group — not in the ops alert channel.
        self.assertIn("await message.reply_text(flag_text)", block)

    def test_unknown_units_are_reported_to_ops(self):
        idx = self.src.index("# === Invoice line items into receipt_items")
        end = self.src.index("# === End invoice line items ===")
        block = self.src[idx:end]
        self.assertIn("unconvertible_units(item_rows)", block)
        self.assertIn("chat_id=ALERT_CHAT_ID", block)

    def test_stored_outlet_is_canonicalised(self):
        idx = self.src.index("def build_and_save_receipt_items(")
        block = self.src[idx: idx + 1800]
        self.assertIn("from outlet_resolver import canonical_outlet", block)
        self.assertIn("outlet=canonical_outlet(outlet) or outlet", block)

    def test_receipt_record_wins_over_the_second_ocr_pass(self):
        idx = self.src.index("def build_and_save_receipt_items(")
        block = self.src[idx: idx + 1800]
        self.assertIn('invoice_date=stored.get("receipt_date")', block)
        self.assertIn('supplier=stored.get("merchant") or invoice_lines.get("supplier")', block)


class BotWastageCommandWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_command_is_registered(self):
        self.assertIn(
            'app.add_handler(CommandHandler("wastage_purchases", wastage_purchases_command))',
            self.src,
        )

    def test_command_is_reviewer_gated(self):
        idx = self.src.index("async def wastage_purchases_command(")
        block = self.src[idx: idx + 900]
        self.assertIn("is_reviewer(_command_owner_id(update))", block)

    def test_command_validates_its_arguments(self):
        idx = self.src.index("async def wastage_purchases_command(")
        block = self.src[idx: idx + 2600]
        self.assertIn("Usage: /wastage_purchases <outlet> <YYYY-MM>", block)
        self.assertIn("wastage_export.parse_month(month)", block)

    def test_command_sends_a_csv_document(self):
        idx = self.src.index("async def wastage_purchases_command(")
        block = self.src[idx: self.src.index("async def cash_no_receipt_today_command(")]
        self.assertIn("wastage_export.build_csv", block)
        self.assertIn("send_document(", block)
        self.assertIn("filename=filename", block)

    def test_command_is_documented(self):
        self.assertIn("/wastage_purchases <outlet> <YYYY-MM>", self.src)
        self.assertIn('BotCommand("wastage_purchases"', self.src)


class MigrationShape(unittest.TestCase):
    """The SQL is not executed in CI, so pin the parts the code depends on."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_ROOT, "migrations", "0038_receipt_items_uom.sql")
        with open(path) as f:
            cls.sql = f.read()

    def test_receipt_items_columns(self):
        for column in ("receipt_id", "outlet", "invoice_date", "supplier",
                       "line_no", "raw_item_text", "qty", "unit_raw",
                       "unit_price", "line_total", "canonical_item",
                       "qty_base", "base_unit", "confidence", "needs_review"):
            self.assertIn(column, self.sql, column)

    def test_base_unit_check_constraint(self):
        self.assertIn("CHECK (base_unit IN ('g', 'ml', 'pcs'))", self.sql)

    def test_receipt_id_is_a_foreign_key(self):
        self.assertIn("bigint REFERENCES public.receipts(id) ON DELETE CASCADE", self.sql)

    def test_upsert_key_exists(self):
        # save_receipt_items upserts on (receipt_id, line_no).
        self.assertIn("UNIQUE (receipt_id, line_no)", self.sql)

    def test_needs_review_defaults_to_false(self):
        self.assertIn("needs_review    boolean NOT NULL DEFAULT false", self.sql)

    def test_rls_is_enabled_on_both_tables(self):
        self.assertIn("ALTER TABLE public.receipt_items  ENABLE ROW LEVEL SECURITY;", self.sql)
        self.assertIn("ALTER TABLE public.uom_conversion ENABLE ROW LEVEL SECURITY;", self.sql)
        self.assertIn("CREATE POLICY receipt_items_service_role", self.sql)
        self.assertIn("CREATE POLICY uom_conversion_service_role", self.sql)

    def test_ambiguous_units_are_not_seeded(self):
        # These have no universal factor; seeding a guess is the one thing this
        # feature must never do.
        # Slice the executed VALUES block only — the commented-out example
        # above it deliberately mentions PAPAN as the thing to add by hand.
        seed_start = self.sql.index("VALUES\n    (NULL")
        seed = self.sql[seed_start: self.sql.index("ON CONFLICT DO NOTHING;")]
        for unit in ("'GUNI'", "'PAPAN'", "'CTN'", "'BTL'", "'PKT'", "'TIN'"):
            self.assertNotIn(unit, seed, unit)

    def test_definitional_units_are_seeded(self):
        for unit in ("'KG'", "'G'", "'L'", "'ML'", "'PCS'", "'EKOR'"):
            self.assertIn(unit, self.sql, unit)

    def test_unreachable_scope_is_rejected(self):
        # A supplier-scoped row with no canonical_item can never match under
        # the documented lookup order.
        self.assertIn("supplier IS NULL OR canonical_item IS NOT NULL", self.sql)


if __name__ == "__main__":
    unittest.main()
