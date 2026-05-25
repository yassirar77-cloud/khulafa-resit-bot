"""Tests for the auto-review-too-aggressive fix.

Layer 1: math agreement -> confidence 100 (overrides verifier).
Layer 2: a WRONG verdict only drops to 80, not the verifier's raw score.
Layer 3: review floor lowered to 40.
Layer 4: duplicate review within 24h is skipped.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pending_review as pr  # noqa: E402
from ocr_quality import items_sum_matches_total  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 5 lines summing to exactly 372.50 (the production FOOK LEONG receipt).
MATCHING_ITEMS = [
    {"name": "a", "qty": 1, "price": 183.60},
    {"name": "b", "qty": 1, "price": 62.40},
    {"name": "c", "qty": 1, "price": 31.50},
    {"name": "d", "qty": 1, "price": 35.0},
    {"name": "e", "qty": 1, "price": 60.0},
]
TOTAL = 372.50


class ItemsSumMatchesTotal(unittest.TestCase):
    def test_matches_within_tolerance(self):
        self.assertTrue(items_sum_matches_total(TOTAL, MATCHING_ITEMS))
        self.assertTrue(items_sum_matches_total(372.5 + 3.0, MATCHING_ITEMS))  # within 1%

    def test_no_match_outside_tolerance(self):
        self.assertFalse(items_sum_matches_total(400.0, MATCHING_ITEMS))  # 7%+ off
        self.assertFalse(items_sum_matches_total(None, MATCHING_ITEMS))
        self.assertFalse(items_sum_matches_total(372.50, None))
        self.assertFalse(items_sum_matches_total(0, MATCHING_ITEMS))

    def test_qty_times_price(self):
        # multi-unit line: 100 × 1.50 = 150 reconciles to 150
        self.assertTrue(items_sum_matches_total(150.0, [{"qty": 100, "price": 1.50}]))


class ResolveConfidence(unittest.TestCase):
    def test_items_match_total_assigns_confidence_100(self):
        self.assertEqual(pr.resolve_confidence("CONFIRMED", 95, MATCHING_ITEMS, TOTAL), 100)

    def test_items_match_total_overrides_verifier_wrong(self):
        # verifier said WRONG with confidence 30, but the math reconciles
        self.assertEqual(pr.resolve_confidence("WRONG", 30, MATCHING_ITEMS, TOTAL), 100)

    def test_verifier_wrong_only_drops_to_80(self):
        # WRONG verdict, math does NOT reconcile -> 80 (not 30)
        self.assertEqual(pr.resolve_confidence("WRONG", 30, MATCHING_ITEMS, 400.0), 80)
        self.assertEqual(pr.resolve_confidence("WRONG", 10, None, 400.0), 80)

    def test_confirmed_partial_keep_verifier_score(self):
        self.assertEqual(pr.resolve_confidence("CONFIRMED", 95, None, 400.0), 95)
        self.assertEqual(pr.resolve_confidence("PARTIAL", 70, None, 400.0), 70)

    def test_unchecked_passes_through_none(self):
        self.assertIsNone(pr.resolve_confidence("UNCHECKED", None, None, 400.0))
        # but math agreement rescues even an unchecked receipt
        self.assertEqual(pr.resolve_confidence("UNCHECKED", None, MATCHING_ITEMS, TOTAL), 100)

    def test_confidence_calculation_priority_order(self):
        # 1) math match beats everything
        self.assertEqual(pr.resolve_confidence("WRONG", 10, MATCHING_ITEMS, TOTAL), 100)
        # 2) no match + WRONG -> 80
        self.assertEqual(pr.resolve_confidence("WRONG", 95, MATCHING_ITEMS, 400.0), 80)
        # 3) no match + CONFIRMED -> verifier score
        self.assertEqual(pr.resolve_confidence("CONFIRMED", 88, MATCHING_ITEMS, 400.0), 88)


class ReviewFloor(unittest.TestCase):
    def test_review_floor_40_not_60(self):
        self.assertEqual(pr.DEFAULT_CONFIDENCE_FLOOR, 40)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pr.review_confidence_floor(), 40)
            self.assertFalse(pr.should_queue(80))   # 80 stays out of review
            self.assertFalse(pr.should_queue(40))   # boundary: 40 is not < 40
            self.assertTrue(pr.should_queue(39))    # truly bad
            self.assertTrue(pr.should_queue(None))  # unverifiable -> review

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"REVIEW_CONFIDENCE_FLOOR": "50"}, clear=True):
            self.assertEqual(pr.review_confidence_floor(), 50)


class DuplicateReview(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        self.parsed = {"merchant": "FOOK LEONG SEA PRODUCTS", "total": 372.50, "receipt_date": "2026-05-25"}

    def _row(self, hours_ago, **kw):
        base = {
            "parsed_merchant": "FOOK LEONG SEA PRODUCTS",
            "parsed_total": 372.50,
            "parsed_date": "2026-05-25",
            "created_at": (self.now - timedelta(hours=hours_ago)).isoformat(),
        }
        base.update(kw)
        return base

    def test_duplicate_review_skipped_within_24h(self):
        rows = [self._row(1)]
        self.assertTrue(pr.is_duplicate_review(rows, self.parsed, now=self.now))

    def test_outside_24h_not_duplicate(self):
        rows = [self._row(30)]
        self.assertFalse(pr.is_duplicate_review(rows, self.parsed, now=self.now))

    def test_different_receipt_not_duplicate(self):
        self.assertFalse(pr.is_duplicate_review([self._row(1, parsed_total=99.0)], self.parsed, now=self.now))
        self.assertFalse(pr.is_duplicate_review([self._row(1, parsed_merchant="BABAS")], self.parsed, now=self.now))

    def test_empty_parsed_never_duplicate(self):
        self.assertFalse(pr.is_duplicate_review([self._row(1)], {}, now=self.now))

    def test_total_rounding_tolerant(self):
        # 372.5 vs 372.50 should still match
        rows = [self._row(1, parsed_total=372.5)]
        self.assertTrue(pr.is_duplicate_review(rows, {"merchant": "FOOK LEONG SEA PRODUCTS", "total": 372.50, "receipt_date": "2026-05-25"}, now=self.now))


class BotWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_run_verification_uses_resolve_confidence(self):
        idx = self.src.index("async def run_verification(")
        body = self.src[idx:idx + 2500]
        self.assertIn("resolve_confidence(", body)

    def test_route_to_review_dedups(self):
        idx = self.src.index("async def route_to_review(")
        body = self.src[idx:idx + 1200]
        self.assertIn("is_duplicate_review(", body)


if __name__ == "__main__":
    unittest.main()
