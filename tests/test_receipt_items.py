"""Unit tests for ``receipt_items``.

The invariant under test throughout: a quantity is only ever written when a
real conversion row said so. Every other path leaves ``qty_base`` NULL and
flags the line, and none of them invents a factor.

Run with::

    python -m unittest tests.test_receipt_items
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402
from receipt_items import (  # noqa: E402
    DEFAULT_CONFIDENCE,
    build_item_rows,
    line_confidence,
    save_receipt_items,
    unconvertible_units,
)

KG = {"supplier": None, "canonical_item": None, "unit_raw": "KG",
      "factor": 1000, "base_unit": "g"}
EKOR = {"supplier": None, "canonical_item": None, "unit_raw": "EKOR",
        "factor": 1, "base_unit": "pcs"}
CONVERSIONS = [KG, EKOR]


def invoice(lines, subtotal=None, **extra):
    return dict(
        {"supplier": "BESTARI FARM", "invoice_no": "INV-1",
         "invoice_date": "2026-08-02", "lines": lines,
         "subtotal": subtotal, "total": subtotal},
        **extra,
    )


def ocr_line(no, item, qty=None, unit=None, unit_price=None, line_total=None):
    return {"line_no": no, "item": item, "qty": qty, "unit": unit,
            "unit_price": unit_price, "line_total": line_total}


def build(lines, subtotal=None, conversions=CONVERSIONS, **kw):
    return build_item_rows(
        invoice(lines, subtotal),
        receipt_id=kw.pop("receipt_id", 77),
        outlet=kw.pop("outlet", "Klang B.Emas"),
        conversions=conversions,
        **kw,
    )


class BuildItemRows(unittest.TestCase):

    def test_clean_invoice_converts_every_line(self):
        rows = build(
            [ocr_line(1, "AYAM SEGAR", 12.5, "KG", 9.8, 122.5),
             ocr_line(2, "AYAM KAMPUNG", 6, "EKOR", 22.0, 132.0)],
            subtotal=254.5,
        )
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["canonical_item"], "ayam")
        self.assertEqual(first["qty_base"], 12500.0)
        self.assertEqual(first["base_unit"], "g")
        self.assertFalse(first["needs_review"])
        self.assertEqual(second["qty_base"], 6.0)
        self.assertEqual(second["base_unit"], "pcs")
        self.assertFalse(second["needs_review"])

    def test_row_carries_the_receipt_identity(self):
        rows = build([ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)], subtotal=10.0)
        row = rows[0]
        self.assertEqual(row["receipt_id"], 77)
        self.assertEqual(row["outlet"], "Klang B.Emas")
        self.assertEqual(row["supplier"], "BESTARI FARM")
        self.assertEqual(row["invoice_date"], "2026-08-02")
        self.assertEqual(row["line_no"], 1)

    def test_unit_is_stored_exactly_as_printed(self):
        rows = build([ocr_line(1, "AYAM", 2, " kg ", 10.0, 20.0)], subtotal=20.0)
        # Stored verbatim (trimmed) even though the lookup folded it to match.
        self.assertEqual(rows[0]["unit_raw"], "kg")
        self.assertEqual(rows[0]["qty_base"], 2000.0)

    def test_caller_values_override_the_second_ocr_pass(self):
        rows = build(
            [ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)],
            subtotal=10.0,
            supplier="FOOK LEONG",       # the reconciled receipt's merchant
            invoice_date="2026-07-31",   # the reconciled receipt's date
        )
        self.assertEqual(rows[0]["supplier"], "FOOK LEONG")
        self.assertEqual(rows[0]["invoice_date"], "2026-07-31")


class NeverGuessesAFactor(unittest.TestCase):

    def test_unknown_unit_leaves_qty_base_null_and_flags(self):
        rows = build([ocr_line(1, "BERAS", 2, "GUNI", 128.5, 257.0)], subtotal=257.0)
        row = rows[0]
        self.assertIsNone(row["qty_base"])
        self.assertIsNone(row["base_unit"])
        self.assertTrue(row["needs_review"])
        # The money and the printed unit survive — only the conversion is absent.
        self.assertEqual(row["qty"], 2.0)
        self.assertEqual(row["unit_raw"], "GUNI")
        self.assertEqual(row["line_total"], 257.0)

    def test_missing_unit_leaves_qty_base_null_and_flags(self):
        rows = build([ocr_line(1, "AYAM", 3, None, 10.0, 30.0)], subtotal=30.0)
        self.assertIsNone(rows[0]["qty_base"])
        self.assertTrue(rows[0]["needs_review"])

    def test_missing_qty_leaves_qty_base_null_and_flags(self):
        rows = build([ocr_line(1, "AYAM", None, "KG", None, 30.0)], subtotal=30.0)
        self.assertIsNone(rows[0]["qty_base"])
        self.assertTrue(rows[0]["needs_review"])

    def test_no_conversion_table_at_all_flags_everything(self):
        rows = build([ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)], subtotal=10.0, conversions=[])
        self.assertIsNone(rows[0]["qty_base"])
        self.assertTrue(rows[0]["needs_review"])


class SubtotalMismatchWithholdsQuantities(unittest.TestCase):

    def setUp(self):
        # Printed subtotal is RM300; the lines only add to RM254.50, so a row
        # was misread or missed — which row is unknowable.
        self.rows = build(
            [ocr_line(1, "AYAM SEGAR", 12.5, "KG", 9.8, 122.5),
             ocr_line(2, "AYAM KAMPUNG", 6, "EKOR", 22.0, 132.0)],
            subtotal=300.0,
        )

    def test_no_line_gets_a_base_quantity(self):
        for row in self.rows:
            self.assertIsNone(row["qty_base"], row)
            self.assertIsNone(row["base_unit"], row)

    def test_every_line_is_flagged(self):
        self.assertTrue(all(r["needs_review"] for r in self.rows))

    def test_the_printed_values_are_still_stored(self):
        self.assertEqual(self.rows[0]["qty"], 12.5)
        self.assertEqual(self.rows[0]["unit_raw"], "KG")
        self.assertEqual(self.rows[0]["line_total"], 122.5)

    def test_a_supplied_check_result_is_honoured(self):
        rows = build_item_rows(
            invoice([ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)], subtotal=10.0),
            receipt_id=1, outlet="Vista", conversions=CONVERSIONS,
            subtotal_check={"needs_review": True},
        )
        self.assertIsNone(rows[0]["qty_base"])
        self.assertTrue(rows[0]["needs_review"])

    def test_no_printed_subtotal_does_not_withhold_quantities(self):
        # An unrun check is not a failed check.
        rows = build([ocr_line(1, "AYAM", 2, "KG", 10.0, 20.0)], subtotal=None)
        self.assertEqual(rows[0]["qty_base"], 2000.0)
        self.assertFalse(rows[0]["needs_review"])


class LineLevelArithmetic(unittest.TestCase):

    def test_line_that_does_not_multiply_out_is_flagged(self):
        # 2 x 10.00 should be 20.00, not 35.00 — but the invoice subtotal
        # matches the printed line total, so only this line is suspect.
        rows = build([ocr_line(1, "AYAM", 2, "KG", 10.0, 35.0)], subtotal=35.0)
        self.assertTrue(rows[0]["needs_review"])
        # The quantity still converted: the unit is known and the qty printed.
        self.assertEqual(rows[0]["qty_base"], 2000.0)

    def test_consistent_line_is_not_flagged(self):
        rows = build([ocr_line(1, "AYAM", 2, "KG", 10.0, 20.0)], subtotal=20.0)
        self.assertFalse(rows[0]["needs_review"])


class Canonicalisation(unittest.TestCase):

    def test_unknown_item_is_kept_with_a_null_canonical(self):
        rows = build([ocr_line(1, "WIDGET 3000", 1, "KG", 5.0, 5.0)], subtotal=5.0)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["canonical_item"])
        self.assertEqual(rows[0]["raw_item_text"], "WIDGET 3000")
        # The conversion still ran: an unrecognised name is not an unknown unit.
        self.assertEqual(rows[0]["qty_base"], 1000.0)

    def test_canonical_scopes_the_conversion_lookup(self):
        # A per-item CTN factor applies to santan and to nothing else: a
        # carton of santan is 12 x 1kg, a carton of anything else is unknown.
        santan_ctn = {"supplier": None, "canonical_item": "santan",
                      "unit_raw": "CTN", "factor": 12000, "base_unit": "g"}
        rows = build(
            [ocr_line(1, "SANTAN 1 KG", 2, "CTN", 128.5, 257.0),
             ocr_line(2, "AYAM", 1, "CTN", 50.0, 50.0)],
            subtotal=307.0,
            conversions=[santan_ctn],
        )
        self.assertEqual(rows[0]["canonical_item"], "santan")
        self.assertEqual(rows[0]["qty_base"], 24000.0)
        self.assertEqual(rows[1]["canonical_item"], "ayam")
        self.assertIsNone(rows[1]["qty_base"])


class EmptyAndMalformedInput(unittest.TestCase):

    def test_no_lines_yields_no_rows(self):
        self.assertEqual(build([]), [])
        self.assertEqual(build_item_rows({}, receipt_id=1, outlet="V", conversions=[]), [])
        self.assertEqual(build_item_rows(None, receipt_id=1, outlet="V", conversions=[]), [])

    def test_lines_without_item_text_are_skipped(self):
        rows = build([ocr_line(1, "  "), {"line_no": 2}, "junk",
                      ocr_line(3, "AYAM", 1, "KG", 10.0, 10.0)], subtotal=10.0)
        self.assertEqual([r["raw_item_text"] for r in rows], ["AYAM"])

    def test_non_numeric_cells_become_null(self):
        line = ocr_line(1, "AYAM", "12", "KG", None, None)
        rows = build([line])
        self.assertIsNone(rows[0]["qty"])
        self.assertIsNone(rows[0]["unit_price"])
        self.assertIsNone(rows[0]["line_total"])


class LineConfidence(unittest.TestCase):

    def _score(self, base=100, **overrides):
        kwargs = dict(
            has_qty=True, has_unit=True, has_unit_price=True,
            has_line_total=True, has_canonical=True, has_conversion=True,
            arithmetic_bad=False, subtotal_bad=False,
        )
        kwargs.update(overrides)
        return line_confidence(base, **kwargs)

    def test_perfect_line_keeps_the_receipt_score(self):
        self.assertEqual(self._score(), 100)

    def test_each_gap_costs_something(self):
        for flag in ("has_qty", "has_unit", "has_unit_price",
                     "has_line_total", "has_canonical", "has_conversion"):
            self.assertLess(self._score(**{flag: False}), 100, flag)

    def test_contradictions_cost_more_than_omissions(self):
        self.assertLess(self._score(subtotal_bad=True), self._score(has_unit_price=False))

    def test_score_is_clamped_to_the_0_100_range(self):
        worst = line_confidence(
            10, has_qty=False, has_unit=False, has_unit_price=False,
            has_line_total=False, has_canonical=False, has_conversion=False,
            arithmetic_bad=True, subtotal_bad=True,
        )
        self.assertEqual(worst, 0)
        self.assertEqual(self._score(base=500), 100)

    def test_missing_receipt_score_falls_back_to_the_default(self):
        for base in (None, "high", True):
            self.assertEqual(
                line_confidence(
                    base, has_qty=True, has_unit=True, has_unit_price=True,
                    has_line_total=True, has_canonical=True, has_conversion=True,
                    arithmetic_bad=False, subtotal_bad=False,
                ),
                DEFAULT_CONFIDENCE,
                base,
            )

    def test_built_rows_carry_a_score(self):
        rows = build([ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)],
                     subtotal=10.0, base_confidence=90)
        self.assertEqual(rows[0]["confidence"], 90)


class UnconvertibleUnits(unittest.TestCase):

    def test_lists_distinct_unconverted_units(self):
        rows = build(
            [ocr_line(1, "BERAS", 2, "GUNI", 1.0, 2.0),
             ocr_line(2, "TELUR", 3, "papan", 1.0, 3.0),
             ocr_line(3, "BERAS", 1, "guni", 1.0, 1.0),
             ocr_line(4, "AYAM", 1, "KG", 1.0, 1.0)],
            subtotal=7.0,
        )
        self.assertEqual(unconvertible_units(rows), ["GUNI", "PAPAN"])

    def test_converted_and_unitless_rows_are_not_listed(self):
        rows = build([ocr_line(1, "AYAM", 1, "KG", 1.0, 1.0),
                      ocr_line(2, "AYAM", 1, None, 1.0, 1.0)], subtotal=2.0)
        self.assertEqual(unconvertible_units(rows), [])

    def test_empty_input(self):
        self.assertEqual(unconvertible_units([]), [])
        self.assertEqual(unconvertible_units(None), [])


class SaveReceiptItems(unittest.TestCase):

    def test_writes_rows(self):
        client = FakeSupabase()
        rows = build([ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)], subtotal=10.0)
        self.assertEqual(save_receipt_items(client, rows), 1)
        stored = client.rows("receipt_items")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["raw_item_text"], "AYAM")

    def test_reprocessing_the_same_receipt_replaces_its_lines(self):
        client = FakeSupabase()
        save_receipt_items(client, build([ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)], subtotal=10.0))
        save_receipt_items(client, build([ocr_line(1, "AYAM SEGAR", 2, "KG", 10.0, 20.0)], subtotal=20.0))
        stored = client.rows("receipt_items")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["qty_base"], 2000.0)

    def test_empty_rows_is_a_no_op(self):
        client = FakeSupabase()
        self.assertEqual(save_receipt_items(client, []), 0)
        self.assertEqual(client.rows("receipt_items"), [])

    def test_write_failure_returns_zero_instead_of_raising(self):
        class Boom:
            def table(self, _name):
                raise RuntimeError("upstream down")

        rows = build([ocr_line(1, "AYAM", 1, "KG", 10.0, 10.0)], subtotal=10.0)
        self.assertEqual(save_receipt_items(Boom(), rows), 0)


if __name__ == "__main__":
    unittest.main()
