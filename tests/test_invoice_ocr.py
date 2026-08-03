"""Unit tests for ``invoice_ocr``.

Two things are pinned here: the PROMPT contract (the rules that stop the model
merging lines or normalising units are the whole reason this pass exists), and
the parser's refusal to fall over on a malformed reply — an OCR hiccup must
never take down the receipt pipeline.

Run with::

    python -m unittest tests.test_invoice_ocr
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_ocr import LINE_ITEMS_PROMPT, parse_line_items_response  # noqa: E402


GOOD_RESPONSE = {
    "supplier": "BESTARI FARM SDN BHD",
    "invoice_no": "INV-0993",
    "invoice_date": "02/08/2026",
    "lines": [
        {"line_no": 1, "item": "AYAM STANDARD", "qty": 12.5, "unit": "KG",
         "unit_price": 9.8, "line_total": 122.5},
        {"line_no": 2, "item": "AYAM KAMPUNG", "qty": 6, "unit": "EKOR",
         "unit_price": 22.0, "line_total": 132.0},
    ],
    "subtotal": 254.5,
    "total": 254.5,
}


class PromptContract(unittest.TestCase):

    def test_declares_every_required_key(self):
        for key in ("supplier", "invoice_no", "invoice_date", "lines",
                    "subtotal", "total", "line_no", "item", "qty", "unit",
                    "unit_price", "line_total"):
            self.assertIn(f'"{key}"', LINE_ITEMS_PROMPT, key)

    def test_forbids_merging_and_splitting_lines(self):
        self.assertIn("ONE object per printed line", LINE_ITEMS_PROMPT)
        self.assertIn("Do not merge", LINE_ITEMS_PROMPT)
        self.assertIn("do not split", LINE_ITEMS_PROMPT)

    def test_requires_the_unit_verbatim(self):
        self.assertIn("EXACTLY as printed", LINE_ITEMS_PROMPT)
        for unit in ("KG", "EKOR", "GUNI", "PAPAN", "CTN", "BTL", "PKT"):
            self.assertIn(unit, LINE_ITEMS_PROMPT, unit)
        self.assertIn("Do not translate it", LINE_ITEMS_PROMPT)

    def test_forbids_inventing_missing_numbers(self):
        self.assertIn("never compute a missing value yourself", LINE_ITEMS_PROMPT)

    def test_demands_json_only(self):
        self.assertIn("Return ONLY a JSON object", LINE_ITEMS_PROMPT)
        self.assertIn("No markdown", LINE_ITEMS_PROMPT)


class ParseWellFormed(unittest.TestCase):

    def test_full_response(self):
        parsed = parse_line_items_response(json.dumps(GOOD_RESPONSE))
        self.assertEqual(parsed["supplier"], "BESTARI FARM SDN BHD")
        self.assertEqual(parsed["invoice_no"], "INV-0993")
        self.assertEqual(parsed["invoice_date"], "2026-08-02")  # normalised
        self.assertEqual(parsed["subtotal"], 254.5)
        self.assertEqual(parsed["total"], 254.5)
        self.assertEqual(len(parsed["lines"]), 2)
        self.assertEqual(parsed["lines"][1], {
            "line_no": 2, "item": "AYAM KAMPUNG", "qty": 6.0, "unit": "EKOR",
            "unit_price": 22.0, "line_total": 132.0,
        })

    def test_unit_case_is_preserved_verbatim(self):
        payload = {"lines": [{"line_no": 1, "item": "BERAS", "unit": " guni "}]}
        parsed = parse_line_items_response(json.dumps(payload))
        # Only whitespace is trimmed — folding is uom_conversion's job, and
        # unit_raw must show what the paper actually said.
        self.assertEqual(parsed["lines"][0]["unit"], "guni")

    def test_code_fences_are_stripped(self):
        wrapped = "```json\n" + json.dumps(GOOD_RESPONSE) + "\n```"
        self.assertEqual(len(parse_line_items_response(wrapped)["lines"]), 2)

    def test_prose_around_the_object_is_tolerated(self):
        noisy = "Here is the invoice:\n" + json.dumps(GOOD_RESPONSE) + "\nHope that helps!"
        self.assertEqual(len(parse_line_items_response(noisy)["lines"]), 2)

    def test_currency_and_thousand_separators_in_numbers(self):
        payload = {
            "lines": [{"line_no": 1, "item": "BERAS", "qty": "10",
                       "unit": "GUNI", "unit_price": "RM 128.50",
                       "line_total": "1,285.00"}],
            "subtotal": "RM1,285.00",
        }
        parsed = parse_line_items_response(json.dumps(payload))
        self.assertEqual(parsed["lines"][0]["unit_price"], 128.5)
        self.assertEqual(parsed["lines"][0]["line_total"], 1285.0)
        self.assertEqual(parsed["subtotal"], 1285.0)


class ParseDegradesSafely(unittest.TestCase):

    def _assert_empty(self, parsed):
        self.assertEqual(parsed["lines"], [])
        for key in ("supplier", "invoice_no", "invoice_date", "subtotal", "total"):
            self.assertIsNone(parsed[key], key)

    def test_empty_and_non_string_input(self):
        for content in (None, "", "   ", 42, {"lines": []}):
            self._assert_empty(parse_line_items_response(content))

    def test_unparseable_text(self):
        self._assert_empty(parse_line_items_response("I could not read this invoice."))

    def test_broken_json_object(self):
        self._assert_empty(parse_line_items_response('{"lines": [{"item": '))

    def test_json_array_instead_of_object(self):
        self._assert_empty(parse_line_items_response('[{"item": "AYAM"}]'))

    def test_lines_not_a_list(self):
        parsed = parse_line_items_response('{"supplier": "X", "lines": "AYAM 12 KG"}')
        self.assertEqual(parsed["lines"], [])
        self.assertEqual(parsed["supplier"], "X")

    def test_line_without_item_text_is_dropped(self):
        payload = {"lines": [
            {"line_no": 1, "item": "  "},
            {"line_no": 2, "qty": 3},
            {"line_no": 3, "item": "AYAM"},
        ]}
        parsed = parse_line_items_response(json.dumps(payload))
        self.assertEqual([l["item"] for l in parsed["lines"]], ["AYAM"])

    def test_non_dict_line_entries_are_dropped(self):
        payload = {"lines": ["AYAM 12 KG", None, {"line_no": 1, "item": "IKAN"}]}
        parsed = parse_line_items_response(json.dumps(payload))
        self.assertEqual([l["item"] for l in parsed["lines"]], ["IKAN"])

    def test_unreadable_numbers_become_null_not_zero(self):
        payload = {"lines": [{"line_no": 1, "item": "AYAM", "qty": "??",
                              "unit_price": "smudged", "line_total": None}]}
        line = parse_line_items_response(json.dumps(payload))["lines"][0]
        self.assertIsNone(line["qty"])
        self.assertIsNone(line["unit_price"])
        self.assertIsNone(line["line_total"])

    def test_bad_invoice_date_becomes_null(self):
        payload = {"invoice_date": "sometime in August", "lines": []}
        self.assertIsNone(parse_line_items_response(json.dumps(payload))["invoice_date"])


class LineNumbering(unittest.TestCase):

    def test_missing_line_no_falls_back_to_position(self):
        payload = {"lines": [{"item": "A"}, {"item": "B"}, {"item": "C"}]}
        parsed = parse_line_items_response(json.dumps(payload))
        self.assertEqual([l["line_no"] for l in parsed["lines"]], [1, 2, 3])

    def test_duplicate_line_numbers_are_renumbered(self):
        # (receipt_id, line_no) is unique in receipt_items — a duplicate would
        # otherwise silently overwrite a real line.
        payload = {"lines": [
            {"line_no": 1, "item": "A"},
            {"line_no": 1, "item": "B"},
            {"line_no": 1, "item": "C"},
        ]}
        parsed = parse_line_items_response(json.dumps(payload))
        numbers = [l["line_no"] for l in parsed["lines"]]
        self.assertEqual(len(set(numbers)), 3)
        self.assertEqual([l["item"] for l in parsed["lines"]], ["A", "B", "C"])

    def test_zero_and_negative_line_numbers_fall_back(self):
        payload = {"lines": [{"line_no": 0, "item": "A"}, {"line_no": -4, "item": "B"}]}
        parsed = parse_line_items_response(json.dumps(payload))
        self.assertEqual([l["line_no"] for l in parsed["lines"]], [1, 2])

    def test_printed_order_is_preserved(self):
        payload = {"lines": [
            {"line_no": 7, "item": "A"},
            {"line_no": 3, "item": "B"},
        ]}
        parsed = parse_line_items_response(json.dumps(payload))
        self.assertEqual([l["item"] for l in parsed["lines"]], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
