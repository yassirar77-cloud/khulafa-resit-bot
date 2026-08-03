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

    def test_all_expected_sections_are_found(self):
        for section in ("itemwise", "payouts", "pinjam", "staff_sales"):
            self.assertIn(section, self.parsed["sections_found"], section)

    def test_payouts_parsed_with_tags_intact(self):
        descriptions = [r["description"] for r in self.parsed["payouts"]]
        self.assertIn("PAY TO AYAM BESTARI", descriptions)
        self.assertIn("PAY [SALARY] TO KALEEL", descriptions)
        self.assertIn("PAY [LEAVE PAY] TO ABU", descriptions)

    def test_pinjam_block_is_separate_from_purchases(self):
        self.assertEqual([r["description"] for r in self.parsed["pinjam"]],
                         ["ADVANCE TO YUSUF"])

    def test_payout_totals_reconcile_with_the_detail(self):
        # The real file prints TOTAL PURCHASE 974 + TOTAL PINJAM 50 = PAYOUT 1024.
        totals = self.parsed["totals"]
        self.assertAlmostEqual(totals["total_purchase"], 974.0, places=2)
        self.assertAlmostEqual(totals["total_pinjam"], 50.0, places=2)
        self.assertAlmostEqual(
            totals["total_purchase"] + totals["total_pinjam"],
            totals["total_payouts"], places=2,
        )
        self.assertAlmostEqual(
            sum(r["amount"] for r in self.parsed["payouts"]),
            totals["total_purchase"], places=2,
        )

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
