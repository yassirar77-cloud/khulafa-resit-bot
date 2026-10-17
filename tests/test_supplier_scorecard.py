"""supplier_scorecard: scoring dimensions, grades, flags and texts."""
import unittest
from datetime import date, timedelta

import supplier_scorecard as sc
from tests.fake_supabase import FakeSupabase

TODAY = date(2026, 10, 15)
DAYS = 30


def _receipts(merchant, n, *, total=200, confidence=95, mismatch_every=0, outlet="SEK20", step=3):
    rows = []
    for i in range(n):
        rows.append({
            "id": 1000 + i, "merchant": merchant, "outlet": outlet,
            "receipt_date": (TODAY - timedelta(days=1 + i * step)).isoformat(),
            "total": total, "confidence": confidence, "receipt_type": "SUPPLIER_PURCHASE",
            "po_mismatch": [{"kind": "qty"}] if mismatch_every and i % mismatch_every == 0 else None,
        })
    return rows


def _lines(merchant, item, prices, *, outlet="SEK20", step=3, start=1):
    rows = []
    for i, p in enumerate(prices):
        rows.append({"receipt_id": 1000 + i, "merchant": merchant, "canonical_item": item,
                     "unit_price": p, "outlet_code": outlet,
                     "receipt_date": (TODAY - timedelta(days=start + i * step)).isoformat()})
    return rows


class Scoring(unittest.TestCase):
    def test_steady_clean_supplier_is_grade_a(self):
        receipts = _receipts("BESTARI FARM", 10)
        lines = _lines("BESTARI FARM", "ayam", [9.5] * 10)
        card = sc.score_all(receipts, lines, today=TODAY, days=DAYS)[0]
        self.assertTrue(card["scored"])
        self.assertEqual(card["grade"], "A")
        self.assertEqual(card["score"], 100.0)
        self.assertEqual(card["concern"], "steady prices, bills read cleanly")
        # No order-vs-bill check in the window: accuracy is not counted, not perfect.
        self.assertIn("accuracy", card["missing"])
        self.assertNotIn("accuracy", card["points"])
        self.assertEqual(card["spend"], 2000.0)

    def test_swinging_prices_lose_stability_points(self):
        receipts = _receipts("SAYUR", 10)
        lines = _lines("SAYUR", "bawang", [4, 6, 4, 7, 3, 6, 4, 8, 3, 6])
        card = sc.score_all(receipts, lines, today=TODAY, days=DAYS)[0]
        self.assertLess(card["points"]["stability"], 20)
        self.assertIn("unit prices swing", card["concern"])

    def test_rising_prices_lose_direction_points(self):
        receipts = _receipts("SAIDA", 10)
        # newest first in _lines: recent half RM22, older half RM18 -> +22%
        lines = _lines("SAIDA", "jintan", [22, 22, 22, 22, 22, 18, 18, 18, 18, 18])
        card = sc.score_all(receipts, lines, today=TODAY, days=DAYS)[0]
        self.assertEqual(card["points"]["direction"], 0.0)
        self.assertAlmostEqual(card["trend_pct"], 22.2, places=1)
        self.assertEqual(card["movers"][0]["item"], "jintan")
        # Direction lost all 15 points, stability about half of 40: the
        # dimension that lost the larger SHARE is the concern.
        self.assertIn("prices up +22%", card["concern"])
        self.assertIn("RM18.00 → RM22.00", card["concern"])

    def test_mismatches_and_unreadable_bills_cost_points(self):
        receipts = _receipts("FOOK LEONG", 10, mismatch_every=2, confidence=60)
        card = sc.score_all(receipts, [], today=TODAY, days=DAYS)[0]
        self.assertEqual(card["mismatch_rate"], 0.5)
        self.assertEqual(card["points"]["accuracy"], 0.0)
        self.assertEqual(card["points"]["legibility"], 0.0)
        self.assertEqual(card["grade"], "D")
        self.assertEqual(card["score"], 0.0)
        # No priced items: stability and direction are "Insufficient data", not
        # a neutral fallback; only accuracy (data present) and legibility count.
        self.assertEqual(card["missing"], ["stability", "direction"])
        self.assertEqual(set(card["points"]), {"accuracy", "legibility"})

    def test_too_few_bills_not_scored_and_listed_last(self):
        receipts = _receipts("BIG", 6) + _receipts("NEWBIE", 2)
        cards = sc.score_all(receipts, [], today=TODAY, days=DAYS)
        self.assertEqual([c["merchant"] for c in cards], ["BIG", "NEWBIE"])
        self.assertFalse(cards[1]["scored"])

    def test_non_purchase_and_unknown_merchants_are_ignored(self):
        receipts = _receipts("TNB", 6)
        for r in receipts:
            r["receipt_type"] = "UTILITY"
        receipts += _receipts("UNKNOWN MERCHANT", 6)
        self.assertEqual(sc.score_all(receipts, [], today=TODAY, days=DAYS), [])

    def test_cross_outlet_gap_is_flagged(self):
        receipts = _receipts("BESTARI FARM", 8)
        lines = (_lines("BESTARI FARM", "ayam", [9.8, 9.8, 9.9], outlet="SEK20")
                 + _lines("BESTARI FARM", "ayam", [8.2, 8.2, 8.1], outlet="KLANG"))
        card = sc.score_all(receipts, lines, today=TODAY, days=DAYS)[0]
        gap = card["outlet_gaps"][0]
        self.assertEqual((gap["high_outlet"], gap["low_outlet"]), ("SEK20", "KLANG"))
        self.assertGreaterEqual(gap["pct"], 15)
        self.assertIn("SEK20 pays RM9.80 vs KLANG RM8.20", sc.format_card(card, DAYS))

    def test_overdue_delivery_is_the_concern_when_scores_are_fine(self):
        receipts = _receipts("HANEE", 6, step=3)
        for r in receipts:   # shift everything 10 days back: last seen 11 days ago, usually 3
            r["receipt_date"] = (date.fromisoformat(r["receipt_date"]) - timedelta(days=10)).isoformat()
        card = sc.score_all(receipts, [], today=TODAY, days=DAYS)[0]
        self.assertTrue(card["delivery"]["overdue"])
        self.assertEqual(card["delivery"]["median_gap"], 3)
        self.assertEqual(card["concern"], "not seen for 11 days (usually every 3)")

    def test_future_misread_dates_do_not_break_delivery(self):
        receipts = _receipts("AYAM BERLIAN", 6)
        receipts[0]["receipt_date"] = "2028-10-06"          # OCR read 2026 as 2028
        card = sc.score_all(receipts, [], today=TODAY, days=DAYS)[0]
        self.assertEqual(card["delivery"]["last_seen_days"], 4)
        self.assertGreaterEqual(card["delivery"]["last_seen_days"], 0)
        self.assertEqual(card["bills"], 6)                    # the bill itself still counts

    def test_robust_cv_ignores_one_bad_line(self):
        self.assertEqual(sc.robust_cv([10, 10, 10, 10, 1000]), 0.0)
        self.assertIsNone(sc.robust_cv([10, 10]))
        self.assertIsNone(sc.robust_cv(["x", None, 0]))

    def test_has_mismatch_reads_json_or_text(self):
        self.assertTrue(sc.has_mismatch({"po_mismatch": [{"kind": "qty"}]}))
        self.assertTrue(sc.has_mismatch({"po_mismatch": '[{"kind": "qty"}]'}))
        self.assertFalse(sc.has_mismatch({"po_mismatch": "[]"}))
        self.assertFalse(sc.has_mismatch({"po_mismatch": None}))
        self.assertFalse(sc.has_mismatch({}))

    def test_grades(self):
        self.assertEqual([sc.grade(x) for x in (100, 85, 84.9, 70, 55, 54)], ["A", "A", "B", "B", "C", "D"])
        self.assertEqual(sc.grade(None), "—")

    def test_score_is_points_over_available_points(self):
        # Only legibility has data: 60% clean -> 12/20 -> 60/100, not 12/100.
        receipts = _receipts("X", 5, confidence=95)
        receipts[0]["confidence"] = receipts[1]["confidence"] = 50
        card = sc.score_all(receipts, [], today=TODAY, days=DAYS)[0]
        self.assertEqual(card["points"], {"legibility": 12.0})
        self.assertEqual(card["score"], 60.0)
        self.assertEqual(card["missing"], ["stability", "direction", "accuracy"])

    def test_board_says_when_order_accuracy_has_no_data(self):
        cards = sc.score_all(_receipts("BESTARI FARM", 6), [], today=TODAY, days=DAYS)
        text = sc.format_board(cards, DAYS)
        self.assertIn("Order accuracy: Insufficient data — no order-vs-bill check recorded in this window", text)
        self.assertFalse(sc.mismatch_data_present(_receipts("BESTARI FARM", 6)))
        self.assertTrue(sc.mismatch_data_present(_receipts("X", 2, mismatch_every=1)))

    def test_potential_duplicates_same_date_and_total(self):
        receipts = _receipts("JUTA RIA", 6)
        receipts.append(dict(receipts[0], id=2000, outlet="Signature"))        # same date + total, other outlet
        receipts.append(dict(receipts[1], id=2001, total=receipts[1]["total"] + 0.5))   # total differs: not a dup
        card = sc.score_all(receipts, [], today=TODAY, days=DAYS)[0]
        self.assertEqual(len(card["duplicates"]), 1)
        self.assertEqual(card["duplicates"][0]["receipt_ids"], [1000, 2000])
        self.assertTrue(card["duplicates"][0]["cross_outlet"])
        self.assertEqual(card["concern"], "1 potential duplicate bill — check the photos")
        text = sc.format_card(card, DAYS)
        self.assertIn("Potential duplicate invoices (same date and total — check the photos):", text)
        self.assertIn("• #1000, #2000 — 2026-10-14 RM200.00 (SEK20 / Signature)", text)

    def test_fingerprint_matches_count_on_the_card(self):
        fp = [{"merchant": "JUTA RIA", "receipt_id": 9, "matched_receipt_id": 1}]
        card = sc.score_all(_receipts("JUTA RIA", 6), [], today=TODAY, days=DAYS, fingerprint_rows=fp)[0]
        self.assertEqual(card["fingerprint_matches"], 1)
        self.assertIn("1 photo / invoice-number match across outlets (/resit_sama)", sc.format_card(card, DAYS))

    def test_unusual_price_lines(self):
        receipts = _receipts("SAYUR", 6)
        lines = _lines("SAYUR", "bawang", [4.0, 4.1, 4.0, 9.5, 4.0, 1.5])
        card = sc.score_all(receipts, lines, today=TODAY, days=DAYS)[0]
        self.assertEqual(card["outlier_count"], 2)
        self.assertEqual({o["price"] for o in card["outliers"]}, {9.5, 1.5})
        text = sc.format_card(card, DAYS)
        self.assertIn("Unusual price lines (2):", text)
        self.assertIn("• bawang: RM9.50 vs usual RM4.00 (#1003, SEK20, ", text)


class Texts(unittest.TestCase):
    def test_board_lists_ranked_watch_and_new(self):
        receipts = (_receipts("BESTARI FARM", 10) + _receipts("FOOK LEONG", 6, mismatch_every=1, confidence=50)
                    + _receipts("NEWBIE", 2))
        cards = sc.score_all(receipts, _lines("BESTARI FARM", "ayam", [9.5] * 10), today=TODAY, days=DAYS)
        text = sc.format_board(cards, DAYS)
        self.assertIn("Supplier scorecard — last 30 days", text)
        self.assertIn("1. BESTARI FARM — A 100 (10 bills, RM2,000)", text)
        self.assertIn("2. FOOK LEONG — D", text)
        self.assertIn("Watch: FOOK LEONG", text)
        self.assertIn("Too few bills to score (< 4): NEWBIE (2, RM400)", text)
        # FOOK LEONG carries mismatch data, so accuracy IS scored on this board.
        self.assertNotIn("Insufficient data", text)

    def test_empty_board(self):
        self.assertIn("No supplier bills", sc.format_board([], DAYS))

    def test_card_and_pick(self):
        cards = sc.score_all(_receipts("BESTARI FARM", 8), _lines("BESTARI FARM", "ayam", [9.5] * 8),
                             today=TODAY, days=DAYS)
        self.assertEqual(len(sc.pick(cards, "bestari")), 1)
        self.assertEqual(sc.pick(cards, "jakel"), [])
        text = sc.format_card(cards[0], DAYS)
        self.assertIn("BESTARI FARM — A 100/100 (last 30 days)", text)
        self.assertIn("Total purchases RM1,600 over 8 bills (actual, recorded); outlets: SEK20", text)
        self.assertIn("Order accuracy: Insufficient data (no order-vs-bill check recorded in this window) — not counted", text)
        self.assertIn("Price stability 40/40 (spread ±0%)", text)
        self.assertIn("Delivers about every 3 days; last seen 1 days ago", text)


class Build(unittest.TestCase):
    def test_build_reads_receipts_and_item_prices(self):
        db = FakeSupabase()
        for r in _receipts("BESTARI FARM", 5):
            db.table("receipts").insert(r).execute()
        for ln in _lines("BESTARI FARM", "ayam", [9.5] * 5):
            db.table("item_prices").insert(ln).execute()
        cards = sc.build(db, TODAY, DAYS)
        self.assertEqual(cards[0]["merchant"], "BESTARI FARM")
        self.assertEqual(cards[0]["bills"], 5)
        self.assertEqual(cards[0]["items_priced"], 1)


if __name__ == "__main__":
    unittest.main()
