"""Tests for weekly manager report content + routing (PR #67, Phase 1)."""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weekly_manager_reports as wmr


class DateMath(unittest.TestCase):
    def test_prior_week_is_previous_full_mon_sun(self):
        # Monday 2026-06-01 -> prior week Mon 05-25 .. Sun 05-31.
        pm, ps = wmr.prior_week_range(date(2026, 6, 1))
        self.assertEqual(pm, date(2026, 5, 25))
        self.assertEqual(ps, date(2026, 5, 31))
        self.assertEqual(pm.weekday(), 0)
        self.assertEqual(ps.weekday(), 6)

    def test_prior_week_from_midweek_day(self):
        # Friday 2026-05-29 -> same prior week Mon 05-18 .. Sun 05-24.
        pm, ps = wmr.prior_week_range(date(2026, 5, 29))
        self.assertEqual(pm, date(2026, 5, 18))
        self.assertEqual(ps, date(2026, 5, 24))

    def test_week_before_is_seven_days_earlier(self):
        bpm, bps = wmr.week_before_range(date(2026, 6, 1))
        self.assertEqual(bpm, date(2026, 5, 18))
        self.assertEqual(bps, date(2026, 5, 24))

    def test_dates_in_range_inclusive_seven_days(self):
        pm, ps = wmr.prior_week_range(date(2026, 6, 1))
        self.assertEqual(len(wmr.dates_in_range(pm, ps)), 7)


class Content(unittest.TestCase):
    def test_message_has_exact_benchmark_line(self):
        msg = wmr.format_manager_message("SEK 20", 31.2, 29.8, 28.0, "note")
        self.assertIn(
            "Your food cost: 31.2% | Group avg: 29.8% | Last week: 28.0%", msg
        )

    def test_message_handles_missing_values(self):
        msg = wmr.format_manager_message("SEK 20", None, None, None, "note")
        self.assertIn("Your food cost: — | Group avg: — | Last week: —", msg)

    def test_note_spike_reads_as_bulk_stocking(self):
        # +8pp week-on-week -> not accusatory, framed as bulk stocking.
        note = wmr.contextual_note(38.0, 30.0, 32.0, complete=True)
        self.assertEqual(note, wmr.NOTE_SPIKE)
        self.assertIn("bulk stocking", note.lower())

    def test_note_green_reads_as_well_done(self):
        note = wmr.contextual_note(27.0, 27.5, 30.0, complete=True)
        self.assertEqual(note, wmr.NOTE_GREEN)

    def test_note_incomplete_reads_as_possible_closure(self):
        note = wmr.contextual_note(None, 30.0, 30.0, complete=False)
        self.assertEqual(note, wmr.NOTE_INCOMPLETE)
        self.assertIn("closure", note.lower())

    def test_no_note_or_message_is_accusatory(self):
        # Every note constant + a rendered message must pass the tone guard.
        for note in (wmr.NOTE_INCOMPLETE, wmr.NOTE_SPIKE, wmr.NOTE_GREEN,
                     wmr.NOTE_INLINE, wmr.NOTE_NEUTRAL):
            self.assertFalse(wmr.contains_accusatory(note), note)
        for this_pct in (None, 26.0, 33.0, 42.0):
            for last_pct in (None, 25.0, 38.0):
                n = wmr.contextual_note(this_pct, last_pct, 32.0, complete=this_pct is not None)
                msg = wmr.format_manager_message("Outlet", this_pct, 32.0, last_pct, n)
                self.assertFalse(wmr.contains_accusatory(msg), msg)

    def test_tone_guard_flags_banned_words(self):
        self.assertTrue(wmr.contains_accusatory("You wasted money this week"))
        self.assertTrue(wmr.contains_accusatory("This outlet FAILED its target"))


class Routing(unittest.TestCase):
    OWNER = 543674519

    def test_flag_off_routes_to_owner_with_test_prefix(self):
        d = wmr.route_message(False, "SEK 20", 111, self.OWNER)
        self.assertEqual(d.target_chat_id, self.OWNER)
        self.assertTrue(d.is_test)
        self.assertIn("[TEST — would go to SEK 20 manager]", d.prefix)
        # The registered manager is NEVER targeted while the flag is off.
        self.assertNotEqual(d.target_chat_id, 111)

    def test_flag_on_routes_to_registered_manager(self):
        d = wmr.route_message(True, "SEK 20", 111, self.OWNER)
        self.assertEqual(d.target_chat_id, 111)
        self.assertFalse(d.is_test)
        self.assertEqual(d.prefix, "")

    def test_flag_on_no_manager_falls_back_to_owner(self):
        d = wmr.route_message(True, "SEK 20", None, self.OWNER)
        self.assertEqual(d.target_chat_id, self.OWNER)
        self.assertTrue(d.is_test)
        self.assertIn("NO MANAGER REGISTERED", d.prefix)

    def test_delivery_flag_defaults_off(self):
        self.assertFalse(wmr.MANAGER_DELIVERY_ENABLED)

    def test_delivery_enabled_env_override(self):
        prev = os.environ.get("MANAGER_DELIVERY_ENABLED")
        try:
            os.environ["MANAGER_DELIVERY_ENABLED"] = "true"
            self.assertTrue(wmr.delivery_enabled())
            os.environ["MANAGER_DELIVERY_ENABLED"] = "false"
            self.assertFalse(wmr.delivery_enabled())
        finally:
            if prev is None:
                os.environ.pop("MANAGER_DELIVERY_ENABLED", None)
            else:
                os.environ["MANAGER_DELIVERY_ENABLED"] = prev


class HQSummary(unittest.TestCase):
    def _rows(self):
        return [
            {"display": "SEK 20", "this_pct": 31.0, "last_pct": 29.0,
             "manager_name": None, "route_reason": "delivery_disabled"},
            {"display": "Klang", "this_pct": 28.0, "last_pct": 27.0,
             "manager_name": "Bala", "route_reason": "manager"},
        ]

    def test_hq_summary_test_mode_banner(self):
        out = wmr.build_hq_summary("2026-05-25 → 2026-05-31", self._rows(), 30.0, False)
        self.assertIn("TEST MODE", out)
        self.assertIn("SEK 20", out)
        self.assertIn("Klang", out)
        self.assertIn("Group avg: 30.0%", out)

    def test_hq_summary_live_banner(self):
        out = wmr.build_hq_summary("2026-05-25 → 2026-05-31", self._rows(), 30.0, True)
        self.assertIn("LIVE", out)


if __name__ == "__main__":
    unittest.main()
