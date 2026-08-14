"""Unit tests for ``demand_forecast`` — the cook-to-demand engine.

Hermetic: the model layers are fed plain dicts, and the DB layers run against
``tests.fake_supabase``. The cases model the real kitchen situations the
feature exists for:

  * a sell-out (Left = 0) is CENSORED demand, not a normal day — the model
    must lift it and lean up, never learn to keep the shop dry;
  * Telur Ikan's ``pos_qty`` is kg PURCHASED, not sold, so it must never be
    read as demand;
  * a Friday must be allowed to run above a Tuesday, but two observed Fridays
    must not swing the plan;
  * a shop cooking well above what it sells gets CUT — but only past BOTH
    gates, so nobody is nagged over two pieces.

Run with::

    python -m unittest tests.test_demand_forecast
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demand_forecast as df  # noqa: E402
from tests.fake_supabase import FakeSupabase  # noqa: E402

TARGET = date(2026, 8, 14)   # a Friday


def usage_row(outlet, day, item, cooked=None, left=None, pos=None):
    """One ``kitchen_daily_usage`` row, with used_qty generated like the DB."""
    used = None if (cooked is None or left is None) else cooked - left
    return {
        "outlet_code": outlet,
        "business_date": day.isoformat() if isinstance(day, date) else day,
        "item_code": item,
        "cooked_qty": cooked,
        "left_qty": left,
        "used_qty": used,
        "pos_qty": pos,
    }


def steady_points(n, demand, *, end=TARGET, cooked=None, left=2.0):
    """``{iso: point}`` of ``n`` days ending the day before ``end``."""
    out = {}
    for i in range(1, n + 1):
        d = end - timedelta(days=i)
        out[d.isoformat()] = {
            "demand": float(demand),
            "censored": False,
            "cooked": float(cooked if cooked is not None else demand + left),
            "left": float(left),
            "source": "pos",
        }
    return out


class DayDemand(unittest.TestCase):

    def test_demand_is_always_used_never_pos(self):
        # Production measurement: pos_qty is present on only 46% of rows and
        # runs 3.6-13x below Used, because kitchen_usage counts only dishes
        # that map cleanly to a whole cut. Mixing the two scales in one series
        # is what produced 23-53% forecast error. Used is the only signal.
        row = usage_row("SEK6", TARGET, "ayam_goreng", cooked=100, left=10, pos=25)
        point = df.day_demand(row, "ayam_goreng")
        self.assertEqual(point["demand"], 90.0)
        self.assertEqual(point["source"], "used")
        self.assertEqual(point["pos"], 25.0)      # carried for diagnostics only

    def test_no_used_means_no_demand_even_with_pos(self):
        # A POS-only row cannot stand in for a keyed-in day: it is a different
        # scale, so admitting it would reintroduce the mixture.
        row = usage_row("SEK6", TARGET, "ayam_goreng", cooked=None, left=None, pos=85)
        self.assertIsNone(df.day_demand(row, "ayam_goreng"))

    def test_telur_ikan_pos_column_is_kg_bought_never_demand(self):
        # pos_qty for Telur Ikan holds kg PURCHASED (PURCHASE_COMPARE_CODES) —
        # 13.4x its Used in production. Nothing reads it.
        row = usage_row("SEK6", TARGET, "telur_ikan", cooked=6.0, left=1.0, pos=20.0)
        point = df.day_demand(row, "telur_ikan")
        self.assertEqual(point["demand"], 5.0)
        self.assertEqual(point["source"], "used")

    def test_zero_left_marks_the_day_censored(self):
        row = usage_row("SEK6", TARGET, "ayam_goreng", cooked=80, left=0, pos=80)
        self.assertTrue(df.day_demand(row, "ayam_goreng")["censored"])

    def test_leftover_day_is_not_censored(self):
        row = usage_row("SEK6", TARGET, "ayam_goreng", cooked=80, left=12, pos=68)
        self.assertFalse(df.day_demand(row, "ayam_goreng")["censored"])

    def test_no_usable_numbers_returns_none(self):
        self.assertIsNone(
            df.day_demand(usage_row("SEK6", TARGET, "ayam_goreng"), "ayam_goreng")
        )

    def test_negative_demand_is_dropped(self):
        # Left keyed in above Cooked — a key-in error, not negative demand.
        # Production: kambing's median Left (4.5) exceeds its median Cooked (4.0).
        row = usage_row("SEK6", TARGET, "ayam_goreng", cooked=10, left=25)
        self.assertIsNone(df.day_demand(row, "ayam_goreng"))


class OutlierRejection(unittest.TestCase):
    """Production carries cooked entries of 2000 pcs, 3500 kg and 10900 kg of
    kambing. They survive a median but wreck the thin per-weekday samples and
    every surplus figure the plan quotes."""

    def _points(self, values):
        return [
            (TARGET - timedelta(days=i + 1), {"demand": float(v), "censored": False})
            for i, v in enumerate(values)
        ]

    def test_numpad_fat_finger_is_dropped(self):
        kept = df.reject_outliers(self._points([3, 4, 3, 5, 3, 4, 10900]))
        self.assertNotIn(10900.0, [p["demand"] for _, p in kept])
        self.assertEqual(len(kept), 6)

    def test_a_real_busy_day_on_a_small_item_survives(self):
        # Median 3 kg; a genuine 20 kg festival day must NOT be thrown away —
        # that is what the absolute margin protects.
        kept = df.reject_outliers(self._points([3, 4, 3, 5, 3, 4, 20]))
        self.assertIn(20.0, [p["demand"] for _, p in kept])

    def test_big_item_drops_its_fat_finger_but_keeps_a_heavy_day(self):
        # ayam_goreng's real numbers: median ~160/day, with a 2000 in the log.
        kept = df.reject_outliers(self._points([150, 160, 170, 155, 165, 2000]))
        self.assertNotIn(2000.0, [p["demand"] for _, p in kept])
        self.assertEqual(len(kept), 5)
        # A heavy-but-possible day stays — the filter kills typos, not business.
        kept = df.reject_outliers(self._points([150, 160, 170, 155, 165, 400]))
        self.assertIn(400.0, [p["demand"] for _, p in kept])

    def test_too_few_points_are_left_alone(self):
        self.assertEqual(len(df.reject_outliers(self._points([5, 4000]))), 2)

    def test_outlier_does_not_reach_the_forecast(self):
        points = steady_points(30, 100)
        spike = (TARGET - timedelta(days=3)).isoformat()
        points[spike] = dict(points[spike], demand=2000.0)
        fc = df.forecast_item("ayam_goreng", points, TARGET)
        self.assertLess(fc["forecast"], 115.0)


class VolumeFloor(unittest.TestCase):
    """Most tracked items move 3-6 a day in production, where one portion
    swings the error by a third. "Cook ~3 kg daging" is noise, not advice."""

    def test_low_volume_pcs_item_gets_no_plan(self):
        self.assertIsNone(
            df.forecast_item("ayam_kicap", steady_points(30, 6), TARGET)
        )

    def test_low_volume_kg_item_gets_no_plan(self):
        self.assertIsNone(
            df.forecast_item("daging", steady_points(30, 1.5), TARGET)
        )

    def test_the_item_that_actually_moves_gets_a_plan(self):
        self.assertIsNotNone(
            df.forecast_item("ayam_goreng", steady_points(30, 160), TARGET)
        )

    def test_kg_floor_is_lower_than_the_pcs_floor(self):
        self.assertIsNotNone(
            df.forecast_item("kambing", steady_points(30, 4.0), TARGET)
        )


class KillSwitch(unittest.TestCase):
    """An unproven forecast must never reach a kitchen on a timer."""

    def setUp(self):
        self._prev = os.environ.get("COOK_PLAN_ENABLED")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("COOK_PLAN_ENABLED", None)
        else:
            os.environ["COOK_PLAN_ENABLED"] = self._prev

    def test_default_is_off(self):
        os.environ.pop("COOK_PLAN_ENABLED", None)
        self.assertFalse(df.cook_plan_enabled())

    def test_explicit_truthy_turns_it_on(self):
        for value in ("1", "true", "TRUE", "yes", "on", "y"):
            os.environ["COOK_PLAN_ENABLED"] = value
            self.assertTrue(df.cook_plan_enabled(), value)

    def test_anything_else_stays_off(self):
        for value in ("", "0", "false", "no", "maybe"):
            os.environ["COOK_PLAN_ENABLED"] = value
            self.assertFalse(df.cook_plan_enabled(), value)


class Rounding(unittest.TestCase):

    def test_pcs_round_to_whole_pieces(self):
        self.assertEqual(df._round_qty(42.4, "pcs"), 42.0)

    def test_big_pcs_round_to_nearest_five(self):
        # Above 50 pieces, single pieces are noise at the stove.
        self.assertEqual(df._round_qty(87.0, "pcs"), 85.0)
        self.assertEqual(df._round_qty(88.0, "pcs"), 90.0)

    def test_kg_rounds_to_half_kilo(self):
        self.assertEqual(df._round_qty(4.3, "kg"), 4.5)
        self.assertEqual(df._round_qty(4.1, "kg"), 4.0)


class Seasonality(unittest.TestCase):

    def _points(self, per_weekday):
        pts = []
        for i in range(1, 43):
            d = TARGET - timedelta(days=i)
            pts.append((d, {"demand": float(per_weekday[d.weekday()]),
                            "censored": False}))
        return pts

    def test_busy_friday_lifts_the_factor(self):
        base = {i: 100 for i in range(7)}
        base[4] = 140                      # Fridays genuinely run hot
        points = self._points(base)
        factor = df.dow_factor(points, 4, level=100.0)
        self.assertGreater(factor, 1.15)
        self.assertLessEqual(factor, df._DOW_CLAMP[1])

    def test_two_observations_are_shrunk_toward_one(self):
        # Only two sampled Fridays, each double the level: the shrink must keep
        # the factor far below the raw 2.0 the median suggests.
        points = [
            (TARGET - timedelta(days=7), {"demand": 200.0, "censored": False}),
            (TARGET - timedelta(days=14), {"demand": 200.0, "censored": False}),
        ]
        self.assertLess(df.dow_factor(points, 4, level=100.0), 1.5)

    def test_single_observation_gives_no_factor(self):
        points = [(TARGET - timedelta(days=7), {"demand": 500.0, "censored": False})]
        self.assertEqual(df.dow_factor(points, 4, level=100.0), 1.0)

    def test_flat_week_is_neutral(self):
        points = self._points({i: 100 for i in range(7)})
        self.assertAlmostEqual(df.dow_factor(points, 2, level=100.0), 1.0, places=2)


class Trend(unittest.TestCase):

    def _series(self, values):
        return [
            (TARGET - timedelta(days=len(values) - i), {"demand": float(v),
                                                        "censored": False})
            for i, v in enumerate(values)
        ]

    def test_flat_history_is_neutral(self):
        self.assertEqual(df.trend_factor(self._series([100] * 28)), 1.0)

    def test_growth_is_followed_but_clamped(self):
        rising = self._series([100] * 14 + [200] * 14)
        self.assertEqual(df.trend_factor(rising), df._TREND_CLAMP[1])

    def test_decline_is_followed_but_clamped(self):
        falling = self._series([200] * 14 + [50] * 14)
        self.assertEqual(df.trend_factor(falling), df._TREND_CLAMP[0])

    def test_too_little_history_is_neutral(self):
        self.assertEqual(df.trend_factor(self._series([100] * 10)), 1.0)


class Safety(unittest.TestCase):

    def _points(self, values):
        return [
            (TARGET - timedelta(days=i + 1), {"demand": float(v), "censored": False})
            for i, v in enumerate(values)
        ]

    def test_steady_item_gets_the_base_buffer(self):
        self.assertEqual(df.safety_pct(self._points([100] * 20), 100.0, 0),
                         df._BASE_SAFETY)

    def test_volatile_item_gets_more_headroom(self):
        swingy = self._points([60, 140] * 10)
        self.assertGreater(df.safety_pct(swingy, 100.0, 0), df._BASE_SAFETY)

    def test_recent_sellouts_add_buffer(self):
        steady = self._points([100] * 20)
        self.assertGreater(
            df.safety_pct(steady, 100.0, 3), df.safety_pct(steady, 100.0, 0)
        )

    def test_buffer_is_capped(self):
        wild = self._points([10, 400] * 10)
        self.assertLessEqual(df.safety_pct(wild, 100.0, 9), df._MAX_SAFETY)


class Action(unittest.TestCase):

    def test_clear_overcooking_is_cut(self):
        self.assertEqual(df.decide_action(80.0, 120.0, 0, "pcs"), "CUT")

    def test_small_gap_is_left_alone(self):
        # 2 pcs over on a 60-pc cook trips neither gate — nobody gets nagged.
        self.assertEqual(df.decide_action(58.0, 60.0, 0, "pcs"), "HOLD")

    def test_percentage_gate_alone_is_not_enough(self):
        # 20% over, but only 2 pcs: the absolute gate holds it back.
        self.assertEqual(df.decide_action(8.0, 10.0, 0, "pcs"), "HOLD")

    def test_repeated_sellouts_raise_instead_of_cut(self):
        # Sold out twice recently: lost sales cost more than a few leftovers,
        # and a kitchen told to cut on a sell-out stops reading the message.
        self.assertEqual(df.decide_action(130.0, 100.0, 2, "pcs"), "RAISE")

    def test_no_cooking_history_holds(self):
        self.assertEqual(df.decide_action(50.0, None, 0, "pcs"), "HOLD")

    def test_busy_day_raises_without_any_sellout(self):
        # A Friday uplift is a real raise. The first cut returned HOLD here and
        # then printed "your current amount is right" above a bigger number.
        self.assertEqual(df.decide_action(190.0, 160.0, 0, "pcs"), "RAISE")

    def test_small_uplift_stays_hold(self):
        self.assertEqual(df.decide_action(164.0, 160.0, 0, "pcs"), "HOLD")

    def test_recent_sellouts_veto_a_cut(self):
        # Telling a kitchen to cook less in a week it kept running out is the
        # fastest way to make it stop reading the message.
        self.assertEqual(df.decide_action(80.0, 120.0, 3, "pcs"), "HOLD")


class Reasons(unittest.TestCase):

    def test_sellout_reason_wins(self):
        self.assertEqual(df._reason_for("RAISE", 3, 1.2, 1.0), "sellout")

    def test_weekday_uplift_is_a_busy_day(self):
        self.assertEqual(df._reason_for("RAISE", 0, 1.2, 1.0), "busy_day")

    def test_flat_weekday_uplift_is_a_trend(self):
        self.assertEqual(df._reason_for("RAISE", 0, 1.0, 1.15), "trend")

    def test_cut_and_hold_reasons(self):
        self.assertEqual(df._reason_for("CUT", 0, 1.0, 1.0), "surplus")
        self.assertEqual(df._reason_for("HOLD", 0, 1.0, 1.0), "steady")

    def test_busy_day_line_names_the_day_and_the_usual(self):
        fc = {
            "label": "Ayam Goreng", "unit": "pcs", "recommend": 190.0,
            "usual_cooked": 160.0, "action": "RAISE", "reason": "busy_day",
            "day_name": "Jumaat", "sellouts": 0,
        }
        line = df._plan_line(fc)
        self.assertIn("190", line)
        self.assertIn("160", line)
        self.assertIn("Jumaat", line)
        self.assertNotIn("சரியா இருக்கு", line)   # never the HOLD copy

    def test_owner_summary_ignores_a_routine_busy_day_raise(self):
        entry = {
            "outlet_code": "SEK6", "display": "Sek 6",
            "business_date": TARGET.isoformat(),
            "items": [{
                "label": "Ayam Goreng", "unit": "pcs", "recommend": 190.0,
                "usual_cooked": 160.0, "action": "RAISE", "reason": "busy_day",
                "day_name": "Jumaat", "sellouts": 0,
            }],
        }
        self.assertEqual(df.format_owner_summary([entry]), "")

    def test_owner_summary_keeps_a_sellout_raise(self):
        entry = {
            "outlet_code": "SEK6", "display": "Sek 6",
            "business_date": TARGET.isoformat(),
            "items": [{
                "label": "Ayam Goreng", "unit": "pcs", "recommend": 190.0,
                "usual_cooked": 160.0, "action": "RAISE", "reason": "sellout",
                "day_name": "Jumaat", "sellouts": 4,
            }],
        }
        self.assertIn("Sek 6", df.format_owner_summary([entry]))


class ForecastItem(unittest.TestCase):

    def test_thin_history_returns_nothing(self):
        points = steady_points(df.MIN_DATA_DAYS - 1, 100)
        self.assertIsNone(df.forecast_item("ayam_goreng", points, TARGET))

    def test_steady_shop_gets_demand_plus_the_base_buffer(self):
        points = steady_points(30, 100)
        fc = df.forecast_item("ayam_goreng", points, TARGET)
        self.assertAlmostEqual(fc["forecast"], 100.0, places=1)
        self.assertEqual(fc["safety"], df._BASE_SAFETY)
        self.assertEqual(fc["recommend"], 105.0)
        self.assertEqual(fc["confidence"], "high")

    def test_target_day_is_never_used_to_forecast_itself(self):
        points = steady_points(30, 100)
        points[TARGET.isoformat()] = {
            "demand": 9999.0, "censored": False, "cooked": 9999.0, "left": 0.0,
        }
        fc = df.forecast_item("ayam_goreng", points, TARGET)
        self.assertAlmostEqual(fc["forecast"], 100.0, places=1)

    def test_selling_out_every_day_pushes_the_plan_above_the_cook(self):
        # 30 days of cooking 100 and finishing every one of them: the shop is
        # capped by the pot, not by demand. The plan must exceed 100.
        points = {}
        for i in range(1, 31):
            d = TARGET - timedelta(days=i)
            points[d.isoformat()] = {
                "demand": 100.0, "censored": True, "cooked": 100.0, "left": 0.0,
            }
        fc = df.forecast_item("ayam_goreng", points, TARGET)
        self.assertGreater(fc["recommend"], 100.0)
        self.assertEqual(fc["action"], "RAISE")
        self.assertEqual(fc["sellouts"], df._RECENT_DAYS)

    def test_chronic_overcooking_is_flagged_with_the_surplus(self):
        points = steady_points(30, 60, cooked=100, left=40)
        fc = df.forecast_item("ayam_goreng", points, TARGET)
        self.assertEqual(fc["action"], "CUT")
        self.assertEqual(fc["usual_cooked"], 100.0)
        self.assertGreater(fc["surplus"], 30.0)

    def test_one_absurd_keyin_does_not_move_the_plan(self):
        # A 3000-pc typo is exactly why the level is a median.
        points = steady_points(30, 100)
        spike_day = (TARGET - timedelta(days=3)).isoformat()
        points[spike_day] = dict(points[spike_day], demand=3000.0)
        fc = df.forecast_item("ayam_goreng", points, TARGET)
        self.assertLess(fc["forecast"], 130.0)

    def test_unknown_item_code_is_ignored(self):
        self.assertIsNone(
            df.forecast_item("nasi_lemak", steady_points(30, 100), TARGET)
        )


class OutletEvaluation(unittest.TestCase):

    def _series(self):
        return {
            "ayam_goreng": steady_points(30, 100, cooked=160, left=60),  # big CUT
            "ayam_kicap": steady_points(30, 40),                          # HOLD
            "ayam_rempah": steady_points(30, 20),                         # Bistro only
        }

    def test_bistro_only_item_is_skipped_elsewhere(self):
        codes = [f["code"] for f in df.evaluate_outlet(self._series(), "SEK6", TARGET)]
        self.assertNotIn("ayam_rempah", codes)

    def test_bistro_keeps_its_own_item(self):
        codes = [
            f["code"] for f in df.evaluate_outlet(self._series(), "BISTRO7", TARGET)
        ]
        self.assertIn("ayam_rempah", codes)

    def test_biggest_correction_is_listed_first(self):
        items = df.evaluate_outlet(self._series(), "SEK6", TARGET)
        self.assertEqual(items[0]["code"], "ayam_goreng")
        self.assertEqual(items[0]["action"], "CUT")

    def test_no_history_gives_no_plan(self):
        self.assertEqual(df.evaluate_outlet({}, "SEK6", TARGET), [])


class Messages(unittest.TestCase):

    def _entry(self):
        series = {"ayam_goreng": steady_points(30, 100, cooked=160, left=60)}
        return {
            "outlet_code": "SEK6",
            "display": "Sek 6",
            "business_date": TARGET.isoformat(),
            "items": df.evaluate_outlet(series, "SEK6", TARGET),
        }

    def test_cook_plan_carries_the_number_and_the_shop(self):
        text = df.format_cook_plan(self._entry())
        self.assertIn("Sek 6", text)
        self.assertIn("Ayam Goreng", text)
        self.assertIn("masak", text)
        self.assertIn("⬇️", text)          # over-cooking, cut it

    def test_cook_plan_empty_without_items(self):
        self.assertEqual(
            df.format_cook_plan({"display": "Sek 6", "items": []}), ""
        )

    def test_cook_plan_survives_garbage(self):
        self.assertEqual(df.format_cook_plan(None), "")
        self.assertEqual(df.format_cook_plan({"items": [{}]}), "")

    def test_owner_summary_lists_only_actionable_outlets(self):
        entry = self._entry()
        quiet = {
            "outlet_code": "SEK20", "display": "Sek 20",
            "business_date": TARGET.isoformat(),
            "items": df.evaluate_outlet(
                {"ayam_kicap": steady_points(30, 40)}, "SEK20", TARGET
            ),
        }
        summary = df.format_owner_summary([entry, quiet])
        self.assertIn("Sek 6", summary)
        self.assertNotIn("Sek 20", summary)

    def test_owner_summary_empty_when_everything_holds(self):
        quiet = {
            "outlet_code": "SEK20", "display": "Sek 20",
            "business_date": TARGET.isoformat(),
            "items": df.evaluate_outlet(
                {"ayam_kicap": steady_points(30, 40)}, "SEK20", TARGET
            ),
        }
        self.assertEqual(df.format_owner_summary([quiet]), "")


class PersistenceAndScoring(unittest.TestCase):

    def _client_with_history(self):
        client = FakeSupabase()
        for i in range(1, 35):
            d = TARGET - timedelta(days=i)
            client.table("kitchen_daily_usage").insert(
                usage_row("SEK6", d, "ayam_goreng", cooked=160, left=60, pos=100)
            ).execute()
        client.table("outlet_canonical").insert(
            {"code": "S-SEK6", "canonical_name": "Sek 6", "active": True}
        ).execute()
        return client

    def test_plan_is_logged_then_scored_the_next_day(self):
        client = self._client_with_history()
        bundle = df.gather_cook_plans(client, today=TARGET)
        self.assertEqual(len(bundle["entries"]), 1)
        logged = client.rows("kitchen_demand_forecast")
        self.assertTrue(logged)
        # Unscored on the way out (the real column defaults to NULL; the fake
        # client simply never writes the key).
        self.assertTrue(all(r.get("actual_qty") is None for r in logged))

        # The day happens; the kitchen keys it in.
        client.table("kitchen_daily_usage").insert(
            usage_row("SEK6", TARGET, "ayam_goreng", cooked=160, left=70, pos=90)
        ).execute()
        scored = df.score_previous_day(client, TARGET)
        self.assertEqual(scored["scored"], len(logged))

        row = client.rows("kitchen_demand_forecast")[0]
        self.assertEqual(row["actual_qty"], 90.0)
        self.assertAlmostEqual(row["abs_error"], abs(90.0 - row["forecast_qty"]), 2)
        self.assertFalse(row["sold_out"])

    def test_scoring_bridges_the_outlet_code_forms(self):
        # The usage row lands under the sales-side code form; an exact-match
        # join would leave that outlet unscored forever.
        client = self._client_with_history()
        df.gather_cook_plans(client, today=TARGET)
        client.table("kitchen_daily_usage").insert(
            usage_row("S-SEK6", TARGET, "ayam_goreng", cooked=160, left=70, pos=90)
        ).execute()
        self.assertEqual(df.score_previous_day(client, TARGET)["scored"], 1)
        self.assertEqual(
            client.rows("kitchen_demand_forecast")[0]["actual_qty"], 90.0
        )

    def test_scoring_records_a_sellout_day(self):
        client = self._client_with_history()
        df.gather_cook_plans(client, today=TARGET)
        client.table("kitchen_daily_usage").insert(
            usage_row("SEK6", TARGET, "ayam_goreng", cooked=160, left=0, pos=160)
        ).execute()
        df.score_previous_day(client, TARGET)
        self.assertTrue(client.rows("kitchen_demand_forecast")[0]["sold_out"])

    def test_second_run_is_idempotent(self):
        client = self._client_with_history()
        df.gather_cook_plans(client, today=TARGET)
        first = len(client.rows("kitchen_demand_forecast"))
        df.gather_cook_plans(client, today=TARGET)
        self.assertEqual(len(client.rows("kitchen_demand_forecast")), first)

    def test_missing_forecast_table_never_blocks_the_plan(self):
        class Broken(FakeSupabase):
            def table(self, name):
                if name == df.FORECAST_TABLE:
                    raise RuntimeError('relation "kitchen_demand_forecast" missing')
                return super().table(name)

        client = Broken()
        for i in range(1, 35):
            d = TARGET - timedelta(days=i)
            client.table("kitchen_daily_usage").insert(
                usage_row("SEK6", d, "ayam_goreng", cooked=160, left=60, pos=100)
            ).execute()
        client.table("outlet_canonical").insert(
            {"code": "S-SEK6", "canonical_name": "Sek 6", "active": True}
        ).execute()
        bundle = df.gather_cook_plans(client, today=TARGET)
        self.assertEqual(len(bundle["entries"]), 1)   # plan still reaches the kitchen

    def test_thin_history_outlets_are_counted_not_guessed_at(self):
        client = FakeSupabase()
        client.table("outlet_canonical").insert(
            {"code": "S-SEK6", "canonical_name": "Sek 6", "active": True}
        ).execute()
        for i in range(1, 4):
            d = TARGET - timedelta(days=i)
            client.table("kitchen_daily_usage").insert(
                usage_row("SEK6", d, "ayam_goreng", cooked=100, left=10, pos=90)
            ).execute()
        bundle = df.gather_cook_plans(client, today=TARGET)
        self.assertEqual(bundle["entries"], [])
        self.assertEqual(bundle["skipped_thin"], 1)


class Accuracy(unittest.TestCase):

    def test_median_error_ignores_one_freak_item(self):
        rows = [{"outlet_code": "SEK6", "pct_error": p, "sold_out": False}
                for p in (5.0, 8.0, 9.0, 400.0)]
        stats = df.summarise_accuracy(rows)
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["median_pct_error"], 8.5)
        self.assertEqual(stats["within_10"], 3)

    def test_sellouts_are_counted_separately(self):
        rows = [
            {"outlet_code": "SEK6", "pct_error": 5.0, "sold_out": True},
            {"outlet_code": "SEK6", "pct_error": None, "sold_out": True},
        ]
        stats = df.summarise_accuracy(rows)
        self.assertEqual(stats["sold_out"], 2)
        self.assertEqual(stats["n"], 1)

    def test_report_says_so_when_nothing_scored_yet(self):
        self.assertIn("nothing scored yet",
                      df.format_accuracy(df.summarise_accuracy([]), 28))

    def test_report_renders_the_numbers(self):
        rows = [{"outlet_code": "SEK6", "pct_error": 6.0, "sold_out": False}]
        text = df.format_accuracy(df.summarise_accuracy(rows), 28)
        self.assertIn("Median error: 6.0%", text)
        self.assertIn("SEK6", text)


if __name__ == "__main__":
    unittest.main()
