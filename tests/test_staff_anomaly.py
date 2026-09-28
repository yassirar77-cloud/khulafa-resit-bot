"""Anomaly questions: detection band, largest deviation, wording, logging."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_anomaly as sa
import staff_chat as sc
import staff_live as sl

VOCAB = {"ayam", "ikan", "sotong", "ayam goreng"}


def _m(metric, item, today, usual, unit="kg"):
    return {"metric": metric, "item": item, "today": today, "usual": usual, "unit": unit}


def _ai(text, english="EN"):
    return lambda system, user: {"data": {"text": text, "english": english},
                                 "provider": "deepseek", "model": "deepseek-flash",
                                 "tokens_in": 400, "tokens_out": 50}


class DetectTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_chicken_double_fires(self):
        facts = sa.detect([_m("order", "ayam", 20, [10, 11, 9, 10]),
                           _m("order", "ikan", 10, [10, 9, 11, 10])])
        self.assertEqual((facts["item"], facts["today"], facts["usual"], facts["direction"]),
                         ("Ayam", "20kg", "10kg", "high"))
        self.assertEqual(facts["deviation_pct"], 100)
        self.assertEqual(facts["metric"], "order")
        self.assertTrue(facts["anomaly"])
        self.assertEqual(facts["item_code"], "ayam")

    def test_all_within_range_is_normal(self):
        self.assertIsNone(sa.detect([_m("order", "ayam", 12, [10, 11, 9, 10]),
                                     _m("sales", "Sales", 210, [200, 240, 190, 220], ""),
                                     _m("wastage", "ayam_goreng", 3, [2, 4, 3], "pcs")]))

    def test_band_and_env(self):
        # 34% off: inside the default band; 36%: outside.
        self.assertIsNone(sa.detect([_m("order", "ayam", 13.4, [10, 10])]))
        self.assertIsNotNone(sa.detect([_m("order", "ayam", 13.6, [10, 10])]))
        self.assertEqual(sa.detect([_m("order", "ayam", 6, [10, 10])])["direction"], "low")
        with mock.patch.dict("os.environ", {"ANOMALY_PCT": "50"}):
            self.assertIsNone(sa.detect([_m("order", "ayam", 14, [10, 10])]))
            self.assertIsNotNone(sa.detect([_m("order", "ayam", 16, [10, 10])]))
        with mock.patch.dict("os.environ", {"ANOMALY_PCT": "abc"}):
            self.assertEqual(sa.threshold(), 0.35)

    def test_largest_deviation_wins_and_needs_two_weeks(self):
        facts = sa.detect([_m("order", "ayam", 15, [10, 10]),          # +50%
                           _m("sales", "Sales", 100, [300, 280, 310], ""),   # -66%
                           _m("wastage", "sotong", 9, [1])])            # one point: ignored
        self.assertEqual((facts["metric"], facts["item"], facts["today"], facts["usual"]),
                         ("sales", "Sales", "100", "297"))
        self.assertEqual(facts["deviation_pct"], -66)
        self.assertIsNone(sa.detect([_m("wastage", "sotong", 9, [1])]))
        self.assertIsNone(sa.detect([_m("order", "ayam", 9, [0, 0])]))
        self.assertIsNone(sa.detect([_m("order", "ayam", "x", [1, 2])]))

    def test_not_asked_twice_today(self):
        metrics = [_m("order", "ayam", 20, [10, 10]), _m("order", "ikan", 14, [10, 10])]
        self.assertEqual(sa.detect(metrics)["item"], "Ayam")
        self.assertEqual(sa.detect(metrics, asked_recently={("order", "ayam")})["item"], "Ikan")
        self.assertIsNone(sa.detect(metrics, asked_recently={("order", "ayam"), ("order", "ikan")}))


class WordingTests(unittest.TestCase):
    facts = sa.detect([_m("order", "ayam", 20, [10, 11, 9, 10])])

    def test_template_in_every_language_names_item_and_both_numbers(self):
        for lang in sc.LANGUAGES:
            for variant in (0, 1):
                text = sc.render_template(sa.SLOT, lang, self.facts, variant)
                self.assertIn("Ayam", text, lang)
                self.assertIn("20kg", text, lang)
                self.assertIn("10kg", text, lang)
                self.assertEqual(sc.fact_check(text, self.facts, vocabulary=VOCAB, language=lang,
                                               slot=sa.SLOT), [], (lang, text))
        sales = sa.detect([_m("sales", "Sales", 100, [300, 280], "")])
        self.assertIn("Sales jual hari ni 100, biasa 290", sc.render_template(sa.SLOT, "bm", sales))
        waste = sa.detect([_m("wastage", "ayam_goreng", 9, [3, 3], "pcs")])
        self.assertIn("thrown", sc.render_template(sa.SLOT, "english", waste))

    def test_ai_wording_must_keep_the_numbers(self):
        res = sc.build_message(sa.SLOT, "bm", self.facts, vocabulary=VOCAB,
                               complete=_ai("Ayam order 20kg hari ni, selalunya 10kg je. Kenapa?"))
        self.assertEqual(res["source"], "ai")
        res = sc.build_message(sa.SLOT, "bm", self.facts, vocabulary=VOCAB,
                               complete=_ai("Ayam order 25kg hari ni, selalunya 10kg. Kenapa?"))
        self.assertEqual(res["source"], "template")
        self.assertIn("number 25 not in data", res["problems"])
        res = sc.build_message(sa.SLOT, "bm", self.facts, vocabulary=VOCAB,
                               complete=_ai("Order hari ni 20kg, biasa 10kg. Kenapa?"))
        self.assertIn("'Ayam' not written as in the data", res["problems"])

    def test_purpose_and_prompt(self):
        purpose = sc._purpose(sa.SLOT, self.facts)
        self.assertIn("Ayam", purpose)
        self.assertIn("20kg", purpose)
        self.assertIn("why it is so high", purpose)
        seen = {}

        def capture(system, user):
            import json
            seen["user"] = json.loads(user)
            return None
        sc.build_message(sa.SLOT, "tamil", self.facts, complete=capture)
        self.assertEqual(seen["user"]["facts"]["today"], "20kg")
        self.assertIn("without blaming", seen["user"]["purpose"])

    def test_no_buttons_and_log_kind(self):
        self.assertIsNone(sl.button_set("order", self.facts))
        self.assertIsNone(sl.button_set("open", self.facts))
        self.assertEqual(sl.button_set("open", {}), "status")
        res = sc.build_message(sa.SLOT, "english", self.facts, vocabulary=VOCAB,
                               complete=_ai("Ayam ordered 20kg today, usually 10kg. Why?"))
        row = sa.log_row("order", "SEK7", -1, "Kalai", "english", self.facts, res, "natural")
        self.assertEqual((row["kind"], row["slot"], row["facts"]["deviation_pct"]),
                         ("anomaly", "order", 100))
        self.assertEqual(row["source"], "ai")


if __name__ == "__main__":
    unittest.main()
