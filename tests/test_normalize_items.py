"""Unit tests for ``items_utils.normalize_items``.

Run with::

    python -m unittest tests.test_normalize_items
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from items_utils import normalize_items, parse_embedded_format  # noqa: E402


class NormalizeItemsStringList(unittest.TestCase):
    """The production crash case: glm-4.6v-flash returned bare strings."""

    def test_list_of_strings_becomes_list_of_dicts(self):
        self.assertEqual(
            normalize_items(["Tube Ice", "Crush Ice", "Block Ice"]),
            [
                {"name": "Tube Ice", "qty": None, "price": None},
                {"name": "Crush Ice", "qty": None, "price": None},
                {"name": "Block Ice", "qty": None, "price": None},
            ],
        )

    def test_two_string_example_from_task(self):
        self.assertEqual(
            normalize_items(["Tube Ice", "Block Ice"]),
            [
                {"name": "Tube Ice", "qty": None, "price": None},
                {"name": "Block Ice", "qty": None, "price": None},
            ],
        )

    def test_string_whitespace_is_trimmed(self):
        self.assertEqual(
            normalize_items(["  Roti  "]),
            [{"name": "Roti", "qty": None, "price": None}],
        )

    def test_empty_or_whitespace_strings_dropped(self):
        self.assertEqual(normalize_items(["", "   ", "Roti"]),
                         [{"name": "Roti", "qty": None, "price": None}])


class NormalizeItemsDictList(unittest.TestCase):
    def test_list_of_dicts_unchanged(self):
        items = [{"name": "Roti", "qty": 5, "price": 1.20}]
        self.assertEqual(normalize_items(items), items)

    def test_dict_with_only_name_kept_as_is(self):
        items = [{"name": "Tea"}]
        self.assertEqual(normalize_items(items), items)

    def test_dict_with_extra_fields_kept(self):
        items = [{"name": "Roti", "qty": 5, "price": 1.20, "category": "food"}]
        self.assertEqual(normalize_items(items), items)


class NormalizeItemsMixed(unittest.TestCase):
    def test_mixed_string_and_dict_all_become_dicts(self):
        result = normalize_items([
            "Tube Ice",
            {"name": "Roti", "qty": 5, "price": 1.20},
            "Block Ice",
        ])
        self.assertEqual(result, [
            {"name": "Tube Ice", "qty": None, "price": None},
            {"name": "Roti", "qty": 5, "price": 1.20},
            {"name": "Block Ice", "qty": None, "price": None},
        ])
        for entry in result:
            self.assertIsInstance(entry, dict)


class NormalizeItemsEmptyOrNone(unittest.TestCase):
    def test_none_returns_empty_list(self):
        self.assertEqual(normalize_items(None), [])

    def test_empty_list_returns_empty_list(self):
        self.assertEqual(normalize_items([]), [])

    def test_non_list_string_returns_empty(self):
        self.assertEqual(normalize_items("Tube Ice"), [])

    def test_non_list_dict_returns_empty(self):
        self.assertEqual(normalize_items({"name": "Roti"}), [])

    def test_non_list_int_returns_empty(self):
        self.assertEqual(normalize_items(42), [])


class NormalizeItemsSkipsBadEntries(unittest.TestCase):
    def test_int_entries_skipped(self):
        with self.assertLogs("items_utils", level="WARNING"):
            result = normalize_items([42, "Roti", 3.14])
        self.assertEqual(result, [{"name": "Roti", "qty": None, "price": None}])

    def test_none_entries_skipped(self):
        with self.assertLogs("items_utils", level="WARNING"):
            self.assertEqual(normalize_items([None, None]), [])

    def test_nested_list_entries_skipped(self):
        with self.assertLogs("items_utils", level="WARNING"):
            result = normalize_items([["nested"], {"name": "Roti"}])
        self.assertEqual(result, [{"name": "Roti"}])

    def test_clean_input_does_not_warn(self):
        # No bad entries -> no warnings emitted. Use a captured handler since
        # ``assertLogs`` requires at least one record.
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[assignment]
        logger = logging.getLogger("items_utils")
        logger.addHandler(handler)
        try:
            normalize_items(["Roti", {"name": "Tea"}])
        finally:
            logger.removeHandler(handler)
        self.assertEqual([r for r in records if r.levelno >= logging.WARNING], [])


class ParseEmbeddedFormat(unittest.TestCase):
    """Direct unit coverage of ``parse_embedded_format``.

    PR #23a: roughly half of receipts come back with qty and price trapped
    inside the name string. The parser pulls them out so the downstream
    price-history layer sees real numbers.
    """

    # 1.
    def test_basic_ayam(self):
        self.assertEqual(
            parse_embedded_format("Ayam x30 RM19.80"),
            {"clean_name": "Ayam", "qty": 30.0, "price": 19.80},
        )

    # 2.
    def test_multi_word_name_with_code(self):
        self.assertEqual(
            parse_embedded_format("super CH P8 x20 RM9.90"),
            {"clean_name": "super CH P8", "qty": 20.0, "price": 9.90},
        )

    # 3.
    def test_integer_qty_decimal_price(self):
        self.assertEqual(
            parse_embedded_format("Tembikai x7 RM3.0"),
            {"clean_name": "Tembikai", "qty": 7.0, "price": 3.0},
        )

    # 4.
    def test_decimal_qty(self):
        result = parse_embedded_format("Madu x7.2 RM5.78")
        self.assertEqual(result["clean_name"], "Madu")
        self.assertAlmostEqual(result["qty"], 7.2)
        self.assertAlmostEqual(result["price"], 5.78)

    # 5.
    def test_rightmost_wins_on_multiple_x_markers(self):
        self.assertEqual(
            parse_embedded_format("Box x2 Burger x3 RM10"),
            {"clean_name": "Box x2 Burger", "qty": 3.0, "price": 10.0},
        )

    # 6.
    def test_no_x_marker_returns_none(self):
        # "5 kg" is a unit descriptor, not a qty marker -> must not match.
        self.assertIsNone(parse_embedded_format("Mutton mysure 5 kg RM27.50"))

    # 9.
    def test_none_input_returns_none(self):
        self.assertIsNone(parse_embedded_format(None))

    # 10.
    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_embedded_format(""))
        self.assertIsNone(parse_embedded_format("   "))

    # 11.
    def test_multiple_whitespace_between_tokens(self):
        result = parse_embedded_format("Ayam  x30  RM19.80")
        self.assertEqual(result["clean_name"], "Ayam")
        self.assertAlmostEqual(result["qty"], 30.0)
        self.assertAlmostEqual(result["price"], 19.80)

    # 12.
    def test_case_insensitive_rm(self):
        result = parse_embedded_format("ayam x30 rm19.80")
        self.assertEqual(result["clean_name"], "ayam")
        self.assertAlmostEqual(result["qty"], 30.0)
        self.assertAlmostEqual(result["price"], 19.80)

    # 15.
    def test_price_without_decimal(self):
        self.assertEqual(
            parse_embedded_format("Item x5 RM10"),
            {"clean_name": "Item", "qty": 5.0, "price": 10.0},
        )

    # 16.
    def test_padded_whitespace_is_stripped(self):
        result = parse_embedded_format("  Padded Name  x5 RM10  ")
        self.assertEqual(result["clean_name"], "Padded Name")
        self.assertAlmostEqual(result["qty"], 5.0)
        self.assertAlmostEqual(result["price"], 10.0)

    # 18.
    def test_no_whitespace_before_x_is_rejected(self):
        # "Ayamx30" has no space before x, so the qty marker is ambiguous
        # with a product-code "x". We reject it rather than parse it.
        self.assertIsNone(parse_embedded_format("Ayamx30 RM19.80"))


class NormalizeItemsEmbeddedRescue(unittest.TestCase):
    """Integration: ``normalize_items`` rescues embedded-format dicts."""

    # 7.
    def test_clean_item_passes_through_unchanged(self):
        items = [{"name": "Jintan", "qty": 3, "price": 35}]
        self.assertEqual(normalize_items(items), items)

    # 8.
    def test_partial_data_with_no_embedded_pattern_passes_through(self):
        # qty present, price null, name has no xN RMX.XX pattern -> nothing
        # to rescue, keep the partial entry as-is.
        items = [{"name": "X", "qty": 5, "price": None}]
        self.assertEqual(normalize_items(items), items)

    # 8b. PR #23b bugfix: partial data + embedded pattern -> full rescue.
    # A successful embedded parse is more reliable than half-filled OCR data,
    # so the parsed values override the partial qty/price.
    def test_partial_data_qty_only_rescued_by_embedded_pattern(self):
        items = [{"name": "SOS CILI x7 RM6.30", "qty": 7, "price": None}]
        result = normalize_items(items)
        self.assertEqual(result[0]["name"], "SOS CILI")
        self.assertAlmostEqual(result[0]["qty"], 7.0)
        self.assertAlmostEqual(result[0]["price"], 6.30)

    def test_partial_data_price_only_rescued_by_embedded_pattern(self):
        items = [{"name": "SOS CILI x7 RM6.30", "qty": None, "price": 6.30}]
        result = normalize_items(items)
        self.assertEqual(result[0]["name"], "SOS CILI")
        self.assertAlmostEqual(result[0]["qty"], 7.0)
        self.assertAlmostEqual(result[0]["price"], 6.30)

    def test_partial_data_wrong_values_overridden_by_embedded_parse(self):
        # Embedded parse wins even if the partial value contradicts it.
        items = [{"name": "Ayam x30 RM19.80", "qty": 99, "price": None}]
        result = normalize_items(items)
        self.assertEqual(result[0]["name"], "Ayam")
        self.assertAlmostEqual(result[0]["qty"], 30.0)
        self.assertAlmostEqual(result[0]["price"], 19.80)

    # 13.
    def test_mixed_clean_and_embedded_both_handled(self):
        items = [
            {"name": "S Jintan Putih", "qty": 3, "price": 35},
            {"name": "Ayam x30 RM19.80", "qty": None, "price": None},
            {"name": "super CH P8 x20 RM9.90", "qty": None, "price": None},
        ]
        result = normalize_items(items)
        self.assertEqual(result[0], {"name": "S Jintan Putih", "qty": 3, "price": 35})
        self.assertEqual(result[1]["name"], "Ayam")
        self.assertAlmostEqual(result[1]["qty"], 30.0)
        self.assertAlmostEqual(result[1]["price"], 19.80)
        self.assertEqual(result[2]["name"], "super CH P8")
        self.assertAlmostEqual(result[2]["qty"], 20.0)
        self.assertAlmostEqual(result[2]["price"], 9.90)

    # 14.
    def test_all_clean_list_is_identical(self):
        items = [
            {"name": "Roti", "qty": 5, "price": 1.20},
            {"name": "Tea", "qty": 2, "price": 4.00},
            {"name": "Jintan", "qty": 3, "price": 35},
        ]
        self.assertEqual(normalize_items(items), items)

    # 17.
    def test_preserves_extra_keys_on_rescue(self):
        items = [
            {
                "qty": None,
                "name": "Ayam x30 RM19.80",
                "price": None,
                "category": "meat",
            }
        ]
        result = normalize_items(items)
        self.assertEqual(result[0]["name"], "Ayam")
        self.assertAlmostEqual(result[0]["qty"], 30.0)
        self.assertAlmostEqual(result[0]["price"], 19.80)
        self.assertEqual(result[0]["category"], "meat")

    def test_unparseable_dict_passes_through_no_raise(self):
        # Dict with no qty/price and a name that doesn't match embedded
        # pattern -> kept as-is, no exception raised.
        items = [{"name": "Mutton mysure 5 kg RM27.50", "qty": None, "price": None}]
        self.assertEqual(normalize_items(items), items)


class ParseEmbeddedFormatTrailingParenthetical(unittest.TestCase):
    """PR #23b: strip a single trailing parenthetical before the regex.

    Zhipu OCR sometimes appends commentary after the price (e.g.
    ``"... RM9.90 (amount should be RM297.00)"``); without the strip the
    embedded-qty regex refuses to match. Parentheticals in the MIDDLE of
    the name (e.g. ``"DISH WSH (HIJAU) x6 RM11.2"``) must be preserved.
    """

    def test_trailing_paren_amount_commentary_is_stripped(self):
        result = parse_embedded_format("SuperCH x30 RM9.90 (amount should be RM297.00)")
        self.assertEqual(result["clean_name"], "SuperCH")
        self.assertAlmostEqual(result["qty"], 30.0)
        self.assertAlmostEqual(result["price"], 9.90)

    def test_trailing_paren_short_form_is_stripped(self):
        result = parse_embedded_format("Super H x20 RM9.90 (amount: RM290.07)")
        self.assertEqual(result["clean_name"], "Super H")
        self.assertAlmostEqual(result["qty"], 20.0)
        self.assertAlmostEqual(result["price"], 9.90)

    def test_mid_name_parenthetical_is_preserved(self):
        # "(HIJAU)" sits between the name and the qty marker -> not
        # trailing -> must stay inside the clean_name.
        result = parse_embedded_format("DISH WSH (HIJAU) x6 RM11.2")
        self.assertEqual(result["clean_name"], "DISH WSH (HIJAU)")
        self.assertAlmostEqual(result["qty"], 6.0)
        self.assertAlmostEqual(result["price"], 11.2)

    def test_no_parenthetical_unchanged(self):
        # Sanity check: strings without any parens still parse the same.
        result = parse_embedded_format("Ayam x30 RM19.80")
        self.assertEqual(result["clean_name"], "Ayam")
        self.assertAlmostEqual(result["qty"], 30.0)
        self.assertAlmostEqual(result["price"], 19.80)

    def test_trailing_paren_with_trailing_whitespace(self):
        # Whitespace between price and paren, and after the paren.
        result = parse_embedded_format("Roti x2 RM3.50   (note)   ")
        self.assertEqual(result["clean_name"], "Roti")
        self.assertAlmostEqual(result["qty"], 2.0)
        self.assertAlmostEqual(result["price"], 3.50)

    def test_rescue_via_normalize_items_with_trailing_paren(self):
        # End-to-end: a dict with embedded format AND trailing OCR
        # commentary still gets rescued cleanly.
        items = [
            {
                "name": "SuperCH x30 RM9.90 (amount should be RM297.00)",
                "qty": None,
                "price": None,
            }
        ]
        result = normalize_items(items)
        self.assertEqual(result[0]["name"], "SuperCH")
        self.assertAlmostEqual(result[0]["qty"], 30.0)
        self.assertAlmostEqual(result[0]["price"], 9.90)


if __name__ == "__main__":
    unittest.main()
