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
