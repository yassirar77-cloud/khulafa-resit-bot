"""Unit tests for the manual-review queue helpers (PR #29b).

These exercise the pure logic in ``pending_review`` and ``config.reviewers``
directly — no telegram/supabase needed.
"""

import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pending_review  # noqa: E402
from pending_review import (  # noqa: E402
    apply_edits_to_parsed,
    build_review_reason,
    serialize_parsed_for_review,
    should_queue,
)


class ShouldQueue(unittest.TestCase):
    def test_below_floor_queues(self):
        self.assertTrue(should_queue(30, []))  # floor is 40

    def test_at_floor_does_not_queue(self):
        self.assertFalse(should_queue(40, []))
        self.assertFalse(should_queue(50, []))  # 50 now stays out of review

    def test_above_floor_does_not_queue(self):
        self.assertFalse(should_queue(80, []))

    def test_flags_do_not_override_final_value(self):
        # 85 is already the adjusted value; flags are advisory only.
        self.assertFalse(should_queue(85, ["decimal_corrected"]))

    def test_none_confidence_queues(self):
        # Verifier couldn't run -> we can't vouch for it -> review.
        self.assertTrue(should_queue(None))

    def test_garbage_confidence_queues(self):
        self.assertTrue(should_queue("not-a-number"))

    def test_floor_is_configurable(self):
        with mock.patch.dict(os.environ, {"REVIEW_CONFIDENCE_FLOOR": "75"}):
            self.assertTrue(should_queue(70))
            self.assertFalse(should_queue(75))

    def test_malformed_floor_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"REVIEW_CONFIDENCE_FLOOR": "abc"}):
            self.assertEqual(pending_review.review_confidence_floor(), 40)


class BuildReviewReason(unittest.TestCase):
    def test_verifier_wrong(self):
        self.assertEqual(build_review_reason(40, "WRONG"), "verifier_wrong")

    def test_verifier_partial(self):
        self.assertEqual(build_review_reason(55, "PARTIAL"), "verifier_partial")

    def test_unchecked_or_none(self):
        self.assertEqual(build_review_reason(None, "UNCHECKED"), "verifier_unchecked")
        self.assertEqual(build_review_reason(None, "CONFIRMED"), "verifier_unchecked")

    def test_conflict_appended(self):
        self.assertEqual(
            build_review_reason(40, "WRONG", ocr_items_conflict=True),
            "verifier_wrong,ocr_items_conflict",
        )

    def test_fallback_low_confidence(self):
        # CONFIRMED but still under floor, no conflict -> generic reason.
        self.assertEqual(build_review_reason(55, "CONFIRMED"), "low_confidence")


class SerializeParsedForReview(unittest.TestCase):
    def test_maps_canonical_fields(self):
        parsed = {
            "merchant": "EVEREST",
            "total": 42.0,
            "receipt_date": "2026-05-20",
            "items": [{"name": "Tube Ice", "price": 42.0}],
        }
        out = serialize_parsed_for_review(parsed)
        self.assertEqual(out["parsed_merchant"], "EVEREST")
        self.assertEqual(out["parsed_total"], 42.0)
        self.assertEqual(out["parsed_date"], "2026-05-20")
        self.assertEqual(len(out["parsed_items"]), 1)

    def test_falls_back_to_legacy_date_key(self):
        out = serialize_parsed_for_review({"date": "2026-05-20"})
        self.assertEqual(out["parsed_date"], "2026-05-20")

    def test_none_safe(self):
        out = serialize_parsed_for_review(None)
        self.assertIsNone(out["parsed_merchant"])
        self.assertEqual(out["parsed_items"], [])


class ApplyEditsToParsed(unittest.TestCase):
    BASE = {"merchant": "EVEREST", "total": 40.0, "receipt_date": "2026-05-20"}

    def test_overrides_provided_fields(self):
        out = apply_edits_to_parsed(self.BASE, {"total": 42.0, "merchant": "EVEREST AISVARAM"})
        self.assertEqual(out["total"], 42.0)
        self.assertEqual(out["merchant"], "EVEREST AISVARAM")
        self.assertEqual(out["receipt_date"], "2026-05-20")  # untouched

    def test_none_means_keep(self):
        out = apply_edits_to_parsed(self.BASE, {"total": None, "merchant": "X"})
        self.assertEqual(out["total"], 40.0)
        self.assertEqual(out["merchant"], "X")

    def test_empty_edits_returns_copy(self):
        out = apply_edits_to_parsed(self.BASE, {})
        self.assertEqual(out, self.BASE)
        self.assertIsNot(out, self.BASE)

    def test_does_not_mutate_input(self):
        apply_edits_to_parsed(self.BASE, {"total": 99.0})
        self.assertEqual(self.BASE["total"], 40.0)


class IsReviewer(unittest.TestCase):
    def setUp(self):
        # Reload with a known reviewer set so the test is independent of env.
        self.reviewers = importlib.import_module("config.reviewers")

    def test_authorised_chat_id(self):
        with mock.patch.object(self.reviewers, "REVIEWER_CHAT_IDS", frozenset({111, 222})):
            self.assertTrue(self.reviewers.is_reviewer(111))
            self.assertTrue(self.reviewers.is_reviewer("222"))  # coerced

    def test_unauthorised_chat_id(self):
        with mock.patch.object(self.reviewers, "REVIEWER_CHAT_IDS", frozenset({111})):
            self.assertFalse(self.reviewers.is_reviewer(999))

    def test_malformed_chat_id(self):
        with mock.patch.object(self.reviewers, "REVIEWER_CHAT_IDS", frozenset({111})):
            self.assertFalse(self.reviewers.is_reviewer(None))
            self.assertFalse(self.reviewers.is_reviewer("abc"))

    def test_env_loading_skips_blank_and_malformed(self):
        with mock.patch.dict(os.environ, {"YASSIR_CHAT_ID": "123", "ARIFFIN_CHAT_ID": ""}, clear=False):
            ids = self.reviewers._load_reviewer_ids()
        self.assertIn(123, ids)


if __name__ == "__main__":
    unittest.main()
