"""Source-level checks that bot.py wires the receipt fingerprint guard, the
supplier scorecard and the cash-out forecast.

bot.py can't be imported in tests (Telegram/Supabase clients and env vars),
so, like the other *_wiring tests, these read it as text. The executable
behaviour lives in test_receipt_fingerprint.py, test_supplier_scorecard.py
and test_cash_forecast.py against the pure modules."""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _block(src, marker):
    start = src.index(marker)
    end = src.find("\nasync def ", start + 1)
    return src[start:end if end != -1 else len(src)]


class IntelligenceWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()
        with open(os.path.join(REPO_ROOT, "migrations", "0071_receipt_fingerprints.sql")) as f:
            cls.sql = f.read()

    def test_modules_are_imported(self):
        for mod in ("receipt_fingerprint", "supplier_scorecard", "cash_forecast"):
            self.assertIn(f"import {mod}\n", self.src)

    def test_photo_hash_is_taken_from_the_original_bytes_before_resize(self):
        photo = _block(self.src, "async def handle_photo(")
        self.assertIn("photo_hash = None", photo)
        hashed = photo.index("photo_hash = await asyncio.to_thread(receipt_fingerprint.dhash, image_bytes)")
        download = photo.index("_photo_requeues.pop(photo_key, None)")
        resize = photo.index("resize_for_ocr, image_bytes, IMAGE_MAX_DIM")
        self.assertLess(download, hashed)
        self.assertLess(hashed, resize)

    def test_hook_runs_after_the_save_for_every_receipt_type(self):
        photo = _block(self.src, "async def handle_photo(")
        save = photo.index("stored = await asyncio.to_thread(store_receipt, record)")
        hook = photo.index("await fingerprint_on_upload(context, stored, parsed, message, photo_hash)")
        pinpoint = photo.index("if receipt_type in _OUTSIDE_RECEIPT_TYPES:")
        self.assertLess(save, hook)
        self.assertLess(hook, pinpoint)
        # Not inside any receipt-type branch: the hook sits at function level.
        line = photo[photo.rfind("\n", 0, hook) + 1:hook]
        self.assertEqual(line, "    ")

    def test_hook_never_breaks_the_pipeline_and_routes_matches(self):
        hook = _block(self.src, "async def fingerprint_on_upload(")
        self.assertIn("receipt_fingerprint.check_and_record, supabase, stored, parsed, photo_hash", hook)
        self.assertIn("except Exception:", hook)
        self.assertIn("if result[\"director\"]:", hook)
        self.assertIn("chat_id=ALERT_CHAT_ID", hook)
        self.assertIn("receipt_fingerprint.format_director_alert(result[\"row\"], result[\"director\"])", hook)
        self.assertIn("if result[\"group\"]:", hook)
        self.assertIn("receipt_fingerprint.format_group_reply(result[\"group\"][0])", hook)
        self.assertNotIn("extract_with_glm", hook)

    def test_commands_are_admin_gated_and_registered(self):
        for name in ("supplier_score", "cash_forecast", "resit_sama"):
            self.assertIn(f'app.add_handler(CommandHandler("{name}", {name}_command))', self.src)
            body = _block(self.src, f"async def {name}_command(")
            self.assertIn("if not message or not _outside_admin(update):", body)
            self.assertIn("asyncio.to_thread(", body)
        self.assertIn("/supplier_score [days]", self.src)
        self.assertIn("/cash_forecast [outlet] [days]", self.src)
        self.assertIn("/resit_sama [days]", self.src)

    def test_weekly_jobs_go_to_the_director_chat_only(self):
        for fn, job_id, dow in (("post_supplier_scorecard", "supplier_scorecard", "mon"),
                                ("post_cash_forecast", "cash_forecast_weekly", "sun")):
            body = _block(self.src, f"async def {fn}(")
            self.assertIn("chat_id=notify_chat_id or ALERT_CHAT_ID", body)
            self.assertIn("except Exception:", body)
            self.assertNotIn("group_reports", body)
            sched = self.src[self.src.index(f"        {fn},\n"):]
            sched = sched[:sched.index("replace_existing=True")]
            self.assertIn(f'day_of_week="{dow}"', sched)
            self.assertIn(f'id="{job_id}"', sched)

    def test_migration_locks_the_table_down(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS public.receipt_fingerprints", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FOR ALL TO service_role", self.sql)
        self.assertIn("REVOKE ALL ON public.receipt_fingerprints FROM anon, authenticated", self.sql)
        for col in ("dhash", "invoice_no", "matched_receipt_id", "match_kind", "match_outlet"):
            self.assertIn(col, self.sql)


if __name__ == "__main__":
    unittest.main()
