"""Hermetic tests for scripts/uom_seeding_worklist.

The whole shadow run is driven end to end here — receipt selection, image
recovery, OCR, miss detection, ranking, CSV — with the network replaced by
fakes. What is NOT faked is the miss test itself: it goes through the real
``uom_conversion.resolve_conversion``, so a change to the lookup order shows up
as a changed worklist.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import uom_seeding_worklist as wl  # noqa: E402
from tests.fake_supabase import FakeSupabase  # noqa: E402

KG = {"supplier": None, "canonical_item": None, "unit_raw": "KG",
      "factor": 1000, "base_unit": "g"}
EKOR = {"supplier": None, "canonical_item": None, "unit_raw": "EKOR",
        "factor": 1, "base_unit": "pcs"}


def line(no, item, qty=None, unit=None, unit_price=None, line_total=None):
    return {"line_no": no, "item": item, "qty": qty, "unit": unit,
            "unit_price": unit_price, "line_total": line_total}


def invoice(lines, subtotal=None, supplier=None):
    return {"supplier": supplier, "invoice_no": None, "invoice_date": None,
            "lines": lines, "subtotal": subtotal, "total": subtotal}


class CollectMisses(unittest.TestCase):

    def test_seeded_units_are_not_listed(self):
        misses, stats = wl.collect_misses(
            {"merchant": "BESTARI FARM"},
            invoice([line(1, "AYAM", 10, "KG", 9.8, 98.0)]),
            [KG],
        )
        self.assertEqual(misses, [])
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["lines"], 1)

    def test_unseeded_unit_is_listed_with_its_money(self):
        misses, _ = wl.collect_misses(
            {"merchant": "MEWAH TRADING"},
            invoice([line(1, "BERAS SUPER", 4, "GUNI", 128.5, 514.0)]),
            [KG],
        )
        self.assertEqual(len(misses), 1)
        self.assertEqual(misses[0], {
            "supplier": "MEWAH TRADING",
            "canonical_item": wl.UNCATEGORISED,   # no 'beras' category in v2
            "unit_raw": "GUNI",
            "cost": 514.0,
        })

    def test_canonical_item_is_reported_when_recognised(self):
        misses, _ = wl.collect_misses(
            {"merchant": "X"},
            invoice([line(1, "SANTAN 1 KG", 2, "CTN", 60.0, 120.0)]),
            [KG],
        )
        self.assertEqual(misses[0]["canonical_item"], "santan")

    def test_unit_is_upper_cased_so_kg_and_Kg_are_one_row(self):
        misses, _ = wl.collect_misses(
            {"merchant": "X"},
            invoice([line(1, "AYAM", 1, " guni ", 1.0, 10.0),
                     line(2, "AYAM", 1, "GUNI", 1.0, 10.0)]),
            [KG],
        )
        self.assertEqual({m["unit_raw"] for m in misses}, {"GUNI"})

    def test_line_with_no_printed_unit_is_counted_not_listed(self):
        # There is nothing to seed for a line that printed no unit.
        misses, stats = wl.collect_misses(
            {"merchant": "X"},
            invoice([line(1, "AYAM", 1, None, 10.0, 10.0)]),
            [KG],
        )
        self.assertEqual(misses, [])
        self.assertEqual(stats["no_unit"], 1)

    def test_unreadable_qty_still_reports_the_unit(self):
        # The unit needs a factor whether or not this particular quantity was
        # legible — using convert_to_base here would have hidden it.
        misses, _ = wl.collect_misses(
            {"merchant": "X"},
            invoice([line(1, "AYAM", None, "PAPAN", None, 30.0)]),
            [KG],
        )
        self.assertEqual(misses[0]["unit_raw"], "PAPAN")

    def test_supplier_specific_seed_only_silences_that_supplier(self):
        mewah_guni = {"supplier": "MEWAH", "canonical_item": "santan",
                      "unit_raw": "GUNI", "factor": 10000, "base_unit": "g"}
        seeded, _ = wl.collect_misses(
            {"merchant": "MEWAH"},
            invoice([line(1, "SANTAN", 1, "GUNI", 1.0, 100.0)]),
            [mewah_guni],
        )
        other, _ = wl.collect_misses(
            {"merchant": "FOOK LEONG"},
            invoice([line(1, "SANTAN", 1, "GUNI", 1.0, 100.0)]),
            [mewah_guni],
        )
        self.assertEqual(seeded, [])
        self.assertEqual(len(other), 1)

    def test_cost_falls_back_to_qty_times_unit_price(self):
        misses, _ = wl.collect_misses(
            {"merchant": "X"},
            invoice([line(1, "AYAM", 4, "GUNI", 25.0, None)]),
            [KG],
        )
        self.assertEqual(misses[0]["cost"], 100.0)

    def test_line_with_no_money_at_all_contributes_zero(self):
        misses, _ = wl.collect_misses(
            {"merchant": "X"}, invoice([line(1, "AYAM", None, "GUNI")]), [KG]
        )
        self.assertEqual(misses[0]["cost"], 0.0)

    def test_receipt_merchant_wins_over_the_ocr_header(self):
        misses, _ = wl.collect_misses(
            {"merchant": "BESTARI FARM"},
            invoice([line(1, "AYAM", 1, "GUNI", 1.0, 1.0)], supplier="BESTAR1 FRM"),
            [KG],
        )
        self.assertEqual(misses[0]["supplier"], "BESTARI FARM")

    def test_missing_merchant_becomes_unknown_not_none(self):
        misses, _ = wl.collect_misses(
            {"merchant": None}, invoice([line(1, "AYAM", 1, "GUNI", 1.0, 1.0)]), [KG]
        )
        self.assertEqual(misses[0]["supplier"], "UNKNOWN")

    def test_failed_subtotal_still_reports_units_but_is_flagged(self):
        # The live pipeline withholds quantities here; the printed units are
        # still real and still need seeding.
        misses, stats = wl.collect_misses(
            {"merchant": "X"},
            invoice([line(1, "AYAM", 1, "GUNI", 10.0, 10.0)], subtotal=500.0),
            [KG],
        )
        self.assertEqual(len(misses), 1)
        self.assertTrue(stats["subtotal_mismatch"])

    def test_empty_and_malformed_invoices_are_safe(self):
        for parsed in (invoice([]), {"lines": None}, {}):
            misses, stats = wl.collect_misses({"merchant": "X"}, parsed, [KG])
            self.assertEqual(misses, [])
            self.assertEqual(stats["lines"], 0)

    def test_junk_line_entries_are_skipped(self):
        misses, _ = wl.collect_misses(
            {"merchant": "X"},
            invoice(["AYAM 4 GUNI", None, line(1, "  ", 1, "GUNI"),
                     line(2, "AYAM", 1, "GUNI", 1.0, 5.0)]),
            [KG],
        )
        self.assertEqual(len(misses), 1)


class RankWorklist(unittest.TestCase):

    def test_groups_and_sums(self):
        rows = wl.rank_worklist([
            {"supplier": "A", "canonical_item": "ayam", "unit_raw": "GUNI", "cost": 100.0},
            {"supplier": "A", "canonical_item": "ayam", "unit_raw": "GUNI", "cost": 50.0},
        ])
        self.assertEqual(rows, [{
            "supplier": "A", "canonical_item": "ayam", "unit_raw": "GUNI",
            "line_count": 2, "total_RM": 150.0,
        }])

    def test_sorted_by_money_descending(self):
        rows = wl.rank_worklist([
            {"supplier": "A", "canonical_item": "x", "unit_raw": "U1", "cost": 10.0},
            {"supplier": "B", "canonical_item": "y", "unit_raw": "U2", "cost": 900.0},
            {"supplier": "C", "canonical_item": "z", "unit_raw": "U3", "cost": 300.0},
        ])
        self.assertEqual([r["total_RM"] for r in rows], [900.0, 300.0, 10.0])

    def test_different_units_stay_separate_rows(self):
        rows = wl.rank_worklist([
            {"supplier": "A", "canonical_item": "ayam", "unit_raw": "GUNI", "cost": 10.0},
            {"supplier": "A", "canonical_item": "ayam", "unit_raw": "PAPAN", "cost": 10.0},
        ])
        self.assertEqual(len(rows), 2)

    def test_ties_break_deterministically(self):
        misses = [
            {"supplier": "Z", "canonical_item": "a", "unit_raw": "U", "cost": 5.0},
            {"supplier": "A", "canonical_item": "a", "unit_raw": "U", "cost": 5.0},
        ]
        first = wl.rank_worklist(misses)
        second = wl.rank_worklist(list(reversed(misses)))
        self.assertEqual(first, second)
        self.assertEqual(first[0]["supplier"], "A")

    def test_empty_input(self):
        self.assertEqual(wl.rank_worklist([]), [])


class BuildCsv(unittest.TestCase):

    def test_header_and_row_shape(self):
        csv_text = wl.build_csv(wl.rank_worklist([
            {"supplier": "MEWAH", "canonical_item": "santan", "unit_raw": "CTN", "cost": 514.0},
        ]))
        lines = csv_text.splitlines()
        self.assertEqual(lines[0], "supplier,canonical_item,unit_raw,line_count,total_RM")
        self.assertEqual(lines[1], "MEWAH,santan,CTN,1,514.00")

    def test_money_always_has_two_decimals(self):
        csv_text = wl.build_csv(wl.rank_worklist([
            {"supplier": "A", "canonical_item": "x", "unit_raw": "U", "cost": 7.0},
        ]))
        self.assertTrue(csv_text.splitlines()[1].endswith(",7.00"))

    def test_commas_in_supplier_names_are_quoted(self):
        csv_text = wl.build_csv(wl.rank_worklist([
            {"supplier": "AH SENG, SDN BHD", "canonical_item": "x", "unit_raw": "U", "cost": 1.0},
        ]))
        self.assertIn('"AH SENG, SDN BHD"', csv_text)

    def test_empty_worklist_still_has_a_header(self):
        self.assertEqual(
            wl.build_csv([]).strip(), "supplier,canonical_item,unit_raw,line_count,total_RM"
        )


class RunEndToEnd(unittest.TestCase):
    """Drive the full run over a fake DB and a fake OCR."""

    def setUp(self):
        wl._today_my = lambda: __import__("datetime").date(2026, 8, 3)
        self.client = FakeSupabase()
        self.client.table("uom_conversion").insert([KG, EKOR]).execute()
        self.client.table("receipts").insert([
            {"id": 1, "merchant": "MEWAH TRADING", "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": "2026-08-01", "image_url": "https://img/1"},
            {"id": 2, "merchant": "BESTARI FARM", "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": "2026-07-20", "image_url": "https://img/2"},
            # Out of window.
            {"id": 3, "merchant": "OLD CO", "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": "2026-01-01", "image_url": "https://img/3"},
            # Wrong type.
            {"id": 4, "merchant": "TNB", "receipt_type": "UTILITY",
             "receipt_date": "2026-08-01", "image_url": "https://img/4"},
        ]).execute()
        self.invoices = {
            1: invoice([line(1, "BERAS SUPER", 4, "GUNI", 128.5, 514.0),
                        line(2, "AYAM", 10, "KG", 9.8, 98.0)], subtotal=612.0),
            2: invoice([line(1, "TELUR GRED A", 3, "PAPAN", 15.0, 45.0),
                        line(2, "AYAM KAMPUNG", 5, "EKOR", 22.0, 110.0)], subtotal=155.0),
        }
        self.ocr_calls = []

    def _run(self, **kwargs):
        def fake_image(row):
            return f"bytes-for-{row['id']}".encode()

        def fake_ocr(image_bytes):
            receipt_id = int(image_bytes.decode().rsplit("-", 1)[1])
            self.ocr_calls.append(receipt_id)
            return self.invoices[receipt_id]

        return wl.run(
            self.client, image_fetcher=fake_image, ocr_call=fake_ocr, **kwargs
        )

    def test_only_supplier_purchases_in_window_are_ocred(self):
        _rows, stats = self._run(days=60)
        self.assertEqual(sorted(self.ocr_calls), [1, 2])
        self.assertEqual(stats["receipts_selected"], 2)
        self.assertEqual(stats["receipts_ocred"], 2)

    def test_worklist_ranks_the_unseeded_units_by_money(self):
        rows, _stats = self._run(days=60)
        self.assertEqual(
            [(r["supplier"], r["unit_raw"], r["total_RM"]) for r in rows],
            [("MEWAH TRADING", "GUNI", 514.0), ("BESTARI FARM", "PAPAN", 45.0)],
        )

    def test_seeded_units_never_reach_the_worklist(self):
        rows, _stats = self._run(days=60)
        listed = {r["unit_raw"] for r in rows}
        self.assertNotIn("KG", listed)
        self.assertNotIn("EKOR", listed)

    def test_csv_is_the_deliverable(self):
        rows, _stats = self._run(days=60)
        self.assertEqual(
            wl.build_csv(rows).splitlines(),
            [
                "supplier,canonical_item,unit_raw,line_count,total_RM",
                "MEWAH TRADING,UNCATEGORISED,GUNI,1,514.00",
                "BESTARI FARM,UNCATEGORISED,PAPAN,1,45.00",
            ],
        )

    def test_dry_run_spends_nothing(self):
        rows, stats = self._run(days=60, dry_run=True)
        self.assertEqual(self.ocr_calls, [])
        self.assertEqual(rows, [])
        self.assertEqual(stats["receipts_selected"], 2)

    def test_max_calls_caps_the_run_and_says_so(self):
        _rows, stats = self._run(days=60, max_calls=1)
        self.assertEqual(len(self.ocr_calls), 1)
        self.assertEqual(stats["truncated"], 1)
        self.assertIn("lower bound", wl.format_report(_rows, stats))

    def test_receipt_with_no_recoverable_image_is_counted_not_fatal(self):
        def no_image(row):
            return None if row["id"] == 1 else b"bytes-for-2"

        rows, stats = wl.run(
            self.client, days=60, image_fetcher=no_image,
            ocr_call=lambda b: self.invoices[2],
        )
        self.assertEqual(stats["receipts_no_image"], 1)
        self.assertEqual(stats["receipts_ocred"], 1)
        self.assertEqual(len(rows), 1)

    def test_one_ocr_failure_does_not_end_the_run(self):
        def flaky(image_bytes):
            receipt_id = int(image_bytes.decode().rsplit("-", 1)[1])
            if receipt_id == 1:
                raise RuntimeError("model timeout")
            return self.invoices[receipt_id]

        rows, stats = wl.run(
            self.client, days=60,
            image_fetcher=lambda r: f"bytes-for-{r['id']}".encode(),
            ocr_call=flaky,
        )
        self.assertEqual(stats["receipts_ocr_failed"], 1)
        self.assertEqual(stats["receipts_ocred"], 1)
        self.assertEqual([r["unit_raw"] for r in rows], ["PAPAN"])

    def test_empty_conversion_table_refuses_to_produce_a_worklist(self):
        # Otherwise the output would tell you to go and measure a kilogram.
        empty = FakeSupabase()
        empty.table("receipts").insert([]).execute()
        with self.assertRaises(SystemExit) as ctx:
            wl.run(empty, days=60)
        self.assertIn("0038", str(ctx.exception))

    def test_empty_conversion_table_can_be_overridden(self):
        empty = FakeSupabase()
        empty.table("receipts").insert([
            {"id": 1, "merchant": "M", "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": "2026-08-01", "image_url": "u"},
        ]).execute()
        rows, _stats = wl.run(
            empty, days=60, allow_empty_conversions=True,
            image_fetcher=lambda r: b"x",
            ocr_call=lambda b: invoice([line(1, "AYAM", 1, "KG", 10.0, 10.0)]),
        )
        self.assertEqual(rows[0]["unit_raw"], "KG")

    def test_report_mentions_coverage_and_skips(self):
        rows, stats = self._run(days=60)
        report = wl.format_report(rows, stats)
        self.assertIn("last 60 days", report)
        self.assertIn("2 selected", report)
        self.assertIn("Top 10 by RM", report)

    def test_report_when_nothing_is_unconvertible(self):
        self.invoices = {
            1: invoice([line(1, "AYAM", 10, "KG", 9.8, 98.0)]),
            2: invoice([line(1, "AYAM", 5, "EKOR", 22.0, 110.0)]),
        }
        rows, stats = self._run(days=60)
        self.assertEqual(rows, [])
        self.assertIn("no seeding needed", wl.format_report(rows, stats))


class ReadOnlyGuarantee(unittest.TestCase):
    """The script must not be able to write anything but its CSV."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "uom_seeding_worklist.py",
        )
        with open(path) as f:
            cls.src = f.read()

    def test_no_write_operations_against_supabase(self):
        # sys.path.insert is the only legitimate ".insert(" in the file.
        body = self.src.replace("sys.path.insert(", "")
        for op in (".insert(", ".upsert(", ".update(", ".delete(", ".rpc("):
            self.assertNotIn(op, body, op)

    def test_never_imports_the_persistence_layer(self):
        self.assertNotIn("save_receipt_items", self.src)
        self.assertNotIn("build_item_rows", self.src)

    def test_uses_the_production_ocr_path(self):
        # A worklist built by a different prompt would seed the wrong factors.
        self.assertIn("from invoice_ocr import call_line_items_ocr", self.src)

    def test_uses_the_real_lookup_for_miss_detection(self):
        self.assertIn("from uom_conversion import UOM_CONVERSION_TABLE, resolve_conversion", self.src)


if __name__ == "__main__":
    unittest.main()
