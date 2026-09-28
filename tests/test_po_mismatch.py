"""Receipt-vs-order mismatch: comparison, question text, explanation, digest."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import po_mismatch as pm
import staff_digest as sd
import staff_live as sl
import staff_nudge as sn

# GLM OCR of the bill: ayam 10 (ordered 12), ikan as ordered, sotong as ordered
OCR_ITEMS = [
    {"name": "AYAM 1KG", "qty": 10, "price": 9.50},
    {"name": "IKAN KEMBUNG", "qty": 5, "price": 12.00},
    {"name": "SOTONG", "qty": 3, "price": 25.00},
]
PO = [{"item": "ayam", "qty": 12, "unit": "kg"},
      {"item": "ikan", "qty": 5, "unit": "kg"},
      {"item": "sotong", "qty": 3, "unit": "kg"}]
USUAL = {"ayam": 9.40, "ikan": 12.00, "sotong": 25.00}


class CompareTests(unittest.TestCase):
    def test_one_wrong_qty_lists_only_that_line(self):
        bill = pm.receipt_lines(OCR_ITEMS)
        self.assertEqual({b["item"]: b["qty"] for b in bill}, {"ayam": 10.0, "ikan": 5.0, "sotong": 3.0})
        mism = pm.compare(bill, PO, USUAL)
        self.assertEqual(mism, [{"kind": "qty", "item": "ayam", "label": "Ayam",
                                 "ordered": "12kg", "received": "10kg"}])
        text = pm.question(mism, "Bestari Farm", "bm")
        self.assertEqual(text, "Bil Bestari Farm tak sama dengan order semalam:\n"
                               "• Ayam: order 12kg, bil 10kg\n"
                               "Kenapa ya? Taip sikit.")
        self.assertNotIn("Ikan", text)
        self.assertNotIn("Sotong", text)

    def test_missing_extra_and_price(self):
        items = [{"name": "AYAM 1KG", "qty": 12, "price": 11.00},          # price +17%
                 {"name": "TELUR GRED A", "qty": 2, "price": 14.00}]       # not ordered
        mism = pm.compare(pm.receipt_lines(items), PO, USUAL)
        kinds = {(m["kind"], m["item"]) for m in mism}
        self.assertEqual(kinds, {("price", "ayam"), ("missing", "ikan"), ("missing", "sotong"),
                                 ("extra", "telur")})
        price = next(m for m in mism if m["kind"] == "price")
        self.assertEqual((price["paid"], price["usual"], price["pct"]), ("11.00", "9.40", 17))
        # 10% is the default band; a 9% rise is not a mismatch.
        self.assertEqual(pm.compare(pm.receipt_lines([{"name": "AYAM", "qty": 12, "price": 10.2}]),
                                    PO[:1], USUAL), [])
        with mock.patch.dict("os.environ", {"PO_PRICE_PCT": "5"}):
            self.assertEqual(pm.compare(pm.receipt_lines([{"name": "AYAM", "qty": 12, "price": 10.2}]),
                                        PO[:1], USUAL)[0]["kind"], "price")

    def test_matching_bill_has_nothing_to_ask(self):
        items = [{"name": "AYAM 1KG", "qty": 12, "price": 9.60},
                 {"name": "IKAN KEMBUNG", "qty": 5.1, "price": 12.00},   # 2% rounding
                 {"name": "SOTONG", "qty": 3, "price": 25.00}]
        self.assertEqual(pm.compare(pm.receipt_lines(items), PO, USUAL), [])
        self.assertEqual(pm.compare([], [], {}), [])
        # No usual price known: price is not judged.
        self.assertEqual(pm.compare(pm.receipt_lines([{"name": "AYAM", "qty": 12, "price": 99}]),
                                    PO[:1], {}), [])

    def test_question_in_every_language(self):
        mism = pm.compare(pm.receipt_lines(OCR_ITEMS), PO, USUAL)
        for lang in ("bm", "tamil", "english", "indonesian", "bengali"):
            text = pm.question(mism, "Bestari Farm", lang)
            self.assertIn("Bestari Farm", text, lang)
            self.assertIn("12kg", text, lang)
            self.assertIn("10kg", text, lang)
            self.assertEqual(text.count("•"), 1, lang)
        both = pm.question(mism, "Bestari Farm", "bm_tamil")
        self.assertEqual(both.count("•"), 2)
        self.assertIn("bill says 10kg", pm.question(mism, "X", "english"))


class ReplyTests(unittest.TestCase):
    def test_parser_status_and_explanation(self):
        self.assertIn("mismatch_explained", sl.REPLY_STATUSES)
        self.assertIn("mismatch_explained", sl.REPLY_PROMPT)
        parsed = sl.parse_reply("q", "q", "supplier hantar 10 je, 2kg esok", lambda s, u: {"data": {
            "is_answer": True, "clear": True, "summary_en": "Supplier sent 10, 2kg tomorrow",
            "status": "mismatch_explained", "explanation_en": "Supplier short by 2kg, rest tomorrow"}})
        self.assertEqual(parsed["status"], "mismatch_explained")
        self.assertEqual(pm.explanation(parsed, "raw"), "Supplier short by 2kg, rest tomorrow")
        self.assertEqual(pm.explanation({"summary_en": "S"}, "raw"), "S")
        self.assertEqual(pm.explanation(None, "raw text"), "raw text")

    def test_facts_nudges_and_digest(self):
        mism = pm.compare(pm.receipt_lines(OCR_ITEMS), PO, USUAL)
        facts = pm.facts(77, "Bestari Farm", mism)
        self.assertEqual((facts["receipt_id"], facts["lines"]), (77, "1"))
        self.assertIsNone(sl.button_set(pm.SLOT, facts))         # typed explanation
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIn(pm.SLOT, sn.slots())
        threads = [{"outlet_code": "SEK7", "slot": pm.SLOT, "status": "no_reply", "facts": facts,
                    "asked_at": "2026-09-24T12:00:00+08:00"},
                   {"outlet_code": "SEK20", "slot": pm.SLOT, "status": "answered", "facts": facts,
                    "asked_at": "2026-09-24T12:00:00+08:00", "reply_status": "mismatch_explained"}]
        self.assertEqual(pm.digest_lines(threads, lambda c: c.title()),
                         [{"outlet": "Sek7", "supplier": "Bestari Farm", "lines": "1"}])
        digest = sd.gather(threads, {"SEK7": "Sek 7", "SEK20": "Sek 20"})
        self.assertEqual(digest["po_unexplained"][0]["outlet"], "Sek 7")
        text = sd.plain(digest)
        self.assertIn("🧾 Sek 7: Bestari Farm bill differs from the order (1 lines) — no explanation", text)
        self.assertNotIn("✅ Sek 7", text)
        self.assertIn("✅ Sek 20", text)


if __name__ == "__main__":
    unittest.main()
