"""Unit tests for ``uom_conversion``.

The lookup ORDER is the whole point of the module, so most of these tests pin
which of several overlapping rows wins — and the rest pin that nothing is
invented when no row matches.

Run with::

    python -m unittest tests.test_uom_conversion
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uom_conversion import (  # noqa: E402
    BASE_UNITS,
    convert_to_base,
    load_conversions,
    normalize_unit,
    resolve_conversion,
)


def row(supplier, item, unit, factor, base_unit, **extra):
    return dict(
        {
            "supplier": supplier,
            "canonical_item": item,
            "unit_raw": unit,
            "factor": factor,
            "base_unit": base_unit,
        },
        **extra,
    )


GENERIC_KG = row(None, None, "KG", 1000, "g")
ITEM_GUNI = row(None, "beras", "GUNI", 25000, "g")
SUPPLIER_GUNI = row("MEWAH", "beras", "GUNI", 10000, "g")


class NormalizeUnit(unittest.TestCase):

    def test_upper_trim_and_strip_dots(self):
        for raw in (" kg ", "Kg", "KG", "kg.", " KG. "):
            self.assertEqual(normalize_unit(raw), "KG", raw)

    def test_collapses_internal_whitespace(self):
        self.assertEqual(normalize_unit("  BOTOL   KECIL "), "BOTOL KECIL")

    def test_empty_and_non_strings_are_none(self):
        for raw in (None, "", "   ", " . ", 5, [], {}):
            self.assertIsNone(normalize_unit(raw), raw)


class ResolveConversionOrder(unittest.TestCase):

    def test_supplier_item_unit_beats_item_unit(self):
        found = resolve_conversion([ITEM_GUNI, SUPPLIER_GUNI], "MEWAH", "beras", "GUNI")
        self.assertEqual(found["factor"], 10000)

    def test_item_unit_beats_unit_only(self):
        generic = row(None, None, "GUNI", 1, "pcs")
        found = resolve_conversion([generic, ITEM_GUNI], None, "beras", "GUNI")
        self.assertEqual(found["factor"], 25000)

    def test_falls_back_to_unit_only(self):
        found = resolve_conversion([GENERIC_KG], "ANY SUPPLIER", "ayam", "KG")
        self.assertEqual(found["factor"], 1000)
        self.assertEqual(found["base_unit"], "g")

    def test_supplier_row_does_not_leak_to_other_suppliers(self):
        # Only MEWAH's GUNI is 10kg; another supplier gets no match at all
        # rather than borrowing MEWAH's factor.
        self.assertIsNone(resolve_conversion([SUPPLIER_GUNI], "FOOK LEONG", "beras", "GUNI"))

    def test_item_row_does_not_leak_to_other_items(self):
        self.assertIsNone(resolve_conversion([ITEM_GUNI], None, "ayam", "GUNI"))

    def test_no_match_returns_none(self):
        self.assertIsNone(resolve_conversion([GENERIC_KG], None, "ayam", "PAPAN"))

    def test_matching_is_case_and_space_insensitive(self):
        found = resolve_conversion([SUPPLIER_GUNI], "  mewah ", "BERAS", " guni ")
        self.assertEqual(found["factor"], 10000)

    def test_missing_unit_never_matches(self):
        for unit in (None, "", "   ", 7):
            self.assertIsNone(resolve_conversion([GENERIC_KG], None, None, unit), unit)

    def test_empty_and_non_list_conversions(self):
        self.assertIsNone(resolve_conversion([], None, None, "KG"))
        self.assertIsNone(resolve_conversion(None, None, None, "KG"))

    def test_non_dict_rows_are_ignored(self):
        self.assertIsNone(resolve_conversion(["KG", None, 3], None, None, "KG"))


class ResolveConversionRejectsBadRows(unittest.TestCase):

    def test_zero_and_negative_factors_are_skipped(self):
        for bad in (0, -1, "0"):
            self.assertIsNone(resolve_conversion([row(None, None, "KG", bad, "g")], None, None, "KG"), bad)

    def test_unparseable_factor_is_skipped(self):
        self.assertIsNone(resolve_conversion([row(None, None, "KG", "many", "g")], None, None, "KG"))

    def test_bad_base_unit_is_skipped(self):
        self.assertIsNone(resolve_conversion([row(None, None, "KG", 1000, "kg")], None, None, "KG"))

    def test_bad_specific_row_does_not_shadow_good_generic_row(self):
        # A malformed supplier-specific row must not block the valid generic
        # one — otherwise one typo silently stops converting a whole unit.
        broken = row("MEWAH", "beras", "KG", 0, "g")
        found = resolve_conversion([broken, GENERIC_KG], "MEWAH", "beras", "KG")
        self.assertEqual(found["factor"], 1000)

    def test_numeric_string_factor_is_accepted(self):
        # PostgREST returns `numeric` columns as strings.
        found = resolve_conversion([row(None, None, "KG", "1000", "g")], None, None, "KG")
        self.assertEqual(found["factor"], "1000")


class ConvertToBase(unittest.TestCase):

    def test_kg_to_grams(self):
        self.assertEqual(convert_to_base(2.5, "KG", conversions=[GENERIC_KG]), (2500.0, "g"))

    def test_string_factor_converts(self):
        rows = [row(None, None, "KG", "1000", "g")]
        self.assertEqual(convert_to_base(1, "KG", conversions=rows), (1000.0, "g"))

    def test_supplier_specific_factor_wins(self):
        qty_base, base_unit = convert_to_base(
            2, "GUNI",
            conversions=[ITEM_GUNI, SUPPLIER_GUNI],
            supplier="MEWAH", canonical_item="beras",
        )
        self.assertEqual((qty_base, base_unit), (20000.0, "g"))

    def test_unknown_unit_yields_nothing_rather_than_a_guess(self):
        self.assertEqual(convert_to_base(3, "PAPAN", conversions=[GENERIC_KG]), (None, None))

    def test_missing_qty_yields_nothing(self):
        for qty in (None, "2", True, [2]):
            self.assertEqual(convert_to_base(qty, "KG", conversions=[GENERIC_KG]), (None, None), qty)

    def test_zero_qty_still_converts(self):
        # A printed zero is data, not a missing value.
        self.assertEqual(convert_to_base(0, "KG", conversions=[GENERIC_KG]), (0.0, "g"))

    def test_no_conversions_at_all(self):
        self.assertEqual(convert_to_base(1, "KG", conversions=[]), (None, None))


class LoadConversions(unittest.TestCase):

    def test_reads_rows(self):
        class Result:
            data = [GENERIC_KG]

        class Chain:
            def select(self, *_a):
                return self

            def execute(self):
                return Result()

        class Client:
            def table(self, name):
                assert name == "uom_conversion"
                return Chain()

        self.assertEqual(load_conversions(Client()), [GENERIC_KG])

    def test_failure_degrades_to_empty(self):
        class Client:
            def table(self, _name):
                raise RuntimeError("network down")

        self.assertEqual(load_conversions(Client()), [])


class BaseUnits(unittest.TestCase):

    def test_matches_the_check_constraint(self):
        self.assertEqual(BASE_UNITS, ("g", "ml", "pcs"))


if __name__ == "__main__":
    unittest.main()
