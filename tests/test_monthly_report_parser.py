"""Tests for ``monthly_report_parser``.

The hazards these pin are not hypothetical — every one of them is present in
``tests/fixtures/sales/S-Damansara 25May2026.TXT``, which is genuine POS output
from the same generator as the monthly report. Where a test asserts on that
file it is checking behaviour against real data, not a mock of it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monthly_report_parser import (  # noqa: E402
    parse_header,
    parse_monthly_report,
    split_trailing_numbers,
    _parse_daily_sales_rows,
    _parse_itemwise_rows,
    _parse_payout_rows,
    _parse_staff_advance_rows,
    _parse_staff_sales_rows,
)
from sales_parser import read_shift_close_file  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REAL_POS_FILE = os.path.join(FIXTURES, "sales", "S-Damansara 25May2026.TXT")
SYNTHETIC_MONTHLY = os.path.join(
    FIXTURES, "monthly_synthetic", "M-Damansara Jul2026-SYNTHETIC.TXT"
)


class SplitTrailingNumbers(unittest.TestCase):

    def test_peels_one_number(self):
        self.assertEqual(split_trailing_numbers("PAY TO AIS      50.00", 1),
                         ("PAY TO AIS", [50.0]))

    def test_peels_two_numbers_in_printed_order(self):
        self.assertEqual(split_trailing_numbers("Ayam Goreng   13    65.00", 2),
                         ("Ayam Goreng", [13.0, 65.0]))

    def test_leading_space_is_preserved(self):
        # Significant: menu hygiene finds duplicate SKUs by exactly this.
        name, _ = split_trailing_numbers(" Nasi Goreng Daging    1    9.50", 2)
        self.assertEqual(name, " Nasi Goreng Daging")

    def test_internal_double_space_is_preserved(self):
        name, _ = split_trailing_numbers("Barli  Panas    1    2.30", 2)
        self.assertEqual(name, "Barli  Panas")

    def test_leading_dot_amount(self):
        # The POS prints zero and sub-ringgit amounts as ".00"/".60".
        self.assertEqual(split_trailing_numbers("Air Panas   2    .60", 2),
                         ("Air Panas", [2.0, 0.6]))

    def test_thousands_separator(self):
        self.assertEqual(split_trailing_numbers("PAY TO BALAJI   1,234.50", 1),
                         ("PAY TO BALAJI", [1234.5]))

    def test_negative_amount(self):
        self.assertEqual(split_trailing_numbers("DINE IN   -1,037.00", 1),
                         ("DINE IN", [-1037.0]))

    def test_name_starting_with_a_digit(self):
        self.assertEqual(split_trailing_numbers("3 Layer ( T )   1   4.50", 2),
                         ("3 Layer ( T )", [1.0, 4.5]))
        self.assertEqual(split_trailing_numbers("100 Plus   1   2.60", 2),
                         ("100 Plus", [1.0, 2.6]))

    def test_too_few_numbers_returns_none(self):
        self.assertIsNone(split_trailing_numbers("Ayam Goreng   13", 2))
        self.assertIsNone(split_trailing_numbers("JUST TEXT", 1))

    def test_label_only_row_returns_none(self):
        self.assertIsNone(split_trailing_numbers("   1   2", 2))

    def test_empty_and_non_string(self):
        for value in (None, "", "   ", 42):
            self.assertIsNone(split_trailing_numbers(value, 1), value)


class ParseHeader(unittest.TestCase):

    def test_standard_header(self):
        h = parse_header("MONTHLY REPORT-Damansara ON Jul 2026")
        self.assertEqual(h["outlet_code"], "DAMANSARA")
        self.assertEqual(h["period"], "2026-07")

    def test_full_month_name_and_spacing(self):
        self.assertEqual(parse_header("MONTHLY REPORT - ST KHU  ON  January 2026")["period"],
                         "2026-01")
        self.assertEqual(parse_header("MONTHLY REPORT-ST KHU ON January 2026")["outlet_code"],
                         "ST KHU")

    def test_missing_header_yields_nulls(self):
        for content in ("", None, "S-Damansara ON 25/May/2026", 42):
            h = parse_header(content)
            self.assertIsNone(h["period"], content)
            self.assertIsNone(h["outlet_code"], content)

    def test_unknown_month_name_has_no_period(self):
        self.assertIsNone(parse_header("MONTHLY REPORT-X ON Smarch 2026")["period"])


class RowParsers(unittest.TestCase):

    def test_payout_row_keeps_bracket_tag(self):
        rows = _parse_payout_rows(["14419  PAY [SALARY] TO KALEEL     120.00"])
        self.assertEqual(rows[0], {"trno": "14419",
                                   "description": "PAY [SALARY] TO KALEEL",
                                   "amount": 120.0})

    def test_payout_row_without_trno(self):
        rows = _parse_payout_rows(["PAY TO BALAJI    900.00"])
        self.assertIsNone(rows[0]["trno"])
        self.assertEqual(rows[0]["description"], "PAY TO BALAJI")

    def test_unparseable_payout_row_is_skipped_not_fatal(self):
        rows = _parse_payout_rows(["garbage with no amount", "PAY TO AIS  50.00"])
        self.assertEqual(len(rows), 1)

    def test_staff_sales_drops_shift_number(self):
        rows = _parse_staff_sales_rows([" 2384    RAFAYUDEEN              867.70"])
        self.assertEqual(rows[0], {"staff_name": "RAFAYUDEEN", "amount": 867.7})

    def test_staff_sales_skips_machine_rows(self):
        # Machine-sales rows share the block in some POS versions; a machine is
        # not a staff member called "1153".
        self.assertEqual(_parse_staff_sales_rows([" 2384        1153      1     1078.00"]), [])

    def test_staff_advance_two_columns(self):
        rows = _parse_staff_advance_rows(["AHMAD          200.00      1800.00"])
        self.assertEqual(rows[0], {"staff_name": "AHMAD", "advance": 200.0, "netsal": 1800.0})

    def test_staff_advance_single_column_leaves_netsal_null(self):
        rows = _parse_staff_advance_rows(["AHMAD          200.00"])
        self.assertEqual(rows[0]["advance"], 200.0)
        self.assertIsNone(rows[0]["netsal"])

    def test_daily_sales_full_row(self):
        rows = _parse_daily_sales_rows(["01/07/2026      4,120.50     820.00      .00"])
        self.assertEqual(rows[0], {"date_cell": "01/07/2026", "sale": 4120.5,
                                   "payout": 820.0, "tax": 0.0})

    def test_daily_sales_short_row_leaves_later_columns_null(self):
        rows = _parse_daily_sales_rows(["01      4,120.50"])
        self.assertEqual(rows[0]["sale"], 4120.5)
        self.assertIsNone(rows[0]["payout"])

    def test_daily_sales_ignores_non_date_rows(self):
        self.assertEqual(_parse_daily_sales_rows(["TOTAL SALES   4,120.50"]), [])

    def test_itemwise_row(self):
        rows = _parse_itemwise_rows(["Ayam Goreng               13        65.00"])
        self.assertEqual(rows[0], {"item_name": "Ayam Goreng", "qty": 13.0, "amount": 65.0})


class ParseAgainstRealPosOutput(unittest.TestCase):
    """The parser run over genuine POS output from the same report generator."""

    @classmethod
    def setUpClass(cls):
        body = read_shift_close_file(REAL_POS_FILE)
        cls.parsed = parse_monthly_report("MONTHLY REPORT-Damansara ON Jul 2026\n" + body)

    def test_header_is_read(self):
        self.assertEqual(self.parsed["outlet_code"], "DAMANSARA")
        self.assertEqual(self.parsed["period"], "2026-07")

    def test_sales_sections_are_found(self):
        for section in ("itemwise", "staff_sales"):
            self.assertIn(section, self.parsed["sections_found"], section)

    def test_shift_money_blocks_are_NOT_claimed_by_the_monthly_anchors(self):
        # The S- file is a SHIFT report: it closes its money blocks with
        # "TOTAL PURCHASE" / "TOTAL PINJAM", which are deliberately NOT monthly
        # closing anchors. The monthly parser must claim nothing here rather
        # than half-parse a foreign format.
        self.assertEqual(self.parsed["closing_totals"], {})
        self.assertEqual(self.parsed["payouts"], [])
        self.assertEqual(self.parsed["pinjam"], [])

    def test_shift_summary_labels_still_read(self):
        totals = self.parsed["totals"]
        self.assertAlmostEqual(totals["total_purchase"], 974.0, places=2)
        self.assertAlmostEqual(totals["total_pinjam"], 50.0, places=2)
        self.assertAlmostEqual(totals["total_payouts"], 1024.0, places=2)

    def test_every_itemwise_row_was_parsed(self):
        self.assertEqual(len(self.parsed["itemwise"]), 143)
        self.assertTrue(all(r["qty"] is not None for r in self.parsed["itemwise"]))

    def test_itemwise_sum_matches_the_printed_total(self):
        # The file prints TOTAL :3050.40 under the itemwise block — proof the
        # row parser neither dropped nor invented a line.
        self.assertAlmostEqual(
            sum(r["amount"] for r in self.parsed["itemwise"]), 3050.40, places=2
        )

    def test_whitespace_hazards_survive_into_the_rows(self):
        names = [r["item_name"] for r in self.parsed["itemwise"]]
        self.assertIn(" Nasi Goreng Daging", names)   # leading space
        self.assertIn(" Tambah_nasi", names)          # leading space + underscore
        self.assertIn("Barli  Panas", names)          # double space

    def test_sub_ringgit_amount_parsed_from_leading_dot(self):
        by_name = {r["item_name"]: r for r in self.parsed["itemwise"]}
        self.assertAlmostEqual(by_name["Air Panas"]["amount"], 0.60, places=2)

    def test_staff_sales_parsed(self):
        names = {r["staff_name"] for r in self.parsed["staff_sales"]}
        self.assertIn("RAFAYUDEEN", names)
        self.assertNotIn("1153", names)


class Robustness(unittest.TestCase):

    def test_empty_and_junk_input_yields_empty_sections(self):
        for content in ("", "   ", None, 42):
            parsed = parse_monthly_report(content)
            self.assertEqual(parsed["itemwise"], [])
            self.assertEqual(parsed["payouts"], [])
            self.assertIsNone(parsed["period"])

    def test_missing_section_is_empty_not_an_error(self):
        parsed = parse_monthly_report(
            "MONTHLY REPORT-X ON Jul 2026\nTOTAL SALES : 100.00\n"
        )
        self.assertEqual(parsed["itemwise"], [])
        self.assertNotIn("itemwise", parsed["sections_found"])
        self.assertAlmostEqual(parsed["totals"]["total_sales"], 100.0, places=2)

    def test_crlf_and_utf16_style_content_normalises(self):
        parsed = parse_monthly_report(
            "MONTHLY REPORT-X ON Jul 2026\r\nTOTAL SALES : 100.00\r\n"
        )
        self.assertAlmostEqual(parsed["totals"]["total_sales"], 100.0, places=2)

    def test_first_occurrence_of_a_label_wins(self):
        # The monthly file repeats labels per shift below the summary; the
        # summary value is the month's.
        parsed = parse_monthly_report(
            "MONTHLY REPORT-X ON Jul 2026\nTOTAL SALES : 148,586.80\n"
            "SHIFT 1\nTOTAL SALES : 3,721.50\n"
        )
        self.assertAlmostEqual(parsed["totals"]["total_sales"], 148586.80, places=2)


if __name__ == "__main__":
    unittest.main()


class ClosingTotalAnchoring(unittest.TestCase):
    """The money blocks are bounded by their CLOSING totals, not their headers.

    Driven by a SYNTHETIC file that reproduces the real report's structure —
    including the `DESCRIPTION AMOUNT` header printed twice, which is exactly
    why header anchoring cannot work. It cannot prove the real file looks like
    this; only the acceptance test can.
    """

    @classmethod
    def setUpClass(cls):
        cls.parsed = parse_monthly_report(read_shift_close_file(SYNTHETIC_MONTHLY))

    def test_all_four_closing_totals_are_read(self):
        self.assertEqual(self.parsed["closing_totals"], {
            "payouts": 45121.10, "cheque": 0.0,
            "salary_block": 23954.30, "staff_meals": 0.0,
        })

    def test_the_twice_printed_header_does_not_split_a_block(self):
        # If DESCRIPTION were an anchor, the supplier rows would land in the
        # payouts block and COGS would be overstated by RM23,954.30.
        payees = {r["description"] for r in self.parsed["salary_block"]}
        self.assertIn("BALAJI", payees)
        self.assertNotIn("BALAJI", {r["description"] for r in self.parsed["payouts"]})

    def test_supplier_block_rows_sum_to_its_closing_total(self):
        self.assertAlmostEqual(
            sum(r["amount"] for r in self.parsed["salary_block"]), 23954.30, places=2
        )

    def test_advance_rows_come_out_of_the_payouts_block(self):
        # PINJAM has no section: ADVANCE TO rows sit inside the payouts block.
        self.assertEqual([r["description"] for r in self.parsed["pinjam"]],
                         ["ADVANCE TO YUSUF", "ADVANCE TO AHMAD"])
        self.assertNotIn("ADVANCE TO YUSUF",
                         {r["description"] for r in self.parsed["payouts"]})

    def test_advances_are_not_double_counted(self):
        combined = sum(r["amount"] for r in self.parsed["payouts"] + self.parsed["pinjam"])
        self.assertAlmostEqual(combined, self.parsed["closing_totals"]["payouts"], places=2)

    def test_paikkasu_rows_are_kept_with_their_inline_tag(self):
        tagged = [r["description"] for r in self.parsed["payouts"]
                  if "_PAIKKASU_" in r["description"]]
        self.assertEqual(len(tagged), 3)
        self.assertTrue(any(d.startswith("NON PAY TO") for d in tagged))
        self.assertTrue(any(d.startswith("PAY TO") for d in tagged))

    def test_summary_labels_are_read(self):
        totals = self.parsed["totals"]
        self.assertAlmostEqual(totals["payout_purchase"], 39743.10, places=2)
        self.assertAlmostEqual(totals["payout_pinjam"], 5378.00, places=2)
        self.assertAlmostEqual(totals["payout_expense"], 0.0, places=2)
        self.assertAlmostEqual(totals["total_salary"], 23954.30, places=2)

    def test_the_report_header_line_is_not_a_payout_row(self):
        # "MONTHLY REPORT-Damansara ON Jul 2026" ends in a number; without the
        # 2-decimal rule it parses as a RM2,026.00 payout.
        self.assertNotIn(2026.0, [r["amount"] for r in self.parsed["payouts"]])

    def test_summary_lines_are_not_payout_rows(self):
        descriptions = {r["description"] for r in self.parsed["payouts"]}
        self.assertNotIn("PAYOUT PURCHASE", descriptions)
        self.assertNotIn("TOTAL SALES", descriptions)


class PaikkasuAndAdvanceHelpers(unittest.TestCase):

    def test_tag_detection_on_both_row_forms(self):
        from monthly_report_parser import has_paikkasu_tag

        self.assertTrue(has_paikkasu_tag("NON PAY TO CAMELLIA TEA _PAIKKASU_"))
        self.assertTrue(has_paikkasu_tag("PAY TO BABAS MASALA _PAIKKASU_"))
        self.assertFalse(has_paikkasu_tag("PAY TO AYAM BESTARI"))
        self.assertFalse(has_paikkasu_tag(None))

    def test_tag_is_stripped_for_matching(self):
        from monthly_report_parser import strip_paikkasu_tag

        self.assertEqual(strip_paikkasu_tag("NON PAY TO CAMELLIA TEA _PAIKKASU_"),
                         "NON PAY TO CAMELLIA TEA")

    def test_advance_detection(self):
        from monthly_report_parser import is_advance_row

        self.assertTrue(is_advance_row("ADVANCE TO YUSUF"))
        self.assertFalse(is_advance_row("PAY TO AYAM BESTARI"))
        self.assertFalse(is_advance_row(None))


class IntegrityGuard(unittest.TestCase):
    """The silent-zero guard: a block that parses short must never pass."""

    @classmethod
    def setUpClass(cls):
        cls.content = read_shift_close_file(SYNTHETIC_MONTHLY)

    def test_a_good_month_reconciles(self):
        from monthly_report_parser import check_report_integrity

        self.assertEqual(check_report_integrity(parse_monthly_report(self.content)), [])

    def test_zero_rows_with_a_non_zero_closing_total_fails(self):
        from monthly_report_parser import (
            MonthlyReportIntegrityError, verify_report_integrity,
        )

        parsed = parse_monthly_report(self.content)
        parsed["salary_block"] = []          # simulate an unrecognised block
        with self.assertRaises(MonthlyReportIntegrityError) as ctx:
            verify_report_integrity(parsed)
        self.assertIn("NO rows were parsed", str(ctx.exception))

    def test_a_partially_parsed_block_fails_too(self):
        from monthly_report_parser import check_report_integrity

        parsed = parse_monthly_report(self.content)
        parsed["salary_block"] = parsed["salary_block"][:2]
        failures = check_report_integrity(parsed)
        self.assertTrue(any("rows sum to" in f for f in failures), failures)

    def test_supplier_sum_is_checked_against_the_summary_control(self):
        from monthly_report_parser import check_report_integrity

        parsed = parse_monthly_report(self.content)
        parsed["totals"]["total_salary"] = 99999.99
        failures = check_report_integrity(parsed)
        self.assertTrue(any("TOTAL SALARY" in f for f in failures), failures)

    def test_advance_sum_is_checked_against_payout_pinjam(self):
        from monthly_report_parser import check_report_integrity

        parsed = parse_monthly_report(self.content)
        parsed["totals"]["payout_pinjam"] = 1.00
        failures = check_report_integrity(parsed)
        self.assertTrue(any("PAYOUT PINJAM" in f for f in failures), failures)

    def test_an_absent_control_is_reported_not_silently_passed(self):
        from monthly_report_parser import check_report_integrity, missing_controls

        parsed = parse_monthly_report(self.content)
        parsed["totals"].pop("total_salary")
        parsed["totals"].pop("payout_pinjam")
        self.assertEqual(check_report_integrity(parsed), [])
        self.assertEqual(len(missing_controls(parsed)), 2)
