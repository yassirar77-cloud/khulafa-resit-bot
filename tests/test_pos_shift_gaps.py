"""Lost-shift (shift-number hole) and silent-outlet detection.

Run with::

    python -m unittest tests.test_pos_shift_gaps
"""

import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pos_shift_gaps as g  # noqa: E402
import sales_analytics  # noqa: E402


def row(code, no, day, received=None):
    return {"outlet_code": code, "shift_no": str(no),
            "shift_business_date": day, "received_at": received}


class GapTests(unittest.TestCase):
    def test_hole_in_sequence_names_the_missing_shifts(self):
        rows = [row("S-SEK6", 2752, "2026-11-12"), row("S-SEK6", 2754, "2026-11-13"),
                row("S-SEK6", 2755, "2026-11-14")]
        gaps = g.find_gaps(rows, ["SEK6"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["missing"], [2753])
        self.assertEqual(gaps[0]["after_date"], date(2026, 11, 12))
        self.assertEqual(gaps[0]["before_date"], date(2026, 11, 13))
        self.assertEqual(
            g.gap_line(gaps[0]),
            "POS shift hilang: SEK6 #2753 (antara 12 Nov dan 13 Nov)")

    def test_multi_shift_hole(self):
        gaps = g.find_gaps([row("S-SEK20", 10096, "2026-11-13"),
                            row("S-SEK20", 10100, "2026-11-16")], ["SEK20"])
        self.assertEqual(gaps[0]["missing"], [10097, 10098, 10099])
        self.assertIn("#10097–#10099 (3 shift)", g.gap_line(gaps[0]))

    def test_complete_sequence_and_duplicates_have_no_gap(self):
        rows = [row("S-KLANG", n, "2026-11-13") for n in (1842, 1843, 1843, 1844)]
        self.assertEqual(g.find_gaps(rows, ["KLANG"]), [])

    def test_counter_reset_is_not_a_gap(self):
        rows = [row("S-VISTA", 3173, "2026-11-13"), row("S-VISTA", 9001, "2026-11-14")]
        self.assertEqual(g.find_gaps(rows, ["VISTA"]), [])

    def test_inactive_outlets_and_bad_numbers_ignored(self):
        rows = [row("S-MB", 1000, "2026-11-13"), row("S-MB", 1005, "2026-11-14"),
                row("S-SEK6", None, "2026-11-13"), row("S-SEK6", "abc", "2026-11-13")]
        self.assertEqual(g.find_gaps(rows, ["SEK6"]), [])

    def test_code_case_and_spacing_group_together(self):
        rows = [row("S-Damansara", 2733, "2026-11-16"), row("s-DAMANSARA", 2735, "2026-11-17")]
        self.assertEqual(g.find_gaps(rows, ["DAMANSARA"])[0]["missing"], [2734])

    def test_fresh_gaps_only_recent_holes(self):
        old = {"outlet": "A", "missing": [1], "after_date": date(2026, 11, 5),
               "before_date": date(2026, 11, 6)}
        new = {"outlet": "A", "missing": [9], "after_date": date(2026, 11, 15),
               "before_date": date(2026, 11, 16)}
        self.assertEqual(g.fresh_gaps([old, new], date(2026, 11, 17)), [new])


class SilentTests(unittest.TestCase):
    NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)

    def test_outlet_quiet_for_too_long_is_silent(self):
        rows = [row("S-SEK20", 10099, "2026-11-15", "2026-11-15T11:00:00+00:00"),
                row("S-SEK6", 2760, "2026-11-16", "2026-11-16T23:00:00+00:00")]
        silent = g.find_silent(rows, ["SEK20", "SEK6"], self.NOW)
        self.assertEqual([s["outlet"] for s in silent], ["SEK20"])
        self.assertEqual(silent[0]["hours"], 47)
        self.assertIn("POS senyap: SEK20", g.silent_line(silent[0]))

    def test_latest_row_wins_and_created_at_is_the_fallback(self):
        rows = [row("S-SEK6", 1, "2026-11-10", "2026-11-10T00:00:00+00:00"),
                {"outlet_code": "S-SEK6", "shift_no": "2", "received_at": None,
                 "created_at": "2026-11-17T09:00:00Z"}]
        self.assertEqual(g.find_silent(rows, ["SEK6"], self.NOW), [])

    def test_outlet_with_no_rows_is_not_reported(self):
        self.assertEqual(g.find_silent([], ["SEK6"], self.NOW), [])


class StatusFormatTests(unittest.TestCase):
    LOG = [
        {"status": "inserted", "detail": None, "source_subject": "S-A"},
        {"status": "inserted", "detail": "recovered_read_mail", "source_subject": "S-B (7)"},
        {"status": "skipped_report", "detail": "monthly_report_not_ingested"},
    ]

    def test_recovered_rows_are_listed(self):
        text = sales_analytics.format_ingest_status(self.LOG)
        self.assertIn("Recovered (email was read before the poll): 1", text)
        self.assertIn("S-B (7)", text)
        self.assertIn("Skipped (monthly/other report): 1", text)
        self.assertNotIn("Missing shifts", text)

    def test_health_lines(self):
        text = sales_analytics.format_ingest_status(self.LOG, ["POS senyap: SEK20"])
        self.assertIn("• POS senyap: SEK20", text)
        clear = sales_analytics.format_ingest_status(self.LOG, [])
        self.assertIn("No missing shift numbers", clear)


if __name__ == "__main__":
    unittest.main()
