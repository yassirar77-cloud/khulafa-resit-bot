"""Source-level checks that bot.py wires the manual-review queue (PR #29b).

Like the existing ``BotGatingTests``, these read bot.py as text rather than
importing it — bot.py has runtime deps (apscheduler, telegram, supabase) and
required env vars that aren't present in CI/dev. The executable behaviour is
covered by ``test_pending_review.py`` against the pure helpers.
"""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BotReviewFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_low_confidence_routes_to_pending_review(self):
        # handle_photo gates on the stored verifier confidence and routes.
        self.assertIn('if should_queue(verification["confidence"]):', self.src)
        self.assertIn("await route_to_review(message, context, parsed, verification", self.src)
        # route_to_review writes to the queue, not to receipts.
        self.assertIn("store_pending_review(", self.src)

    def test_high_confidence_still_saves_directly(self):
        # The routing is an early return; the normal save path is untouched
        # and lives AFTER it, so at-or-above-floor receipts fall through.
        guard = 'if should_queue(verification["confidence"]):'
        save = "stored = await asyncio.to_thread(store_receipt, record)"
        self.assertIn(guard, self.src)
        self.assertIn(save, self.src)
        guard_idx = self.src.index(guard)
        save_idx = self.src.index(save)
        self.assertLess(guard_idx, save_idx, "save path must follow the review guard")
        # An early return separates the guard from the save path.
        between = self.src[guard_idx:save_idx]
        self.assertIn("return", between)

    def test_save_as_is_callback_promotes_to_receipts(self):
        self.assertIn('if action == "save":', self.src)
        self.assertIn('_finalize_review(review_id, reviewer_chat_id, "approved")', self.src)
        # Promotion ultimately inserts into receipts.
        self.assertIn("def promote_pending_to_receipt(", self.src)
        self.assertIn("return store_receipt(record)", self.src)
        self.assertIn("asyncio.to_thread(promote_pending_to_receipt, pending, edits)", self.src)

    def test_discard_callback_marks_rejected(self):
        self.assertIn('if action == "discard":', self.src)
        self.assertIn('update_pending_review, review_id, "rejected", reviewer_chat_id', self.src)
        # Discard must not promote to receipts.
        discard_idx = self.src.index('if action == "discard":')
        save_idx = self.src.index('if action == "save":')
        discard_block = self.src[discard_idx:save_idx]
        self.assertNotIn("promote_pending_to_receipt", discard_block)

    def test_edit_callback_starts_conversation_flow(self):
        self.assertIn(r'CallbackQueryHandler(review_edit_start, pattern=r"^review:\d+:edit$")', self.src)
        self.assertIn("return REVIEW_EDIT_TOTAL", self.src)
        for state in ("REVIEW_EDIT_TOTAL", "REVIEW_EDIT_MERCHANT", "REVIEW_EDIT_DATE"):
            self.assertIn(state, self.src)
        # Completing the edit flow writes edited_data and marks 'edited'.
        self.assertIn('_finalize_review(review_id, reviewer_chat_id, "edited", edits)', self.src)

    def test_non_reviewer_callback_ignored(self):
        # Both the action handler and the edit entry point gate on is_reviewer.
        self.assertGreaterEqual(
            self.src.count("if not is_reviewer(reviewer_chat_id):"), 2,
            "both save/discard and edit-entry must reject non-reviewers",
        )
        # The action handler returns (no DB write) when not authorised.
        action_idx = self.src.index("async def handle_review_action(")
        gate_idx = self.src.index("if not is_reviewer(reviewer_chat_id):", action_idx)
        tail = self.src[gate_idx:gate_idx + 200]
        self.assertIn("return", tail)

    def test_handlers_registered(self):
        self.assertIn("app.add_handler(build_review_edit_conversation())", self.src)
        self.assertIn(
            r'CallbackQueryHandler(handle_review_action, pattern=r"^review:\d+:(save|discard)$")',
            self.src,
        )
        # Edit conversation must be registered before the audit-reply handler
        # so in-flow text replies aren't swallowed by it.
        conv_idx = self.src.index("build_review_edit_conversation()")
        audit_idx = self.src.index("handle_audit_reply)\n    )")
        self.assertLess(conv_idx, audit_idx)


class AuditFixes(unittest.TestCase):
    """Source-level regression checks for the 2026-07 audit fixes."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_finalize_review_claims_before_promoting(self):
        # Every reviewer is DM'd the same item; without a compare-and-swap two
        # near-simultaneous Save taps both promote -> duplicate receipts.
        fin_idx = self.src.index("async def _finalize_review(")
        block = self.src[fin_idx:fin_idx + 1500]
        claim_idx = block.index("claim_pending_review")
        promote_idx = block.index("promote_pending_to_receipt")
        self.assertLess(claim_idx, promote_idx, "must claim BEFORE promoting")
        # The claim itself is a CAS on status='pending'.
        claim_def = self.src.index("def claim_pending_review(")
        claim_body = self.src[claim_def:claim_def + 800]
        self.assertIn('.eq("status", "pending")', claim_body)

    def test_advances_command_is_reviewer_gated(self):
        idx = self.src.index("async def advances_command(")
        head = self.src[idx:idx + 300]
        self.assertIn("is_reviewer(_command_owner_id(update))", head)
        # The Y/N confirmation reply is gated too — otherwise anyone in the
        # group can confirm a reviewer's pending repayment.
        conf_idx = self.src.index("async def handle_advances_confirmation(")
        conf_block = self.src[conf_idx:self.src.index("answer = ", conf_idx)]
        self.assertIn("is_reviewer(_command_owner_id(update))", conf_block)

    def test_verifier_date_correction_syncs_receipt_date(self):
        # handle_photo copies date->receipt_date BEFORE verification and the
        # stored record prefers receipt_date; _apply_corrections must keep
        # them in sync or the verifier's date fix is silently dropped.
        idx = self.src.index("def _apply_corrections(")
        block = self.src[idx:idx + 2000]
        self.assertIn('parsed["receipt_date"] = new_val', block)

    def test_big_purchase_excludes_current_receipt(self):
        idx = self.src.index("def _check_big_purchase(")
        block = self.src[idx:idx + 1000]
        self.assertIn("current_id", block)
        self.assertIn("_check_big_purchase(chat_id, total, current_id)", self.src)

    def test_apply_all_paths_paginate_past_row_cap(self):
        # .limit(1_000_000) is clamped server-side to ~1000; apply-all must
        # fetch every page or it reports "applied ALL" after one page.
        self.assertNotIn("1_000_000", self.src)
        self.assertIn("_fetch_pending_audit_rows, None", self.src)
        self.assertIn("_fetch_pending_backfill_rows, None", self.src)
        self.assertIn("from db_pagination import fetch_all_pages", self.src)

    def test_receipt_reply_is_chunked_and_non_fatal(self):
        # A many-item receipt pushing the reply past 4096 chars must not abort
        # the post-store pipeline (side tables, price aggregation, audit).
        self.assertIn("await _reply_chunked(message, user_alert)", self.src)
        idx = self.src.index("await _reply_chunked(message, user_alert)")
        self.assertIn("except Exception", self.src[idx - 200:idx + 300])


class MigrationFile(unittest.TestCase):
    def test_pending_review_migration_exists_and_shaped(self):
        path = os.path.join(REPO_ROOT, "migrations", "0005_pending_review.sql")
        self.assertTrue(os.path.exists(path), "0005_pending_review.sql missing")
        with open(path) as f:
            ddl = f.read()
        self.assertIn("CREATE TABLE IF NOT EXISTS public.pending_review", ddl)
        self.assertIn("idx_pending_review_status", ddl)
        for token in ("'pending'", "'approved'", "'edited'", "'rejected'"):
            self.assertIn(token, ddl)


if __name__ == "__main__":
    unittest.main()
