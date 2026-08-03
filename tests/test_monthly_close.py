"""Tests for ``monthly_close`` — the two mandatory reclassifications, the P&L,
and the cash reconciliation.

The reclassifications are the reason this module exists, so they get the most
tests: booking a supplier as payroll moves real money between COGS and wages,
and treating a recoverable advance as an expense understates profit.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monthly_close import (  # noqa: E402
    CATEGORY_ADVANCE,
    CATEGORY_COGS,
    CATEGORY_FLAGGED,
    CATEGORY_OTHER,
    CATEGORY_WAGES,
    bracket_tag,
    build_cash_reconciliation,
    build_pl,
    check_pinjam_addback,
    classify_payout,
    extract_payee,
    format_monthly_digest,
    format_pl_lines,
    reclassify_salary_block,
)

STAFF = {"YUSUF", "KALEEL", "ABU", "HARI", "AHMAD"}

# The five suppliers the spec names as living under the "TOTAL PAY SALARY"
# heading on the real report.
SALARY_BLOCK = [
    {"description": "BALAJI", "amount": 12000.0},
    {"description": "JASMINE RICE", "amount": 8000.0},
    {"description": "JUTA RIA TELUR", "amount": 4000.0},
    {"description": "MEWAH DAIRIES", "amount": 3000.0},
    {"description": "REZA PLASTIC", "amount": 1000.0},
]


class PayeeExtraction(unittest.TestCase):

    def test_pay_to(self):
        self.assertEqual(extract_payee("PAY TO AYAM BESTARI"), "AYAM BESTARI")

    def test_bracket_tag_is_stripped_from_the_payee(self):
        self.assertEqual(extract_payee("PAY [SALARY] TO KALEEL"), "KALEEL")
        self.assertEqual(bracket_tag("PAY [SALARY] TO KALEEL"), "SALARY")
        self.assertEqual(bracket_tag("PAY [LEAVE PAY] TO ABU"), "LEAVE PAY")

    def test_advance_to(self):
        self.assertEqual(extract_payee("ADVANCE TO YUSUF"), "YUSUF")

    def test_bare_name_is_returned_as_is(self):
        self.assertEqual(extract_payee("BALAJI"), "BALAJI")

    def test_non_string(self):
        self.assertEqual(extract_payee(None), "")
        self.assertIsNone(bracket_tag(None))


class ReclassificationA(unittest.TestCase):
    """The 'TOTAL PAY SALARY' block is suppliers, not payroll."""

    def test_every_named_supplier_becomes_cogs(self):
        result = reclassify_salary_block(SALARY_BLOCK, STAFF)
        self.assertEqual(result["supplier_cogs"], 28000.0)
        self.assertEqual(result["staff_wages"], 0.0)
        self.assertEqual(result["flagged_amount"], 0.0)

    def test_never_payroll_even_though_the_heading_says_salary(self):
        for row in SALARY_BLOCK:
            verdict = classify_payout(row["description"], block="salary_block",
                                      staff_names=STAFF)
            self.assertEqual(verdict["category"], CATEGORY_COGS, row["description"])
            self.assertEqual(verdict["bucket"], "supplier_bank")

    def test_a_real_staff_member_in_the_block_is_wages(self):
        result = reclassify_salary_block(
            SALARY_BLOCK + [{"description": "AHMAD", "amount": 1500.0}], STAFF
        )
        self.assertEqual(result["staff_wages"], 1500.0)
        self.assertEqual(result["supplier_cogs"], 28000.0)

    def test_a_name_matching_neither_is_flagged_not_guessed(self):
        result = reclassify_salary_block(
            SALARY_BLOCK + [{"description": "ZZZ TRADING", "amount": 777.0}], STAFF
        )
        self.assertEqual(result["flagged_amount"], 777.0)
        self.assertEqual([r["payee"] for r in result["flagged_rows"]], ["ZZZ TRADING"])
        # And its money is in NEITHER bucket.
        self.assertEqual(result["supplier_cogs"], 28000.0)
        self.assertEqual(result["staff_wages"], 0.0)

    def test_flagged_money_never_reaches_cogs_or_wages_in_the_pl(self):
        pl = build_pl(
            {"total_sales": 100000.0},
            [], [], SALARY_BLOCK + [{"description": "ZZZ TRADING", "amount": 777.0}],
            STAFF, None,
        )
        self.assertEqual(pl["cogs"], 28000.0)
        self.assertEqual(pl["till_wages"], 0.0)
        self.assertTrue(any("neither a supplier nor a staff name" in f for f in pl["flags"]))

    def test_ambiguous_short_token_is_flagged_rather_than_booked(self):
        # '99' is in the supplier master (99 Speedmart) but also matches a
        # staff nickname; a bidirectional substring hit alone must not move money.
        result = reclassify_salary_block([{"description": "MAT 99", "amount": 500.0}], set())
        self.assertEqual(result["supplier_cogs"], 0.0)
        self.assertEqual(result["flagged_amount"], 500.0)

    def test_empty_block(self):
        result = reclassify_salary_block([], STAFF)
        self.assertEqual(result["supplier_cogs"], 0.0)
        self.assertEqual(result["flagged_rows"], [])


class ReclassificationB(unittest.TestCase):
    """PAYOUT PINJAM is a recoverable advance, added back."""

    def test_advance_rows_are_categorised_as_advance(self):
        verdict = classify_payout("ADVANCE TO YUSUF", block="pinjam", staff_names=STAFF)
        self.assertEqual(verdict["category"], CATEGORY_ADVANCE)

    def test_addback_matches_the_printed_total(self):
        result = check_pinjam_addback(
            [{"amount": 5000.0}, {"amount": 378.0}], 5378.0
        )
        self.assertEqual(result["addback"], 5378.0)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["flag"])

    def test_mismatch_is_flagged_with_both_numbers(self):
        result = check_pinjam_addback([{"amount": 5000.0}], 5378.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["delta"], -378.0)
        self.assertIn("5000.00", result["flag"])
        self.assertIn("5378.00", result["flag"])

    def test_one_sen_is_within_tolerance(self):
        self.assertTrue(check_pinjam_addback([{"amount": 5378.01}], 5378.0)["ok"])

    def test_two_sen_is_not(self):
        self.assertFalse(check_pinjam_addback([{"amount": 5378.02}], 5378.0)["ok"])

    def test_no_printed_total_is_flagged_not_silently_accepted(self):
        result = check_pinjam_addback([{"amount": 100.0}], None)
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["flag"])

    def test_addback_appears_in_the_pl(self):
        pl = build_pl({"total_sales": 100000.0}, [],
                      [{"amount": 5378.0}], [], STAFF, None)
        self.assertEqual(pl["pinjam_addback"], 5378.0)

    def test_advances_are_not_counted_as_expense(self):
        # Contribution must be identical with and without the advance rows.
        base = build_pl({"total_sales": 100000.0}, [], [], [], STAFF, None)
        with_advance = build_pl({"total_sales": 100000.0}, [],
                                [{"amount": 5378.0}], [], STAFF, None)
        self.assertEqual(base["contribution"], with_advance["contribution"])


class TillPayoutClassification(unittest.TestCase):

    def test_supplier_purchase_is_cogs(self):
        self.assertEqual(
            classify_payout("PAY TO AYAM BESTARI", block="purchase")["category"],
            CATEGORY_COGS,
        )

    def test_salary_and_leave_pay_tags_are_wages(self):
        for description in ("PAY [SALARY] TO KALEEL", "PAY [LP] TO ABU",
                            "PAY [LEAVE PAY] TO HARI"):
            self.assertEqual(
                classify_payout(description, block="purchase")["category"],
                CATEGORY_WAGES, description,
            )

    def test_other_expense_keywords(self):
        cases = {
            "PAY TO GAS": "gas", "PAY TO HARDWARE SHOP": "hardware",
            "PAY TNB BILL": "bills", "PAY PEST CONTROL": "pest_control",
            "PAY KLINIK": "medical", "PAY LALAMOVE": "delivery",
            "PAY SHOPEE": "shopee", "DERMA MASJID": "donation",
            "CUSTOMER REFUND": "customer_refund",
        }
        for description, bucket in cases.items():
            verdict = classify_payout(description, block="purchase")
            self.assertEqual(verdict["category"], CATEGORY_OTHER, description)
            self.assertEqual(verdict["bucket"], bucket, description)

    def test_unrecognised_purchase_row_stays_cogs_but_is_marked(self):
        verdict = classify_payout("PAY TO SOMETHING NEW", block="purchase")
        self.assertEqual(verdict["category"], CATEGORY_COGS)
        self.assertEqual(verdict["bucket"], "till_purchase_unmatched")

    def test_salary_block_never_uses_the_till_defaults(self):
        # The same unrecognised name is FLAGGED in the salary block, because
        # that block's heading is the one known to lie.
        verdict = classify_payout("SOMETHING NEW", block="salary_block")
        self.assertEqual(verdict["category"], CATEGORY_FLAGGED)


class ProfitAndLoss(unittest.TestCase):

    def _pl(self, **kwargs):
        totals = {"total_sales": 148586.80}
        totals.update(kwargs.pop("totals", {}))
        return build_pl(
            totals,
            kwargs.pop("payouts", []),
            kwargs.pop("pinjam", []),
            kwargs.pop("salary", SALARY_BLOCK),
            kwargs.pop("staff", STAFF),
            kwargs.pop("config", None),
        )

    def test_cogs_is_the_sum_of_its_three_sources(self):
        pl = self._pl(
            payouts=[{"description": "PAY TO AYAM BESTARI", "amount": 20000.0}],
            totals={"total_paikkasu": 7882.30},
        )
        self.assertEqual(pl["cogs_breakdown"]["till_purchases"], 20000.0)
        self.assertEqual(pl["cogs_breakdown"]["paikkasu"], 7882.30)
        self.assertEqual(pl["cogs_breakdown"]["supplier_bank_block"], 28000.0)
        self.assertEqual(pl["cogs"], 55882.30)
        self.assertEqual(pl["cogs_pct"], 37.6)

    def test_gross_profit_and_contribution(self):
        pl = self._pl(
            payouts=[{"description": "PAY TO AYAM BESTARI", "amount": 20000.0},
                     {"description": "PAY [SALARY] TO KALEEL", "amount": 6000.0},
                     {"description": "PAY TO GAS", "amount": 1815.10}],
            totals={"total_paikkasu": 7882.30},
        )
        self.assertEqual(pl["gross_profit"], 92704.50)
        self.assertEqual(pl["till_wages"], 6000.0)
        self.assertEqual(pl["other_till_expense"], 1815.10)
        self.assertEqual(pl["contribution"], 84889.40)

    def test_paikkasu_already_in_the_rows_is_not_double_counted(self):
        pl = self._pl(
            payouts=[{"description": "PAY PAIKKASU", "amount": 7882.30}],
            totals={"total_paikkasu": 7882.30},
        )
        self.assertEqual(pl["cogs_breakdown"]["paikkasu"], 0.0)
        self.assertEqual(pl["cogs_breakdown"]["till_purchases"], 7882.30)

    def test_without_config_it_is_contribution_and_never_net_profit(self):
        pl = self._pl()
        self.assertTrue(pl["is_contribution_only"])
        self.assertIsNone(pl["net_profit"])
        self.assertEqual(pl["label"], "CONTRIBUTION (pre-rent/utilities/central payroll)")
        rendered = " ".join(str(label) for label, _ in format_pl_lines(pl))
        self.assertNotIn("NET PROFIT", rendered)
        self.assertTrue(any("CONTRIBUTION, not net profit" in f for f in pl["flags"]))

    def test_missing_config_lines_are_never_estimated(self):
        pl = self._pl()
        self.assertIsNone(pl["fixed_costs"])
        self.assertIsNone(pl["outlet_config"])

    def test_with_config_it_reaches_net_profit(self):
        pl = self._pl(config={"rent": 10000.0, "tnb": 3000.0, "air_selangor": 500.0,
                              "unifi": 200.0, "central_payroll": 20000.0,
                              "kwsp_perkeso": 2500.0})
        self.assertFalse(pl["is_contribution_only"])
        self.assertEqual(pl["fixed_costs"], 36200.0)
        self.assertEqual(pl["net_profit"], round(pl["contribution"] - 36200.0, 2))
        rendered = " ".join(str(label) for label, _ in format_pl_lines(pl))
        self.assertIn("NET PROFIT", rendered)

    def test_partial_config_still_counts_only_what_was_given(self):
        pl = self._pl(config={"rent": 10000.0})
        self.assertEqual(pl["fixed_costs"], 10000.0)
        self.assertFalse(pl["is_contribution_only"])

    def test_zero_sales_is_flagged_and_does_not_divide(self):
        pl = build_pl({"total_sales": 0}, [], [], [], STAFF, None)
        self.assertIsNone(pl["cogs_pct"])
        self.assertTrue(any("TOTAL SALES is zero" in f for f in pl["flags"]))

    def test_empty_inputs_do_not_raise(self):
        pl = build_pl({}, [], [], [], set(), None)
        self.assertEqual(pl["sales"], 0)
        self.assertEqual(pl["cogs"], 0)


class CashReconciliation(unittest.TestCase):

    BASE = {"total_sales": 148586.80, "total_bank_sales": 70000.0,
            "total_fp_sales": 8948.30, "total_payouts": 45121.10}

    def test_cash_sales_and_expected_bank_in(self):
        cash = build_cash_reconciliation(self.BASE)
        self.assertEqual(cash["cash_sales"], 69638.50)
        self.assertEqual(cash["expected_bank_in"], 24517.40)
        self.assertEqual(cash["flags"], [])

    def test_negative_expected_bank_in_is_flagged(self):
        cash = build_cash_reconciliation({**self.BASE, "total_payouts": 90000.0})
        self.assertLess(cash["expected_bank_in"], 0)
        self.assertTrue(any("NEGATIVE" in f for f in cash["flags"]))

    def test_negative_cash_sales_is_flagged(self):
        cash = build_cash_reconciliation({**self.BASE, "total_bank_sales": 200000.0})
        self.assertTrue(any("cash_sales is NEGATIVE" in f for f in cash["flags"]))

    def test_missing_channels_are_treated_as_zero_and_reported(self):
        cash = build_cash_reconciliation({"total_sales": 100.0})
        self.assertEqual(cash["cash_sales"], 100.0)
        self.assertIn("TOTAL BANK SALES", cash["missing"])
        self.assertTrue(any("treated as zero" in f for f in cash["flags"]))

    def test_missing_sales_cannot_reconcile(self):
        cash = build_cash_reconciliation({})
        self.assertIsNone(cash["expected_bank_in"])
        self.assertTrue(cash["flags"])


class Digest(unittest.TestCase):

    def _digest(self, config=None):
        totals = {"total_sales": 148586.80, "total_bank_sales": 70000.0,
                  "total_fp_sales": 8948.30, "total_payouts": 45121.10}
        pl = build_pl(totals, [], [{"amount": 5378.0}], SALARY_BLOCK, STAFF, config)
        cash = build_cash_reconciliation(totals)
        wastage = {"rows": [{"canonical_item": "ayam", "variance_pct": 32.0,
                             "verdict": "HIGH WASTAGE", "flagged_rows": 0,
                             "flagged_cost": 0.0}],
                   "unreliable_count": 1, "flagged_rows": 2, "flagged_cost": 90.0}
        hygiene = {"duplicate_count": 7, "dead_count": 131}
        return format_monthly_digest("D.U", "2026-07", pl, cash, wastage, hygiene)

    def test_leads_with_the_actionable_numbers(self):
        text = self._digest()
        self.assertIn("Sales:", text)
        self.assertIn("COGS:", text)
        self.assertIn("Expected bank-in:", text)

    def test_says_contribution_not_net_profit_without_config(self):
        text = self._digest()
        self.assertIn("CONTRIBUTION", text)
        self.assertIn("NOT net profit", text)

    def test_says_net_profit_when_config_is_present(self):
        text = self._digest(config={"rent": 1000.0})
        self.assertIn("NET PROFIT", text)

    def test_reports_top_variances_and_flag_counts(self):
        text = self._digest()
        self.assertIn("ayam", text)
        self.assertIn("+32.0%", text)
        self.assertIn("7 duplicate SKU group(s), 131 dead SKU(s)", text)
        self.assertIn("2 purchase line(s) not convertible", text)


if __name__ == "__main__":
    unittest.main()
