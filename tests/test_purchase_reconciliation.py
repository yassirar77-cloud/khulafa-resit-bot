"""Tests for the smart receipt/POS-payout merge (PR #37)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import purchase_reconciliation as pr
from purchase_reconciliation import POSPayout, Receipt

CANONICAL = ["BABAS", "EVEREST", "AIS", "KACHANG", "GARDENIA", "SAIDA"]


def _r(amount, canonical=None, merchant=None, rid=1, created_at=None):
    return Receipt(id=rid, amount=amount, merchant_canonical=canonical,
                   merchant=merchant, created_at=created_at)


def _p(desc, amount, pid=1, created_at=None):
    return POSPayout(id=pid, description=desc, amount=amount, created_at=created_at)


class PrefixAndClassification(unittest.TestCase):
    def test_strip_pos_prefix(self):
        self.assertEqual(pr.strip_pos_prefix("PAY TO BABAS"), "BABAS")
        self.assertEqual(pr.strip_pos_prefix("PAY [LP] KARUNGARAJ"), "KARUNGARAJ")
        self.assertEqual(pr.strip_pos_prefix("PAYOUT TO GAS"), "GAS")
        self.assertEqual(pr.strip_pos_prefix("BAYAR KEPADA SAIDA"), "SAIDA")
        self.assertEqual(pr.strip_pos_prefix("BABAS"), "BABAS")

    def test_staff_advance_detection(self):
        self.assertTrue(pr.is_staff_advance("PAY [LP] KARUNGARAJ"))
        self.assertTrue(pr.is_staff_advance("PAY [AD] STAFF ADVANCE"))
        self.assertTrue(pr.is_staff_advance("PAY TO STAFF SALARY"))
        self.assertFalse(pr.is_staff_advance("PAY TO BABAS"))
        self.assertFalse(pr.is_staff_advance(""))

    def test_pinjam_loan_is_staff_advance(self):
        # "pinjam" (Malay: loan) and the noun "pinjaman" are staff cash loans.
        self.assertTrue(pr.is_staff_advance("PINJAM TO B ALAM pai cash"))
        self.assertTrue(pr.is_staff_advance("PINJAM TO NATARAJAN pai cash"))
        self.assertTrue(pr.is_staff_advance("PAY TO PINJAMAN STAFF"))
        # "pai cash" alone is just a cash-payment method, NOT a loan signal — a
        # genuine supplier paid in cash must stay food cost.
        self.assertFalse(pr.is_staff_advance("PAY TO BABAS pai cash"))

    def test_non_pay_detection(self):
        self.assertTrue(pr.is_non_pay("NON_PAY TO MEWAH DAIRIES OKI SUSU"))
        self.assertTrue(pr.is_non_pay("NON-PAY TO X"))
        self.assertTrue(pr.is_non_pay("NONPAY TO Y"))
        self.assertFalse(pr.is_non_pay("PAY TO BABAS"))
        self.assertFalse(pr.is_non_pay(""))
        self.assertFalse(pr.is_non_pay(None))

    def test_utility_detection(self):
        self.assertTrue(pr.is_utility("PAY TO GAS"))
        self.assertTrue(pr.is_utility("PAY TO TNB"))
        self.assertTrue(pr.is_utility("PAY TO AIR SELANGOR"))
        self.assertTrue(pr.is_utility("PAY TO ASTRO"))
        # "AIS" is ice (a supply), not "AIR" (water) — must NOT be a utility.
        self.assertFalse(pr.is_utility("PAY TO AIS"))
        self.assertFalse(pr.is_utility("PAY TO BABAS"))


class FuzzyMerchant(unittest.TestCase):
    def test_pay_to_prefix_stripped_and_exact_match(self):
        canon, conf = pr.fuzzy_match_merchant("PAY TO BABAS", CANONICAL)
        self.assertEqual(canon, "BABAS")
        self.assertEqual(conf, 1.0)

    def test_substring_match(self):
        canon, conf = pr.fuzzy_match_merchant("PAY TO BABAS MASALA", CANONICAL)
        self.assertEqual(canon, "BABAS")
        self.assertGreater(conf, 0.0)
        self.assertLess(conf, 1.0)

    def test_no_match_returns_none(self):
        canon, conf = pr.fuzzy_match_merchant("PAY TO NOBODY", CANONICAL)
        self.assertIsNone(canon)
        self.assertEqual(conf, 0.0)


class Matching(unittest.TestCase):
    def test_exact_amount_exact_merchant_matches(self):
        res = pr.match_receipts_to_payouts(
            [_r(60.0, canonical="BABAS")], [_p("PAY TO BABAS", 60.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 1)
        self.assertEqual(res.matched[0].confidence, 1.0)
        self.assertEqual(res.matched[0].method, "exact_amount_exact_merchant")
        self.assertEqual(res.cash_no_receipt, [])
        self.assertEqual(res.account_only_receipts, [])

    def test_amount_within_tolerance_matches(self):
        # RM62 receipt vs RM60 POS — within ±RM5.
        res = pr.match_receipts_to_payouts(
            [_r(62.0, canonical="BABAS")], [_p("PAY TO BABAS", 60.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 1)
        self.assertEqual(res.matched[0].method, "fuzzy_amount_exact_merchant")

    def test_amount_over_tolerance_does_not_match(self):
        # RM200 receipt vs RM60 POS — far outside ±RM5 / ±2%.
        res = pr.match_receipts_to_payouts(
            [_r(200.0, canonical="BABAS")], [_p("PAY TO BABAS", 60.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 0)
        self.assertEqual(len(res.cash_no_receipt), 1)   # payout unmatched -> Type B
        self.assertEqual(len(res.account_only_receipts), 1)  # receipt unmatched -> Type C

    def test_fuzzy_merchant_matches_via_raw_name(self):
        # KACHANG isn't the receipt's canonical, but the raw merchant overlaps.
        res = pr.match_receipts_to_payouts(
            [_r(45.0, merchant="KACHANG ICE SUPPLY")], [_p("PAY TO KACHANG", 45.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 1)
        self.assertEqual(res.matched[0].method, "exact_amount_fuzzy_merchant")

    def test_staff_advance_excluded_from_match(self):
        res = pr.match_receipts_to_payouts(
            [], [_p("PAY [LP] KARUNGARAJ", 300.0)], CANONICAL
        )
        self.assertEqual(len(res.excluded_staff), 1)
        self.assertEqual(len(res.matched), 0)
        self.assertEqual(len(res.cash_no_receipt), 0)

    def test_utility_payment_excluded_from_match(self):
        res = pr.match_receipts_to_payouts(
            [], [_p("PAY TO GAS", 250.0)], CANONICAL
        )
        self.assertEqual(len(res.excluded_utility), 1)
        self.assertEqual(len(res.cash_no_receipt), 0)

    def test_multiple_receipts_same_supplier_matched_individually(self):
        # BABAS delivers twice: RM60 and RM120. Two POS payouts at those amounts.
        receipts = [_r(60.0, canonical="BABAS", rid=1), _r(120.0, canonical="BABAS", rid=2)]
        payouts = [_p("PAY TO BABAS", 120.0, pid=10), _p("PAY TO BABAS", 60.0, pid=11)]
        res = pr.match_receipts_to_payouts(receipts, payouts, CANONICAL)
        self.assertEqual(len(res.matched), 2)
        # Each receipt matched its nearest-amount payout.
        pairs = {(m.receipt.amount, m.payout.amount) for m in res.matched}
        self.assertEqual(pairs, {(60.0, 60.0), (120.0, 120.0)})

    def test_pos_payout_no_receipt_creates_alert(self):
        res = pr.match_receipts_to_payouts(
            [], [_p("PAY TO BABAS", 200.0)], CANONICAL
        )
        self.assertEqual(len(res.cash_no_receipt), 1)
        self.assertEqual(res.cash_no_receipt[0].amount, 200.0)

    def test_receipt_no_pos_payout_treated_as_account(self):
        # Petty cash account purchase, no POS entry.
        res = pr.match_receipts_to_payouts(
            [_r(25.0, canonical="SAIDA")], [], CANONICAL
        )
        self.assertEqual(len(res.account_only_receipts), 1)
        self.assertEqual(len(res.matched), 0)


class AmountOnlyFallback(unittest.TestCase):
    """PR #64: ~95% of receipts have no canonical merchant, so a merchant-
    anchored match never fires and both the receipt and its POS payout count,
    doubling food cost. The amount-only fallback dedupes them."""

    def test_amount_only_matches_when_merchant_null(self):
        # No merchant on the receipt, no canonical for the payout, amounts ±RM2.
        res = pr.match_receipts_to_payouts(
            [_r(60.0)], [_p("PAY TO MYSTERY SUPPLIER", 61.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 1)
        self.assertEqual(res.matched[0].method, "amount_only")
        self.assertEqual(res.matched[0].confidence, 0.5)
        self.assertEqual(res.cash_no_receipt, [])
        self.assertEqual(res.account_only_receipts, [])

    def test_amount_only_respects_2rm_tolerance(self):
        # RM63 receipt vs RM60 payout — RM3 apart, outside the ±RM2 amount-only
        # window, so they must NOT be merged (no merchant to lean on).
        res = pr.match_receipts_to_payouts(
            [_r(63.0)], [_p("PAY TO MYSTERY SUPPLIER", 60.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 0)
        self.assertEqual(len(res.cash_no_receipt), 1)        # Type B
        self.assertEqual(len(res.account_only_receipts), 1)  # Type C

    def test_amount_only_does_not_consume_staff_advance(self):
        # A same-amount staff advance must stay excluded (Type D), never matched.
        res = pr.match_receipts_to_payouts(
            [_r(300.0)], [_p("PAY [LP] KARUNGARAJ", 300.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 0)
        self.assertEqual(len(res.excluded_staff), 1)
        self.assertEqual(len(res.account_only_receipts), 1)

    def test_amount_only_does_not_consume_utility(self):
        # A same-amount utility payout stays excluded (Type E), never matched.
        res = pr.match_receipts_to_payouts(
            [_r(250.0)], [_p("PAY TO TNB", 250.0)], CANONICAL
        )
        self.assertEqual(len(res.matched), 0)
        self.assertEqual(len(res.excluded_utility), 1)
        self.assertEqual(len(res.account_only_receipts), 1)

    def test_merchant_match_preferred_over_amount_only(self):
        # Receipt knows its supplier (BABAS); two same-amount payouts compete —
        # the merchant-anchored one must win, the other stays cash-no-receipt.
        receipt = _r(60.0, canonical="BABAS")
        payouts = [_p("PAY TO MYSTERY", 60.0, pid=10),
                   _p("PAY TO BABAS", 60.0, pid=11)]
        res = pr.match_receipts_to_payouts([receipt], payouts, CANONICAL)
        self.assertEqual(len(res.matched), 1)
        self.assertEqual(res.matched[0].payout.id, 11)
        self.assertEqual(res.matched[0].method, "exact_amount_exact_merchant")
        self.assertEqual(len(res.cash_no_receipt), 1)
        self.assertEqual(res.cash_no_receipt[0].id, 10)


class CandidateResolution(unittest.TestCase):
    """PR #64: deterministic one-to-one assignment for the amount-only path."""

    def test_closest_amount_wins(self):
        receipt = _r(60.0)
        payouts = [_p("PAY TO A", 61.5, pid=10),   # RM1.50 away
                   _p("PAY TO B", 60.5, pid=11)]    # RM0.50 away — closer
        res = pr.match_receipts_to_payouts([receipt], payouts, CANONICAL)
        self.assertEqual(len(res.matched), 1)
        self.assertEqual(res.matched[0].payout.id, 11)

    def test_one_receipt_consumes_one_payout(self):
        # Two RM60 receipts, two RM60 payouts -> exactly two matches, each row
        # used once (no cross double-counting).
        receipts = [_r(60.0, rid=1), _r(60.0, rid=2)]
        payouts = [_p("PAY TO X", 60.0, pid=10), _p("PAY TO Y", 60.0, pid=11)]
        res = pr.match_receipts_to_payouts(receipts, payouts, CANONICAL)
        self.assertEqual(len(res.matched), 2)
        self.assertEqual({m.payout.id for m in res.matched}, {10, 11})
        self.assertEqual({m.receipt.id for m in res.matched}, {1, 2})
        self.assertEqual(res.cash_no_receipt, [])
        self.assertEqual(res.account_only_receipts, [])

    def test_tie_breaks_to_earliest_by_time(self):
        # Equal amount distance -> earliest payout by time wins.
        receipt = _r(60.0, rid=1)
        payouts = [
            _p("PAY TO X", 60.0, pid=10, created_at="2026-05-29T10:00:00+00:00"),
            _p("PAY TO Y", 60.0, pid=11, created_at="2026-05-29T08:00:00+00:00"),
        ]
        res = pr.match_receipts_to_payouts([receipt], payouts, CANONICAL)
        self.assertEqual(len(res.matched), 1)
        self.assertEqual(res.matched[0].payout.id, 11)   # 08:00 beats 10:00

    def test_deterministic_regardless_of_input_order(self):
        receipts = [_r(60.0, rid=1), _r(120.0, rid=2)]
        payouts = [_p("PAY TO X", 60.0, pid=10), _p("PAY TO Y", 120.0, pid=11)]
        forward = pr.match_receipts_to_payouts(receipts, payouts, CANONICAL)
        reverse = pr.match_receipts_to_payouts(
            list(reversed(receipts)), list(reversed(payouts)), CANONICAL
        )
        pairs = lambda res: {(m.receipt.id, m.payout.id) for m in res.matched}  # noqa: E731
        self.assertEqual(pairs(forward), pairs(reverse))
        self.assertEqual(pairs(forward), {(1, 10), (2, 11)})


class TypeClassification(unittest.TestCase):
    def _result(self):
        receipts = [
            _r(60.0, canonical="BABAS", rid=1),   # Type A (matched)
            _r(25.0, canonical="SAIDA", rid=2),   # Type C (account only)
        ]
        payouts = [
            _p("PAY TO BABAS", 60.0, pid=10),       # Type A
            _p("PAY TO EVEREST", 80.0, pid=11),     # Type B (cash no receipt)
            _p("PAY [LP] KARUNGARAJ", 300.0, pid=12),  # Type D
            _p("PAY TO GAS", 250.0, pid=13),        # Type E
            _p("NON_PAY TO KACHANG", 90.0, pid=14),  # Type F (recorded, not paid)
        ]
        return pr.match_receipts_to_payouts(receipts, payouts, CANONICAL)

    def test_classifies_all_types(self):
        res = self._result()
        self.assertEqual(len(res.matched), 1)               # A
        self.assertEqual(len(res.cash_no_receipt), 1)       # B
        self.assertEqual(len(res.account_only_receipts), 1)  # C
        self.assertEqual(len(res.excluded_staff), 1)        # D
        self.assertEqual(len(res.excluded_utility), 1)      # E
        self.assertEqual(len(res.excluded_non_pay), 1)      # F

    def test_match_log_has_all_type_codes(self):
        rows = pr.build_match_log(self._result())
        types = {r["match_type"] for r in rows}
        self.assertEqual(types, {
            "A_matched", "B_cash_no_receipt", "C_account_only",
            "D_excluded_staff", "E_excluded_utility", "F_excluded_non_pay",
        })


class DamansaraUptownJune4Regression(unittest.TestCase):
    """The real D.U / 2026-06-04 lines that surfaced the bug: RM14,576 of
    "food" was 10 cash_no_receipt payouts dominated by 3 PINJAM staff loans and
    1 NON_PAY dairy line that should never have counted. After the classifier
    fix total_food_purchases must drop to RM6,583.50."""

    def _run(self):
        # Only the one real D.U receipt that day (FOOK LEONG, no canonical).
        receipts = [_r(158.0, merchant="FOOK LEONG SEA PRODUCTS SDN BHD", rid=2707)]
        payouts = [
            _p("PAY TO BALAJI BILL", 5572.00, pid=984),
            _p("NON_PAY TO MEWAH DAIRIES OKI SUSU", 4342.50, pid=983),
            _p("PINJAM TO B ALAM pai cash", 3000.00, pid=985),
            _p("PINJAM TO NATARAJAN pai cash", 500.00, pid=986),
            _p("PAY TO AYAM BESTARI", 492.00, pid=987),
            _p("PAY TO EXTRA JUSS", 185.00, pid=989),
            _p("PAY TO GAS", 175.00, pid=988),
            _p("PINJAM TO PARANJOTHI", 150.00, pid=995),
            _p("PAY [SALARY] TO KALEEL", 120.00, pid=994),
            _p("PAY TO MINI MART", 102.00, pid=991),
            _p("PAY TO AIS", 57.50, pid=990),
            _p("PAY [LEAVE PAY] TO SITHIK ALI", 25.00, pid=992),
            _p("PAY [LEAVE PAY] TO KALEEL", 20.00, pid=993),
            _p("PAY TO KELAPA", 17.00, pid=996),
        ]
        return pr.match_receipts_to_payouts(receipts, payouts, CANONICAL)

    def test_pinjam_and_non_pay_now_excluded(self):
        res = self._run()
        non_pay_amounts = {p.amount for p in res.excluded_non_pay}
        staff_amounts = {p.amount for p in res.excluded_staff}
        self.assertEqual(non_pay_amounts, {4342.50})            # MEWAH dairy
        self.assertIn(3000.00, staff_amounts)                   # PINJAM B ALAM
        self.assertIn(500.00, staff_amounts)                    # PINJAM NATARAJAN
        self.assertIn(150.00, staff_amounts)                    # PINJAM PARANJOTHI
        self.assertEqual({p.amount for p in res.excluded_utility}, {175.00})  # GAS
        # BALAJI BILL is a genuine supplier bill — still food (cash_no_receipt).
        self.assertIn(5572.00, {p.amount for p in res.cash_no_receipt})

    def test_total_food_purchases_drops_to_expected(self):
        res = self._run()
        row = pr.summarize(res, "D.U", "2026-06-04", sales_total=None)
        self.assertEqual(row["total_food_purchases"], 6583.50)


class FoodCostPercent(unittest.TestCase):
    def test_normal_case(self):
        # RM2,000 purchases / RM8,000 sales = 25%.
        self.assertEqual(pr.compute_food_cost_percent(2000.0, 0.0, 0.0, 8000.0), 25.0)

    def test_counts_cash_no_receipt_and_account(self):
        # matched 1000 + account 500 + cash-no-receipt 500 = 2000 / 8000 = 25%.
        self.assertEqual(pr.compute_food_cost_percent(1000.0, 500.0, 500.0, 8000.0), 25.0)

    def test_zero_sales_returns_none(self):
        self.assertIsNone(pr.compute_food_cost_percent(500.0, 0.0, 0.0, 0.0))
        self.assertIsNone(pr.compute_food_cost_percent(500.0, 0.0, 0.0, None))

    def test_summary_excludes_staff_and_utility(self):
        receipts = [_r(60.0, canonical="BABAS", rid=1)]
        payouts = [
            _p("PAY TO BABAS", 60.0, pid=10),
            _p("PAY [LP] STAFF", 300.0, pid=11),  # excluded
            _p("PAY TO GAS", 250.0, pid=12),      # excluded
        ]
        res = pr.match_receipts_to_payouts(receipts, payouts, CANONICAL)
        row = pr.summarize(res, "Vista", "2026-05-29", sales_total=600.0)
        # Only the matched RM60 counts toward food purchases.
        self.assertEqual(row["total_food_purchases"], 60.0)
        self.assertEqual(row["food_cost_percent"], 10.0)
        self.assertEqual(row["matched_count"], 1)
        self.assertEqual(row["total_pos_payouts"], 3)


if __name__ == "__main__":
    unittest.main()
