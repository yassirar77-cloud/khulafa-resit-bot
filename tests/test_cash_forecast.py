"""cash_forecast: rhythm detection, projection, overdue suppliers and texts."""
import unittest
from datetime import date, timedelta

import cash_forecast as cf
from tests.fake_supabase import FakeSupabase

TODAY = date(2026, 10, 15)      # a Thursday


def _bill(merchant, day, total, *, outlet="SEK 20", rtype="SUPPLIER_PURCHASE", created=None):
    row = {"merchant": merchant, "outlet": outlet, "receipt_date": day.isoformat() if day else None,
           "total": total, "receipt_type": rtype}
    if created:
        row["created_at"] = created
    return row


def _every(merchant, step, n, total, *, end=TODAY - timedelta(days=1), **kw):
    return [_bill(merchant, end - timedelta(days=i * step), total, **kw) for i in range(n)]


class Rhythm(unittest.TestCase):
    def test_weekday_pattern_found_when_two_weekdays_cover_most_days(self):
        mondays = [date(2026, 9, 7) + timedelta(days=7 * i) for i in range(5)]
        thursdays = [date(2026, 9, 10) + timedelta(days=7 * i) for i in range(5)]
        self.assertEqual(cf.weekday_pattern(mondays + thursdays), [0, 3])
        self.assertEqual(cf.weekday_pattern(mondays + thursdays + [date(2026, 9, 8)]), [0, 3])

    def test_no_pattern_for_daily_or_scattered(self):
        daily = [TODAY - timedelta(days=i) for i in range(1, 15)]
        self.assertIsNone(cf.weekday_pattern(daily))
        self.assertIsNone(cf.weekday_pattern(daily[:3]))

    def test_projection_steps_by_median_gap(self):
        days = [TODAY - timedelta(days=d) for d in (1, 4, 7, 10)]
        rh = cf.rhythm(days, TODAY)
        self.assertEqual(rh["median_gap"], 3)
        self.assertFalse(rh["overdue"])
        self.assertEqual(cf.project_days(rh, TODAY, 7), [TODAY + timedelta(days=2), TODAY + timedelta(days=5)])

    def test_projection_on_weekdays(self):
        days = [date(2026, 9, 7) + timedelta(days=7 * i) for i in range(5)]       # Mondays
        days += [date(2026, 9, 10) + timedelta(days=7 * i) for i in range(5)]     # Thursdays
        rh = cf.rhythm(days, TODAY)
        got = cf.project_days(rh, TODAY, 7)
        self.assertEqual([d.weekday() for d in got], [0, 3])
        self.assertEqual(got[0], date(2026, 10, 19))

    def test_overdue_supplier_is_projected_from_tomorrow(self):
        days = [TODAY - timedelta(days=d) for d in (9, 12, 15, 18)]       # every 3, silent 9 days
        rh = cf.rhythm(days, TODAY)
        self.assertTrue(rh["overdue"])
        self.assertEqual(cf.project_days(rh, TODAY, 7)[0], TODAY + timedelta(days=1))

    def test_rare_supplier_is_not_projected(self):
        days = [TODAY - timedelta(days=d) for d in (10, 70, 130)]
        rh = cf.rhythm(days, TODAY)
        self.assertEqual(cf.project_days(rh, TODAY, 7), [])
        self.assertFalse(rh["overdue"])


class Forecast(unittest.TestCase):
    def test_daily_roti_and_three_day_chicken(self):
        rows = _every("ROTI CANAI SDN BHD", 1, 14, 45) + _every("BESTARI FARM", 3, 8, 600)
        fc = cf.forecast(rows, today=TODAY, horizon=7)
        by = {ln["merchant"]: ln for ln in fc["lines"]}
        self.assertEqual(len(by["ROTI CANAI SDN BHD"]["days"]), 7)
        self.assertEqual(by["ROTI CANAI SDN BHD"]["expected"], 315)
        self.assertEqual(len(by["BESTARI FARM"]["days"]), 2)       # +2, +5
        self.assertEqual(by["BESTARI FARM"]["expected"], 1200)
        self.assertEqual(fc["total"], 1515)
        self.assertEqual(fc["by_outlet"]["SEK-20"]["total"], 1515)
        self.assertEqual(fc["recent_actual"], 6 * 45 + 2 * 600)      # TODAY-6..TODAY; bills end yesterday
        self.assertEqual(fc["overdue"], [])

    def test_two_bills_one_day_are_one_delivery_and_range_uses_quartiles(self):
        d = TODAY - timedelta(days=1)
        rows = [_bill("JAKEL", d, 100), _bill("JAKEL", d, 80)]
        rows += [_bill("JAKEL", d - timedelta(days=2), 150), _bill("JAKEL", d - timedelta(days=4), 200),
                 _bill("JAKEL", d - timedelta(days=6), 250)]
        fc = cf.forecast(rows, today=TODAY, horizon=4)
        ln = fc["lines"][0]
        self.assertEqual(ln["typical"], 190)          # median of 180, 150, 200, 250
        self.assertEqual(ln["low"], 172.5)
        self.assertEqual(ln["high"], 212.5)
        self.assertEqual(len(ln["days"]), 2)

    def test_outlets_are_kept_apart_and_canonicalised(self):
        rows = _every("BESTARI FARM", 3, 6, 600, outlet="SEK 20") + _every("BESTARI FARM", 3, 6, 400, outlet="Klang")
        fc = cf.forecast(rows, today=TODAY, horizon=7)
        self.assertEqual(set(fc["by_outlet"]), {"SEK-20", "Klang B.Emas"})
        self.assertEqual(fc["by_outlet"]["Klang B.Emas"]["total"], 800)

    def test_undated_bill_counts_on_its_upload_day(self):
        rows = _every("BESTARI FARM", 3, 5, 600)
        rows.append(_bill("BESTARI FARM", None, 600,
                          created=f"{(TODAY - timedelta(days=16)).isoformat()}T03:00:00+00:00"))
        fc = cf.forecast(rows, today=TODAY, horizon=7)
        self.assertEqual(fc["lines"][0]["merchant"], "BESTARI FARM")

    def test_non_purchases_small_history_and_unknown_are_skipped(self):
        rows = _every("TNB", 3, 6, 300, rtype="UTILITY") + _every("NEW SHOP", 3, 2, 100)
        rows += _every("UNKNOWN MERCHANT", 3, 6, 100)
        fc = cf.forecast(rows, today=TODAY, horizon=7)
        self.assertEqual(fc["lines"], [])
        self.assertEqual(fc["total"], 0)

    def test_overdue_listed(self):
        rows = _every("FOOK LEONG", 3, 5, 500, end=TODAY - timedelta(days=9))
        fc = cf.forecast(rows, today=TODAY, horizon=7)
        self.assertEqual(fc["overdue"][0]["merchant"], "FOOK LEONG")
        self.assertEqual(fc["overdue"][0]["since_last"], 9)
        self.assertIn("Expected but not seen: FOOK LEONG @ SEK-20 (9d, usually every 3, ~RM500)",
                      cf.format_summary(fc))


class Texts(unittest.TestCase):
    def test_summary(self):
        rows = _every("ROTI CANAI SDN BHD", 1, 14, 45) + _every("BESTARI FARM", 3, 8, 600)
        rows += _every("JAKEL", 3, 8, 300, outlet="Klang")
        text = cf.format_summary(cf.forecast(rows, today=TODAY, horizon=7))
        self.assertIn("Cash-out forecast 16 Oct–22 Oct: RM2,115", text)
        self.assertIn("last 7 days actual", text)
        self.assertIn("• SEK-20: RM1,515 — BESTARI FARM RM1,200 (17 Oct, 20 Oct); ROTI CANAI SDN BHD RM315 (daily)", text)
        self.assertIn("• Klang B.Emas: RM600 — JAKEL RM600 (17 Oct, 20 Oct)", text)
        self.assertIn("Heaviest day:", text)

    def test_outlet_text_and_empty(self):
        rows = _every("BESTARI FARM", 3, 8, 600)
        fc = cf.forecast(rows, today=TODAY, horizon=7)
        text = cf.format_outlet(fc, "sek20")
        self.assertIn("SEK-20 — expected cash out next 7 days: RM1,200 (range RM1,200–RM1,200)", text)
        self.assertIn("• BESTARI FARM: RM1,200 — 2× ~RM600 (Sat 17, Tue 20)", text)
        self.assertIn("not enough bill history", cf.format_outlet(fc, "Klang"))
        self.assertIn("not enough bill history", cf.format_summary(cf.forecast([], today=TODAY)))


class Build(unittest.TestCase):
    def test_build_reads_dated_and_undated_rows(self):
        db = FakeSupabase()
        for r in _every("BESTARI FARM", 3, 5, 600):
            db.table("receipts").insert(r).execute()
        db.table("receipts").insert(_bill("BESTARI FARM", None, 600,
                                          created=f"{(TODAY - timedelta(days=16)).isoformat()}T03:00:00+00:00")).execute()
        rows = cf.fetch_rows(db, TODAY)
        self.assertEqual(len(rows), 6)
        fc = cf.build(db, TODAY)
        self.assertEqual(fc["lines"][0]["merchant"], "BESTARI FARM")


if __name__ == "__main__":
    unittest.main()
