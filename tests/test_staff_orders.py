import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import staff_orders as so  # noqa: E402

THREAD = {"id": 7, "outlet_code": "SBESI", "cashier": "Kalai",
          "shift_date": "2026-09-24", "slot": "order"}


class CleanTests(unittest.TestCase):
    def test_keeps_valid_items_and_normalises_units(self):
        items = so.clean_items([
            {"item": "Ayam", "qty": "40", "unit": "KG"},
            {"item": "ikan", "qty": 10.5, "unit": "kilo"},
            {"item": "telur", "qty": 3, "unit": "karton"},
            {"item": "", "qty": 5},                      # no item
            {"item": "sotong", "qty": 0},                # zero
            {"item": "udang", "qty": 90000},             # typo-sized
            {"item": "gas", "qty": "dua"},               # not a number
            "ayam 5kg",                                  # not a dict
        ])
        self.assertEqual(items, [
            {"item": "Ayam", "qty": 40.0, "unit": "kg"},
            {"item": "ikan", "qty": 10.5, "unit": "kg"},
            {"item": "telur", "qty": 3.0, "unit": "kotak"},
        ])


class RowsTests(unittest.TestCase):
    def test_rows_are_for_the_next_day_with_canonical_names(self):
        rows = so.rows_for_reply(THREAD, [{"item": "Ayam", "qty": 40, "unit": "kg"},
                                          {"item": "chicken nugget", "qty": 2}],
                                 "esok ayam 40kg, chicken nugget 2")
        self.assertEqual(rows[0], {
            "outlet_code": "SBESI", "order_for": "2026-09-25", "raw_item": "Ayam",
            "canonical_item": "ayam", "qty": 40.0, "unit": "kg", "cashier": "Kalai",
            "thread_id": 7, "reply_text": "esok ayam 40kg, chicken nugget 2"})
        self.assertIsNone(rows[1]["canonical_item"])   # kept, never guessed

    def test_bad_thread_date_saves_nothing(self):
        self.assertEqual(so.rows_for_reply({**THREAD, "shift_date": None},
                                           [{"item": "ayam", "qty": 1}], "x"), [])


class MergeTests(unittest.TestCase):
    def _staff(self, day, item="ayam", outlet="SBESI"):
        return {"outlet_code": outlet, "canonical_item": item, "qty": 40,
                "receipt_date": day, "source": "staff"}

    def test_receipt_within_a_day_wins(self):
        receipts = [{"outlet_code": "SBESI", "canonical_item": "ayam",
                     "receipt_date": "2026-09-25"}]
        merged = so.merge(receipts, [self._staff("2026-09-24"), self._staff("2026-09-25"),
                                     self._staff("2026-09-26"), self._staff("2026-09-28")])
        self.assertEqual([r["receipt_date"] for r in merged],
                         ["2026-09-25", "2026-09-28"])

    def test_other_items_and_outlets_are_kept(self):
        receipts = [{"outlet_code": "SBESI", "canonical_item": "ayam",
                     "receipt_date": "2026-09-25"}]
        merged = so.merge(receipts, [self._staff("2026-09-25", item="ikan"),
                                     self._staff("2026-09-25", outlet="VISTA")])
        self.assertEqual(len(merged), 3)

    def test_staff_rows_count_as_buying_days_for_the_gate(self):
        import order_sanity as osy
        staff = [self._staff(f"2026-09-{d:02d}") for d in range(1, 21)]
        hist = osy.history_from_rows(so.merge([], staff), date(2026, 9, 24))
        self.assertEqual(hist["days_60"], 20)
        self.assertEqual(hist["item_days"]["ayam"], 20)


class FetchTests(unittest.TestCase):
    def test_read_failure_returns_empty(self):
        class Broken:
            def table(self, name):
                raise RuntimeError("down")
        self.assertEqual(so.fetch_history_rows(Broken(), today=date(2026, 9, 24),
                                               lookback=60), [])


if __name__ == "__main__":
    unittest.main()


class SynonymTests(unittest.TestCase):
    """Cashiers answer in Tamil, Bengali, English and Indonesian."""

    CASES = {
        "ayam": ["கோழி", "kozhi", "মুরগি", "murgi", "chicken", "Chicken 40kg", "ayam"],
        "ikan": ["மீன்", "meen", "মাছ", "maach", "fish"],
        "daging": ["மாட்டுக்கறி", "গরুর মাংস", "gorur mangsho", "beef", "daging sapi"],
        "kambing": ["ஆட்டுக்கறி", "attukari", "খাসি", "khasi", "mutton", "goat"],
        "sotong": ["கணவாய்", "kanavai", "squid", "cumi"],
        "udang": ["இறால்", "iral", "চিংড়ি", "chingri", "prawn", "shrimp"],
        "sayur": ["காய்கறி", "সবজি", "sobji", "vegetables"],
        "roti": ["ரொட்டி", "parotta", "রুটি", "ruti", "bread"],
        "telur": ["முட்டை", "muttai", "ডিম", "dim", "eggs"],
        "santan": ["தேங்காய் பால்", "thengai paal", "coconut milk"],
        "kelapa": ["தேங்காய்", "coconut"],
        "sos_cili": ["chilli sauce", "sos cili"],
        "cili": ["மிளகாய்", "morich", "chilli"],
        "beras": ["அரிசி", "চাল", "chal", "rice"],
        "bawang_besar": ["onion", "வெங்காயம்", "peyaj", "bawang besar"],
        "bawang_merah": ["shallot", "சின்ன வெங்காயம்", "bawang merah", "bawang kecil"],
        "bawang_putih": ["garlic", "பூண்டு", "roshun", "bawang putih"],
        "bawang": ["bawang"],                       # just "bawang": kept generic
        "cili_kering": ["dry chilli", "காய்ந்த மிளகாய்", "shukno morich", "cili kering"],
        "kentang": ["potato", "உருளைக்கிழங்கு", "alu", "aloo", "kentang"],
        "tomato": ["tomato", "தக்காளி", "টমেটো"],
        "sos_tomato": ["tomato sauce"],
        "halia": ["ginger", "இஞ்சி", "আদা", "ada", "halia"],
        "daun_kari": ["curry leaves", "கறிவேப்பிலை", "daun kari"],
        # Receipts keep one milk and one roti bucket, so these stay together.
        "susu": ["milk", "paal", "dudh", "susu pekat", "condensed milk"],
    }

    def test_all_languages_map_to_the_same_item(self):
        for item, words in self.CASES.items():
            for word in words:
                self.assertEqual(so.canonical(word), item, word)

    def test_processed_goods_and_look_alikes_are_not_guessed(self):
        for text in ("chicken nugget", "Knorr chicken stock", "Maggi mee ayam",
                     "rice cooker", "hotel", "dimsum", "price", "xyz", "",
                     "ada lagi"):          # "ada" is ginger only as the whole name
            self.assertIsNone(so.canonical(text), text)

    def test_every_synonym_points_to_a_known_item(self):
        import json
        import item_canonicalization_v2 as icv2
        with open(so._SYNONYMS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        import order_items
        known = set(icv2._CATEGORIES) | set(order_items._KIND)
        for item in raw:
            if not item.startswith("_"):
                self.assertIn(item, known, item)
                # New items get a draft label, not a title-cased key.
                self.assertIn(item, order_items._DISPLAY, item)

    def test_no_word_means_two_items(self):
        seen = {}
        for pattern, item, staff in so.SYNONYMS:
            if staff:
                self.assertEqual(seen.setdefault(pattern.pattern, item), item,
                                 pattern.pattern)
