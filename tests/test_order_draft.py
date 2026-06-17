"""Quantity fork + draft formatting (order_draft)."""
import unittest
from datetime import date, timedelta

import order_cadence as oc
import order_draft


def _recs(start: date, step: int, qtys: list[float]) -> list[dict]:
    return [{"date": start + timedelta(days=step * i), "qty": q}
            for i, q in enumerate(qtys)]


class WeekendMultiplierTests(unittest.TestCase):
    def test_derives_multiplier_from_data(self):
        per_buy = []
        d = date(2026, 4, 6)  # Monday
        for _ in range(4):
            per_buy.append((d, 10.0))             # Mon weekday
            per_buy.append((d + timedelta(days=5), 20.0))  # Sat weekend
            d += timedelta(days=7)
        mult = order_draft.weekend_multiplier(per_buy)
        self.assertAlmostEqual(mult, 2.0, places=2)

    def test_clamped_and_safe_without_data(self):
        self.assertEqual(order_draft.weekend_multiplier([]), 1.0)
        # weekday only -> no weekend evidence -> 1.0
        weekday_only = [(date(2026, 4, 6), 10.0)]
        self.assertEqual(order_draft.weekend_multiplier(weekday_only), 1.0)


class ForecastQtyTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 7)

    def test_daily_uses_trailing_average(self):
        recs = _recs(self.today - timedelta(days=9), 1, [10] * 10)
        ci = {"cadence": oc.DAILY, "canonical_item": "ayam"}
        # target a weekday (Wednesday) so no weekend bump.
        fc = order_draft.forecast_qty(recs, ci, target_day=date(2026, 6, 10))
        self.assertEqual(fc["qty"], 10)
        self.assertFalse(fc["pack_known"])  # no known pack -> flag for manager

    def test_weekly_qty_covers_cycle(self):
        recs = _recs(self.today - timedelta(days=28), 7, [100, 100, 100, 100, 100])
        ci = {"cadence": oc.WEEKLY, "canonical_item": "beras"}
        fc = order_draft.forecast_qty(recs, ci, target_day=self.today + timedelta(days=1))
        self.assertEqual(fc["qty"], 100)  # whole-cycle qty, not 1/7th

    def test_rounds_up(self):
        recs = _recs(self.today - timedelta(days=5), 1, [3.2, 3.4, 3.1])
        ci = {"cadence": oc.DAILY, "canonical_item": "udang"}
        fc = order_draft.forecast_qty(recs, ci, target_day=date(2026, 6, 10))
        self.assertEqual(fc["qty"], 4)

    def test_no_history_returns_none(self):
        ci = {"cadence": oc.DAILY, "canonical_item": "ayam"}
        fc = order_draft.forecast_qty([], ci, target_day=self.today)
        self.assertIsNone(fc["qty"])


class QtyGuardTests(unittest.TestCase):
    """Plausibility guard + window alignment (Bugs 1 & 3)."""

    def setUp(self):
        self.today = date(2026, 6, 7)
        self.wednesday = date(2026, 6, 10)  # weekday target, no weekend bump

    def _recs(self, qtys):
        # One row per distinct day, ending today.
        n = len(qtys)
        return [{"date": self.today - timedelta(days=n - 1 - i), "qty": q}
                for i, q in enumerate(qtys)]

    def test_dominating_outlier_excluded_before_average(self):
        # Normal ~40/day with one OCR-merged 40250 (receipt 2254). The offending
        # DAY is dropped before averaging, so the forecast lands at the real ~40
        # rather than a capped 200, and the excluded row is reported (not flagged
        # as an anomaly, because exclusion already handled it).
        recs = self._recs([40, 41, 39, 40, 40250, 40, 41, 39])
        ci = {"cadence": oc.DAILY, "canonical_item": "ais_batu"}
        fc = order_draft.forecast_qty(recs, ci, target_day=self.wednesday,
                                      today=self.today, lookback_days=90)
        self.assertFalse(fc["qty_anomaly"])            # handled by exclusion
        self.assertEqual(fc["excluded_count"], 1)
        self.assertEqual(fc["excluded_qtys"], [40250])
        self.assertLessEqual(fc["qty"], 45)            # real ~40, not a capped 200
        self.assertIn("excluded", fc["basis"])

    def test_outlier_note_in_manager_block_only(self):
        # The excluded-row note appears in the full (manager) line, never in the
        # compact (forwarded) line.
        line = {
            "canonical_item": "ais_batu", "qty": 40, "pack": "bag",
            "pack_known": False, "history_expired": False, "qty_anomaly": False,
            "excluded_count": 1, "excluded_qtys": [40250], "raw_qty": 40,
            "cadence_info": {"cadence": oc.DAILY, "needs_review": False,
                             "last_purchase_date": self.today, "median_gap_days": 1.0,
                             "reason": "daily"},
            "due_info": {"due": True}, "supplier": "EVEREST", "alternate": None,
            "spike": None,
        }
        self.assertIn("diabaikan", order_draft.format_item_line(line))
        self.assertNotIn("diabaikan", order_draft.format_item_line_compact(line))

    def test_clean_history_not_flagged(self):
        recs = self._recs([40, 41, 39, 40, 42, 38, 40, 41])
        ci = {"cadence": oc.DAILY, "canonical_item": "ais_batu"}
        fc = order_draft.forecast_qty(recs, ci, target_day=self.wednesday,
                                      today=self.today, lookback_days=90)
        self.assertFalse(fc["qty_anomaly"])
        self.assertEqual(fc["excluded_count"], 0)
        self.assertLessEqual(fc["qty"], 45)

    def test_future_dated_only_is_history_expired(self):
        # Only a future-dated row -> no in-window history -> no fabricated qty.
        recs = [{"date": (self.today + timedelta(days=10)).isoformat(), "qty": 5}]
        ci = {"cadence": oc.NEEDS_REVIEW, "canonical_item": "extra_juss"}
        fc = order_draft.forecast_qty(recs, ci, target_day=self.today + timedelta(days=1),
                                      today=self.today, lookback_days=90)
        self.assertIsNone(fc["qty"])
        self.assertTrue(fc["history_expired"])

    def test_history_expired_renders_reorder_not_a_number(self):
        line = {
            "canonical_item": "extra_juss", "qty": None, "pack": "pack",
            "pack_known": False, "history_expired": True, "qty_anomaly": False,
            "cadence_info": {"cadence": oc.NEEDS_REVIEW, "needs_review": True,
                             "last_purchase_date": None, "median_gap_days": None,
                             "reason": "no purchases in the last 90 days"},
            "due_info": {"due": True}, "supplier": None, "alternate": None,
            "spike": None,
        }
        txt = order_draft.format_item_line(line)
        self.assertIn("reorder?", txt)
        self.assertIn("history expired", txt)
        self.assertNotIn("qty?", txt)

    def test_qty_anomaly_flag_shows_raw(self):
        line = {
            "canonical_item": "ais_batu", "qty": 200, "pack": "bag",
            "pack_known": False, "history_expired": False, "qty_anomaly": True,
            "raw_qty": 5062,
            "cadence_info": {"cadence": oc.DAILY, "needs_review": False,
                             "last_purchase_date": self.today, "median_gap_days": 1.0,
                             "reason": "daily"},
            "due_info": {"due": True}, "supplier": "EVEREST", "alternate": None,
            "spike": None,
        }
        txt = order_draft.format_item_line(line)
        self.assertIn("❗", txt)
        self.assertIn("5062", txt)
        # The compact form surfaces the anomaly mark too.
        self.assertIn("❗", order_draft.format_item_line_compact(line))


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 7)
        self.tomorrow = self.today + timedelta(days=1)

    def _line(self, **over):
        base = {
            "canonical_item": "ayam",
            "qty": 12, "pack": "kg", "pack_known": False,
            "cadence_info": {"cadence": oc.DAILY, "needs_review": False,
                             "last_purchase_date": self.today, "median_gap_days": 1.0,
                             "reason": "daily"},
            "due_info": {"due": True, "reason": "daily item"},
            "supplier": "BESTARI FARM",
            "alternate": None, "spike": None,
        }
        base.update(over)
        return base

    def test_line_shows_reasoning(self):
        txt = order_draft.format_item_line(self._line())
        self.assertIn("Ayam", txt)
        self.assertIn("12 kg", txt)
        self.assertIn("cadence: daily", txt)
        self.assertIn("last bought", txt)
        self.assertIn("BESTARI FARM", txt)

    def test_flags_render(self):
        line = self._line(
            pack_known=False,
            cadence_info={"cadence": oc.NEEDS_REVIEW, "needs_review": True,
                          "last_purchase_date": self.today, "median_gap_days": None,
                          "reason": "irregular gaps"},
            alternate={"alternate": "Shree Map Jaya", "note": "lagi murah"},
            spike="harga naik",
        )
        txt = order_draft.format_item_line(line)
        self.assertIn("❓", txt)
        self.assertIn("NEEDS REVIEW", txt)
        self.assertIn("💰", txt)
        self.assertIn("⚠️", txt)

    def test_outlet_message_groups_by_supplier(self):
        lines = [self._line(supplier="BESTARI FARM"),
                 self._line(canonical_item="udang", supplier="FOOK LEONG")]
        msg = order_draft.build_outlet_message("SEK-20", self.tomorrow, lines)
        self.assertIn("SEK-20", msg)
        self.assertIn("BESTARI FARM", msg)
        self.assertIn("FOOK LEONG", msg)

    def test_empty_message(self):
        msg = order_draft.build_outlet_message("SEK-20", self.tomorrow, [])
        self.assertIn("Tiada item due", msg)


class ChunkingTests(unittest.TestCase):
    """build_outlet_messages: stay under the 4096 cap, never split an item."""

    def setUp(self):
        self.today = date(2026, 6, 7)
        self.tomorrow = self.today + timedelta(days=1)

    def _lines(self, n, *, supplier="BESTARI FARM"):
        # Reasoning-rich lines (each has a "↳ last bought" sub-line) so the full
        # form is large — the worst case for the 4096 cap.
        return [{
            "canonical_item": "ayam", "qty": 12, "pack": "kg", "pack_known": False,
            "cadence_info": {"cadence": oc.DAILY, "needs_review": False,
                             "last_purchase_date": self.today, "median_gap_days": 1.0,
                             "reason": "daily"},
            "due_info": {"due": True, "reason": "daily item"},
            "supplier": "%s %d" % (supplier, i % 5),  # a few suppliers, so groups span chunks
            "alternate": None, "spike": None,
        } for i in range(n)]

    def test_small_outlet_single_chunk(self):
        msgs = order_draft.build_outlet_messages("SEK-20", self.tomorrow, self._lines(3))
        self.assertEqual(len(msgs), 1)
        self.assertNotIn("sambungan", msgs[0])
        self.assertIn("SEK-20", msgs[0])

    def test_empty_is_single_note(self):
        msgs = order_draft.build_outlet_messages("SEK-20", self.tomorrow, [])
        self.assertEqual(len(msgs), 1)
        self.assertIn("Tiada item due", msgs[0])

    def test_oversized_full_form_splits_under_limit(self):
        # The full (reasoning-rich) form splits into multiple chunks, each <=
        # limit. Tested on _pack_messages directly so condensation doesn't mask
        # the multi-chunk path. 200 items would otherwise be one giant string.
        limit = 3500
        msgs = order_draft._pack_messages("SEK-20", self.tomorrow,
                                          self._lines(200), limit=limit, compact=False)
        self.assertGreater(len(msgs), 2)
        for m in msgs:
            self.assertLessEqual(len(m), limit, "a chunk exceeded the safety limit")

    def test_no_item_split_across_chunks(self):
        msgs = order_draft._pack_messages("SEK-20", self.tomorrow,
                                          self._lines(200), limit=3500, compact=False)
        for m in msgs:
            seen_head = False
            for ln in m.split("\n"):
                # An indented continuation line ("   ↳ ...") must be preceded by
                # its "• ..." head WITHIN the same chunk — never orphaned.
                if ln.startswith("   "):
                    self.assertTrue(seen_head,
                                    "continuation line orphaned from its item head")
                if ln.startswith("• "):
                    seen_head = True

    def test_all_items_preserved(self):
        lines = self._lines(200)
        msgs = order_draft._pack_messages("SEK-20", self.tomorrow, lines,
                                          limit=3500, compact=False)
        total_heads = sum(m.count("• ") for m in msgs)
        self.assertEqual(total_heads, len(lines))

    def test_two_chunk_full_form_not_condensed(self):
        # An outlet whose full draft fits in exactly two chunks keeps its full
        # reasoning (condensation only kicks in beyond two chunks).
        msgs = order_draft.build_outlet_messages("SEK-20", self.tomorrow,
                                                 self._lines(30), limit=3800)
        self.assertEqual(len(msgs), 2)
        self.assertIn("↳ last bought", "\n".join(msgs))
        for m in msgs:
            self.assertLessEqual(len(m), 3800)

    def test_condensation_kicks_in_when_over_two_chunks(self):
        # Many reasoning-rich lines -> full form needs >2 chunks -> compact form.
        lines = self._lines(120)
        msgs = order_draft.build_outlet_messages("SEK-20", self.tomorrow, lines, limit=3800)
        joined = "\n".join(msgs)
        # Compact form drops the multi-line reasoning entirely...
        self.assertNotIn("↳ last bought", joined)
        # ...keeps one head per item...
        self.assertEqual(sum(m.count("• ") for m in msgs), len(lines))
        # ...and is far more compact than the full form would have been.
        full = order_draft._pack_messages("SEK-20", self.tomorrow, lines,
                                          limit=3500, compact=False)
        self.assertLess(len(msgs), len(full))


if __name__ == "__main__":
    unittest.main()
