"""merchant_groups: OCR spellings of one supplier fold into one name, and the
scorecard / cash forecast use it."""
import unittest
from datetime import date, timedelta

import cash_forecast as cf
import merchant_groups as mg
import supplier_scorecard as sc
from tests.fake_supabase import FakeSupabase

TODAY = date(2026, 10, 15)
CANON = [{"id": 1, "display_name": "Ayam Berlian"}, {"id": 2, "display_name": "Bestari Farm"}]
ALIASES = [{"alias_text": "AYAM BERLIAN", "canonical_id": 1},
           {"alias_text": "AYAM BERLIAN SDN BHD", "canonical_id": 1},
           {"alias_text": "AYAM BERLIAN SDN. BHD.", "canonical_id": 1},
           {"alias_text": "BESTARI FARM (M) SDN BHD", "canonical_id": 2}]


def _bills(merchant, n, total=500, step=3):
    return [{"id": i, "merchant": merchant, "outlet": "SEK 20", "total": total, "confidence": 95,
             "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": (TODAY - timedelta(days=1 + i * step)).isoformat()} for i in range(n)]


class NameMap(unittest.TestCase):
    def test_spellings_fold_and_unknown_stays_itself(self):
        names = mg.name_map(["AYAM BERLIAN SDN BHD", "Ayam Berlian Sdn. Bhd.", "ayam berlian son bhd",
                             "BESTARI FARM (M) SDN BHD", "PASAR MINI A M", None, ""], ALIASES, CANON)
        self.assertEqual(names["AYAM BERLIAN SDN BHD"], "AYAM BERLIAN")
        self.assertEqual(names["AYAM BERLIAN SDN. BHD."], "AYAM BERLIAN")
        self.assertEqual(names["AYAM BERLIAN SON BHD"], "AYAM BERLIAN")      # fuzzy tier
        self.assertEqual(names["BESTARI FARM (M) SDN BHD"], "BESTARI FARM")
        self.assertEqual(names["PASAR MINI A M"], "PASAR MINI A M")
        self.assertNotIn("", names)

    def test_empty_snapshot_means_no_grouping(self):
        self.assertEqual(mg.name_map(["X Y"], [], []), {"X Y": "X Y"})

    def test_load_never_raises(self):
        class Broken:
            def table(self, _name):
                raise RuntimeError("db down")
        self.assertEqual(mg.load(Broken()), ([], []))


class UsedByScorecardAndForecast(unittest.TestCase):
    def test_scorecard_scores_one_supplier_not_three(self):
        receipts = _bills("AYAM BERLIAN SDN BHD", 3) + _bills("AYAM BERLIAN SDN. BHD.", 3) + _bills("AYAM BERLIAN SON BHD", 2)
        for i, r in enumerate(receipts):
            r["id"] = i
            r["receipt_date"] = (TODAY - timedelta(days=1 + i * 2)).isoformat()
        names = mg.name_map({r["merchant"] for r in receipts}, ALIASES, CANON)
        cards = sc.score_all(receipts, [], today=TODAY, days=30, names=names)
        self.assertEqual([c["merchant"] for c in cards], ["AYAM BERLIAN"])
        self.assertEqual(cards[0]["bills"], 8)
        # Without the map they are three suppliers, two of them unscored.
        self.assertEqual(len(sc.score_all(receipts, [], today=TODAY, days=30)), 3)

    def test_forecast_projects_one_supplier(self):
        rows = _bills("AYAM BERLIAN SDN BHD", 3, step=6) + _bills("AYAM BERLIAN SDN. BHD.", 3, step=6)
        for i, r in enumerate(rows[3:]):
            r["receipt_date"] = (TODAY - timedelta(days=4 + i * 6)).isoformat()
        names = mg.name_map({r["merchant"] for r in rows}, ALIASES, CANON)
        fc = cf.forecast(rows, today=TODAY, horizon=7, names=names)
        self.assertEqual([ln["merchant"] for ln in fc["lines"]], ["AYAM BERLIAN"])
        self.assertEqual(fc["lines"][0]["median_gap"], 3)

    def test_build_loads_the_snapshot(self):
        db = FakeSupabase()
        for r in _bills("AYAM BERLIAN SDN BHD", 3) + _bills("AYAM BERLIAN SDN. BHD.", 3):
            r.pop("id")
            db.table("receipts").insert(r).execute()
        for a in ALIASES:
            db.table("merchant_alias").insert(dict(a)).execute()
        for c in CANON:
            db.table("merchant_canonical").insert(dict(c)).execute()
        cards = sc.build(db, TODAY, 30)
        self.assertEqual([c["merchant"] for c in cards], ["AYAM BERLIAN"])
        fc = cf.build(db, TODAY)
        self.assertEqual({ln["merchant"] for ln in fc["lines"]}, {"AYAM BERLIAN"})


if __name__ == "__main__":
    unittest.main()
