"""Acceptance test: the real Jul 2026 Damansara monthly report.

This is the test that decides whether the monthly close is correct. Every other
test in the suite pins a rule in isolation; this one runs the whole chain —
parse, reclassify, P&L, cash reconciliation, menu hygiene — over the genuine
file and checks the owner's known-good figures:

    sales 148,586.80 | COGS 55,882.30 (37.6%) | contribution 84,889.40
    cash_sales 69,638.50 | expected_bank_in 24,517.40
    pinjam addback 5,378.00 | duplicate SKUs 7 | dead SKUs 131

**The fixture is not in the repository.** The July report is production data and
was never committed. Until it is dropped in, every test here SKIPS with the path
it wants — it does not pass, and it does not quietly disappear. Drop the file at

    tests/fixtures/monthly/M-Damansara Jul2026.TXT

(any filename in that directory works; the newest match is used) and run

    python -m unittest tests.test_monthly_close_acceptance -v

Each expectation is asserted separately, so a run tells you exactly WHICH figure
disagrees rather than failing on the first one. A skipped acceptance test is a
statement that the engine is unverified against real data, not that it works.
"""

import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "monthly"
)

# The owner's known-good close for Jul 2026 Damansara.
EXPECTED = {
    "sales": 148586.80,
    "cogs": 55882.30,
    "cogs_pct": 37.6,
    "contribution": 84889.40,
    "cash_sales": 69638.50,
    "expected_bank_in": 24517.40,
    "pinjam_addback": 5378.00,
    "duplicate_skus": 7,
    "dead_skus": 131,
}

MONEY_PLACES = 2


def _find_fixture():
    """Newest .TXT in tests/fixtures/monthly, or None."""
    if not os.path.isdir(FIXTURE_DIR):
        return None
    candidates = sorted(
        glob.glob(os.path.join(FIXTURE_DIR, "*.TXT"))
        + glob.glob(os.path.join(FIXTURE_DIR, "*.txt")),
        key=os.path.getmtime, reverse=True,
    )
    return candidates[0] if candidates else None


SKIP_REASON = (
    "the real Jul 2026 Damansara report is not in the repo — drop it at "
    f"{os.path.relpath(FIXTURE_DIR)}/M-Damansara Jul2026.TXT to run the "
    "acceptance checks (production data, never committed)"
)


@unittest.skipIf(_find_fixture() is None, SKIP_REASON)
class JulyDamansaraAcceptance(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from menu_hygiene import build_menu_hygiene
        from monthly_close import build_cash_reconciliation, build_pl
        from monthly_report_parser import parse_monthly_report
        from sales_parser import read_shift_close_file

        cls.path = _find_fixture()
        cls.parsed = parse_monthly_report(read_shift_close_file(cls.path))

        by_block = {"purchase": cls.parsed["payouts"],
                    "pinjam": cls.parsed["pinjam"],
                    "salary_block": cls.parsed["salary_block"]}
        staff_names = {
            str(r["staff_name"]).strip().upper()
            for r in cls.parsed["staff_advance"] if r.get("staff_name")
        }
        cls.pl = build_pl(cls.parsed["totals"], by_block["purchase"],
                          by_block["pinjam"], by_block["salary_block"],
                          staff_names, None)
        cls.cash = build_cash_reconciliation(cls.parsed["totals"])
        cls.hygiene = build_menu_hygiene(cls.parsed["itemwise"])

    # --- the file itself ---

    def test_header_is_july_2026_damansara(self):
        self.assertEqual(self.parsed["period"], "2026-07")
        self.assertIn("DAMANSARA", (self.parsed["outlet_code"] or "").upper())

    def test_the_report_carries_every_section_the_close_needs(self):
        for section in ("itemwise", "payouts"):
            self.assertIn(section, self.parsed["sections_found"], section)

    # --- P&L ---

    def test_sales(self):
        self.assertAlmostEqual(self.pl["sales"], EXPECTED["sales"], places=MONEY_PLACES)

    def test_cogs(self):
        self.assertAlmostEqual(self.pl["cogs"], EXPECTED["cogs"], places=MONEY_PLACES)

    def test_cogs_percent(self):
        self.assertAlmostEqual(self.pl["cogs_pct"], EXPECTED["cogs_pct"], places=1)

    def test_contribution(self):
        self.assertAlmostEqual(self.pl["contribution"], EXPECTED["contribution"],
                               places=MONEY_PLACES)

    def test_gross_profit_is_sales_minus_cogs(self):
        self.assertAlmostEqual(self.pl["gross_profit"],
                               EXPECTED["sales"] - EXPECTED["cogs"], places=MONEY_PLACES)

    def test_it_is_contribution_not_net_profit(self):
        # No outlet_config is supplied here, so the close must refuse to name a
        # net profit no matter how complete the rest of the month looks.
        self.assertTrue(self.pl["is_contribution_only"])
        self.assertIsNone(self.pl["net_profit"])

    # --- the two mandatory reclassifications ---

    def test_pinjam_addback(self):
        self.assertAlmostEqual(self.pl["pinjam_addback"], EXPECTED["pinjam_addback"],
                               places=MONEY_PLACES)

    def test_pinjam_detail_reconciles_with_the_printed_total(self):
        self.assertTrue(self.pl["pinjam_check"]["ok"], self.pl["pinjam_check"]["flag"])

    def test_the_salary_block_really_did_contain_suppliers(self):
        # If this is zero the reclassification never fired, which would mean the
        # banner was not recognised — and COGS would be understated by the whole
        # supplier bank block.
        self.assertGreater(self.pl["cogs_breakdown"]["supplier_bank_block"], 0)

    def test_no_bank_block_row_was_left_unclassified(self):
        reclass = self.pl["salary_reclass"]
        self.assertEqual(
            reclass["flagged_rows"], [],
            f"unresolved names in the SALARY block: "
            f"{[r['payee'] for r in reclass['flagged_rows']]}",
        )

    # --- cash ---

    def test_cash_sales(self):
        self.assertAlmostEqual(self.cash["cash_sales"], EXPECTED["cash_sales"],
                               places=MONEY_PLACES)

    def test_expected_bank_in(self):
        self.assertAlmostEqual(self.cash["expected_bank_in"],
                               EXPECTED["expected_bank_in"], places=MONEY_PLACES)

    def test_cash_reconciliation_had_every_channel(self):
        self.assertEqual(self.cash["missing"], [])

    # --- menu hygiene ---

    def test_duplicate_sku_count(self):
        self.assertEqual(self.hygiene["duplicate_count"], EXPECTED["duplicate_skus"])

    def test_dead_sku_count(self):
        self.assertEqual(self.hygiene["dead_count"], EXPECTED["dead_skus"])

    def test_itemwise_amounts_sum_to_sales_ballpark(self):
        # Itemwise excludes rounding/tax, so this is a sanity band rather than
        # an equality: a parser that dropped a whole section would fail it.
        total = sum(r["amount"] for r in self.parsed["itemwise"])
        self.assertGreater(total, EXPECTED["sales"] * 0.8)
        self.assertLess(total, EXPECTED["sales"] * 1.2)


class FixturePresence(unittest.TestCase):
    """Always runs, so the acceptance state is visible in every test run."""

    def test_report_whether_the_acceptance_fixture_is_available(self):
        path = _find_fixture()
        if path is None:
            print(f"\n[acceptance] SKIPPED — {SKIP_REASON}")
        else:
            print(f"\n[acceptance] running against {os.path.relpath(path)}")
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
