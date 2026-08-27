"""Unit tests for ``invoices_export`` (/invoices_export <outlet> <YYYY-MM>).

Hermetic — the pure helpers take plain values, and the reads run against
the shared in-memory ``FakeSupabase`` double. The Telegram side (album
send, gate) is checked source-level against bot.py at the bottom, the way
``test_order_drafts_wiring`` does, because bot.py can't be imported without
telegram/supabase and env vars.

Run with::

    python -m unittest tests.test_invoices_export
"""

import csv
import io
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402

import invoices_export  # noqa: E402
from invoices_export import (  # noqa: E402
    RECONCILE_TOLERANCE,
    build_export,
    distinct_outlets,
    format_empty,
    format_outlet_choice,
    format_summary,
    line_sum,
    match_outlets,
    month_label,
    month_window,
    parse_month,
    receipt_lines,
    reconciles,
    reconciliation_ratio,
    to_number,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def receipt(receipt_id, *, outlet="Bistro", merchant="BESTARI FARM",
            receipt_date="2026-07-15", total=100.0, items=None):
    return {
        "id": receipt_id,
        "outlet": outlet,
        "merchant": merchant,
        "receipt_date": receipt_date,
        "total": total,
        "items": [] if items is None else items,
    }


def client_with(*receipts):
    client = FakeSupabase()
    for row in receipts:
        client.table("receipts").insert(row).execute()
    return client


class RangeRecorder:
    """Wraps a client and records every ``.range()`` window it is asked for,
    so a test can prove the month was WALKED rather than slurped in one go."""

    def __init__(self, inner):
        self._inner = inner
        self.ranges = []

    def table(self, name):
        return _RecordingQuery(self._inner.table(name), self.ranges)


class _RecordingQuery:
    def __init__(self, inner, ranges):
        self._inner = inner
        self._ranges = ranges

    def __getattr__(self, attr):
        method = getattr(self._inner, attr)

        def wrapper(*args, **kwargs):
            if attr == "range":
                self._ranges.append(args)
            result = method(*args, **kwargs)
            # Builder calls return the inner query; keep recording on it.
            return self if result is self._inner else result

        return wrapper


def has_code_line(src, text):
    """True when ``text`` starts a real line of code — so the assertion
    fails if the line is commented out rather than removed."""
    return any(line.strip().startswith(text) for line in src.splitlines())


def read_csv(blob):
    """Decode one produced CSV back into a list of rows."""
    return list(csv.reader(io.StringIO(blob.decode("utf-8-sig"))))


def files_by_kind(export):
    """``{"suppliers": rows, "bills": rows, "items": rows}`` from an export."""
    out = {}
    for name, blob in export["files"]:
        kind = name.rsplit("_", 1)[-1].removesuffix(".csv")
        out[kind] = read_csv(blob)
    return out


# --- argument parsing --------------------------------------------------------

class ParseMonth(unittest.TestCase):
    def test_valid_month(self):
        self.assertEqual(parse_month("2026-07"), (2026, 7))
        self.assertEqual(parse_month("  2026-12 "), (2026, 12))

    def test_rejects_loose_and_invalid_forms(self):
        for bad in ("2026-7", "2026-13", "2026-00", "07-2026", "2026",
                    "last", "", None, "2026-07-01", "abcd-ef", "0000-07"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_month(bad))

    def test_label_is_zero_padded(self):
        self.assertEqual(month_label(2026, 7), "2026-07")


class MonthWindow(unittest.TestCase):
    def test_window_is_half_open(self):
        self.assertEqual(month_window(2026, 7), ("2026-07-01", "2026-08-01"))

    def test_december_rolls_the_year(self):
        self.assertEqual(month_window(2026, 12), ("2026-12-01", "2027-01-01"))

    def test_short_and_leap_february(self):
        # The half-open upper bound never has to know 28 vs 29.
        self.assertEqual(month_window(2026, 2), ("2026-02-01", "2026-03-01"))
        self.assertEqual(month_window(2024, 2), ("2024-02-01", "2024-03-01"))


# --- outlet matching ---------------------------------------------------------

class MatchOutlets(unittest.TestCase):
    OUTLETS = ["Bistro", "Klang B.Emas", "One Bistro", "SEK-20", "Vista"]

    def test_zero_matches(self):
        self.assertEqual(match_outlets(self.OUTLETS, "jakel"), [])

    def test_exactly_one_match(self):
        self.assertEqual(match_outlets(self.OUTLETS, "vist"), ["Vista"])

    def test_many_matches_are_all_returned(self):
        # "bistro" is a whole outlet name AND a substring of another; the
        # command must stop and list both rather than pick one.
        self.assertEqual(
            match_outlets(self.OUTLETS, "bistro"), ["Bistro", "One Bistro"]
        )

    def test_matching_is_case_insensitive_both_ways(self):
        self.assertEqual(match_outlets(["VISTA ALAM"], "vista"), ["VISTA ALAM"])
        self.assertEqual(match_outlets(["vista alam"], "VISTA"), ["vista alam"])

    def test_blank_term_matches_nothing(self):
        for term in ("", "   ", None):
            with self.subTest(term=term):
                self.assertEqual(match_outlets(self.OUTLETS, term), [])

    def test_term_is_trimmed(self):
        self.assertEqual(match_outlets(self.OUTLETS, "  vista  "), ["Vista"])


class DistinctOutlets(unittest.TestCase):
    def test_dedupes_and_sorts_case_insensitively(self):
        client = client_with(
            receipt(1, outlet="Vista"),
            receipt(2, outlet="Bistro"),
            receipt(3, outlet="Vista"),
            receipt(4, outlet="one bistro"),
        )
        self.assertEqual(
            distinct_outlets(client), ["Bistro", "one bistro", "Vista"]
        )

    def test_blank_and_missing_outlets_are_skipped(self):
        client = client_with(
            receipt(1, outlet="Vista"),
            receipt(2, outlet=""),
            receipt(3, outlet="   "),
            receipt(4, outlet=None),
        )
        self.assertEqual(distinct_outlets(client), ["Vista"])

    def test_unknown_bucket_is_offered_not_hidden(self):
        client = client_with(receipt(1, outlet="UNKNOWN"), receipt(2, outlet="Vista"))
        self.assertEqual(distinct_outlets(client), ["UNKNOWN", "Vista"])

    def test_names_are_offered_exactly_as_stored(self):
        # build_export filters with .eq on whatever is picked here, so a
        # tidied-up name would offer a shop whose export came back empty.
        client = client_with(receipt(1, outlet="  Vista  "), receipt(2, outlet="Vista"))
        offered = distinct_outlets(client)
        self.assertEqual(sorted(offered), ["  Vista  ", "Vista"])
        for name in offered:
            export = build_export(client, name, 2026, 7)
            self.assertEqual(export["stats"]["bill_count"], 1, f"{name!r} not exportable")


# --- items jsonb -------------------------------------------------------------

class ToNumber(unittest.TestCase):
    def test_real_json_numbers_pass_through(self):
        self.assertEqual(to_number(30), 30.0)
        self.assertEqual(to_number(7.2), 7.2)
        self.assertEqual(to_number(0), 0.0)

    def test_plain_decimal_strings_are_cast(self):
        self.assertEqual(to_number("3"), 3.0)
        self.assertEqual(to_number("19.80"), 19.8)
        self.assertEqual(to_number(" 2 "), 2.0)

    def test_anything_else_is_unusable(self):
        for bad in ("2 pcs", "RM19.80", "-3", "1,5", "1.2.3", "", "   ",
                    ".5", "3.", "1e3", "+3", None, True, False, [], {}, "3\n4"):
            with self.subTest(bad=bad):
                self.assertIsNone(to_number(bad))

    def test_trailing_newline_does_not_slip_through_the_anchors(self):
        self.assertEqual(to_number("3\n"), 3.0)  # trimmed, then matched

    def test_oversized_json_integer_is_blank_not_an_exception(self):
        # float(10**400) raises OverflowError; a bad row must never raise.
        self.assertIsNone(to_number(10 ** 400))

    def test_digits_too_long_for_a_float_do_not_become_inf(self):
        # Passes the numeric gate, casts to inf — not a number to report.
        self.assertIsNone(to_number("1" + "0" * 400))

    def test_non_finite_floats_are_unusable(self):
        self.assertIsNone(to_number(float("inf")))
        self.assertIsNone(to_number(float("-inf")))
        self.assertIsNone(to_number(float("nan")))


class ReceiptLines(unittest.TestCase):
    def test_item_key_variant(self):
        self.assertEqual(
            receipt_lines([{"item": "AYAM", "qty": 2, "price": 10}]),
            [("AYAM", 2.0, 10.0)],
        )

    def test_name_key_variant(self):
        self.assertEqual(
            receipt_lines([{"name": "IKAN", "quantity": "3", "price": "5.5"}]),
            [("IKAN", 3.0, 5.5)],
        )

    def test_item_wins_over_name_when_both_present(self):
        self.assertEqual(
            receipt_lines([{"item": "AYAM", "name": "CHICKEN", "qty": 1, "price": 1}]),
            [("AYAM", 1.0, 1.0)],
        )

    def test_null_item_falls_through_to_name(self):
        self.assertEqual(
            receipt_lines([{"item": None, "name": "TELUR", "qty": 1, "price": 2}]),
            [("TELUR", 1.0, 2.0)],
        )

    def test_empty_string_item_does_not_fall_through(self):
        # coalesce is on presence, like SQL's over ->>: an empty string is
        # a value, so "name" is not consulted.
        self.assertEqual(
            receipt_lines([{"item": "", "name": "TELUR", "qty": 1, "price": 2}]),
            [("", 1.0, 2.0)],
        )

    def test_qty_wins_over_quantity_and_does_not_fall_through(self):
        # A present-but-unusable qty stays unusable — it must NOT quietly
        # fall back to quantity, or the export would invent numbers.
        self.assertEqual(
            receipt_lines([{"name": "X", "qty": "2 pcs", "quantity": 3, "price": 1}]),
            [("X", None, 1.0)],
        )

    def test_null_qty_falls_through_to_quantity(self):
        self.assertEqual(
            receipt_lines([{"name": "X", "qty": None, "quantity": 4, "price": 1}]),
            [("X", 4.0, 1.0)],
        )

    def test_null_and_non_numeric_qty_price_become_none(self):
        self.assertEqual(
            receipt_lines([
                {"name": "A", "qty": None, "price": None},
                {"name": "B", "qty": "banyak", "price": "RM10"},
                {"name": "C", "qty": 2, "price": None},
            ]),
            [("A", None, None), ("B", None, None), ("C", 2.0, None)],
        )

    def test_names_are_trimmed_and_coerced(self):
        self.assertEqual(
            receipt_lines([{"name": "  AYAM  ", "qty": 1, "price": 1},
                           {"item": 12345, "qty": 1, "price": 1}]),
            [("AYAM", 1.0, 1.0), ("12345", 1.0, 1.0)],
        )

    def test_string_entries_keep_their_text(self):
        # glm-4.6v-flash still emits bare strings on terse receipts.
        self.assertEqual(
            receipt_lines(["Tube Ice", " Block Ice "]),
            [("Tube Ice", None, None), ("Block Ice", None, None)],
        )

    def test_embedded_qty_is_not_rescued(self):
        # items_utils rescues this at ingest; the export shows what is
        # stored rather than re-deriving numbers from the name.
        self.assertEqual(
            receipt_lines([{"name": "Ayam x30 RM19.80", "qty": None, "price": None}]),
            [("Ayam x30 RM19.80", None, None)],
        )

    def test_empty_items_array(self):
        self.assertEqual(receipt_lines([]), [])

    def test_non_list_items_never_raise(self):
        for bad in (None, "AYAM", {"name": "AYAM"}, 7):
            with self.subTest(bad=bad):
                self.assertEqual(receipt_lines(bad), [])

    def test_junk_entries_are_dropped_not_fatal(self):
        self.assertEqual(
            receipt_lines([None, 5, ["a"], {"name": "OK", "qty": 1, "price": 1}]),
            [("OK", 1.0, 1.0)],
        )


# --- reconciliation ----------------------------------------------------------

class LineSum(unittest.TestCase):
    def test_sums_qty_times_unit_price(self):
        self.assertEqual(
            line_sum([("A", 2.0, 10.0), ("B", 3.0, 5.0)]), (35.0, 2)
        )

    def test_half_filled_lines_contribute_nothing(self):
        self.assertEqual(
            line_sum([("A", 2.0, 10.0), ("B", None, 5.0), ("C", 3.0, None)]),
            (20.0, 1),
        )

    def test_no_usable_line_is_none_not_zero(self):
        # None means "can't tell", 0.0 would mean "the bill bought nothing".
        self.assertEqual(line_sum([("A", None, None)]), (None, 0))
        self.assertEqual(line_sum([]), (None, 0))

    def test_zero_priced_line_is_still_usable(self):
        self.assertEqual(line_sum([("A", 2.0, 0.0)]), (0.0, 1))


class ReconciliationRatio(unittest.TestCase):
    def test_exact_match_is_one(self):
        self.assertEqual(reconciliation_ratio(100.0, 100.0), 1.0)

    def test_over_counted_lines(self):
        self.assertAlmostEqual(reconciliation_ratio(180.0, 100.0), 1.8)

    def test_under_counted_lines(self):
        self.assertAlmostEqual(reconciliation_ratio(40.0, 50.0), 0.8)

    def test_unusable_when_there_is_nothing_to_divide(self):
        self.assertIsNone(reconciliation_ratio(None, 100.0))
        self.assertIsNone(reconciliation_ratio(100.0, None))
        self.assertIsNone(reconciliation_ratio(100.0, 0.0))
        self.assertIsNone(reconciliation_ratio(None, None))

    def test_never_raises_on_junk(self):
        self.assertIsNone(reconciliation_ratio("abc", 100.0))
        self.assertIsNone(reconciliation_ratio(100.0, "abc"))


class Reconciles(unittest.TestCase):
    def test_within_tolerance_inclusive(self):
        self.assertTrue(reconciles(1.0))
        self.assertTrue(reconciles(1.0 + RECONCILE_TOLERANCE))
        self.assertTrue(reconciles(1.0 - RECONCILE_TOLERANCE))
        self.assertTrue(reconciles(0.96))

    def test_outside_tolerance(self):
        self.assertFalse(reconciles(1.06))
        self.assertFalse(reconciles(0.9))
        self.assertFalse(reconciles(1.8))

    def test_unusable_is_not_a_reconciliation(self):
        self.assertFalse(reconciles(None))

    def test_tolerance_is_taken_on_the_exact_ratio_not_the_printed_one(self):
        # 105.4 / 100 prints as 1.05 in the CSV but is 5.4% out, so it must
        # not be counted as reconciled.
        ratio = reconciliation_ratio(105.4, 100.0)
        self.assertEqual(f"{ratio:.2f}", "1.05")
        self.assertFalse(reconciles(ratio))


# --- the export --------------------------------------------------------------

class BuildExportMonthWindow(unittest.TestCase):
    def test_only_the_named_month_is_exported(self):
        client = client_with(
            receipt(1, receipt_date="2026-06-30", total=1.0),   # before
            receipt(2, receipt_date="2026-07-01", total=2.0),   # first day
            receipt(3, receipt_date="2026-07-31", total=3.0),   # last day
            receipt(4, receipt_date="2026-08-01", total=4.0),   # next month
        )
        export = build_export(client, "Bistro", 2026, 7)
        bills = files_by_kind(export)["bills"][1:]
        self.assertEqual([r[0] for r in bills], ["2026-07-01", "2026-07-31"])
        self.assertEqual(export["stats"]["bill_count"], 2)

    def test_december_export_does_not_leak_into_january(self):
        client = client_with(
            receipt(1, receipt_date="2026-12-31", total=5.0),
            receipt(2, receipt_date="2027-01-01", total=9.0),
        )
        export = build_export(client, "Bistro", 2026, 12)
        self.assertEqual(export["stats"]["bill_count"], 1)
        self.assertEqual(export["stats"]["total_spend"], 5.0)

    def test_february_last_day_is_kept(self):
        client = client_with(receipt(1, receipt_date="2026-02-28", total=5.0))
        self.assertEqual(
            build_export(client, "Bistro", 2026, 2)["stats"]["bill_count"], 1
        )

    def test_other_outlets_are_not_mixed_in(self):
        client = client_with(
            receipt(1, outlet="Bistro", total=10.0),
            receipt(2, outlet="One Bistro", total=99.0),
        )
        export = build_export(client, "Bistro", 2026, 7)
        self.assertEqual(export["stats"]["total_spend"], 10.0)


class BuildExportFiles(unittest.TestCase):
    def setUp(self):
        self.client = client_with(
            receipt(1, merchant="BESTARI FARM", receipt_date="2026-07-20",
                    total=100.0,
                    items=[{"item": "AYAM", "qty": 2, "price": 50}]),
            receipt(2, merchant="PASAR BORONG", receipt_date="2026-07-02",
                    total=300.0,
                    items=[{"name": "IKAN", "quantity": "3", "price": "100"}]),
            receipt(3, merchant="BESTARI FARM", receipt_date="2026-07-10",
                    total=50.0, items=[]),
        )
        self.export = build_export(self.client, "Bistro", 2026, 7)
        self.csvs = files_by_kind(self.export)

    def test_three_files_named_after_outlet_and_month(self):
        self.assertEqual(
            [name for name, _ in self.export["files"]],
            ["bistro_2026-07_suppliers.csv",
             "bistro_2026-07_bills.csv",
             "bistro_2026-07_items.csv"],
        )

    def test_suppliers_headers_and_ordering(self):
        rows = self.csvs["suppliers"]
        self.assertEqual(rows[0], ["merchant", "bill_count", "total_amount"])
        # PASAR BORONG (300) outranks BESTARI FARM (150) despite fewer bills.
        self.assertEqual(rows[1:], [
            ["PASAR BORONG", "1", "300.00"],
            ["BESTARI FARM", "2", "150.00"],
        ])

    def test_bills_headers_and_date_ordering(self):
        rows = self.csvs["bills"]
        self.assertEqual(rows[0], ["receipt_date", "merchant", "total", "receipt_id"])
        self.assertEqual([r[0] for r in rows[1:]],
                         ["2026-07-02", "2026-07-10", "2026-07-20"])
        self.assertEqual(rows[1], ["2026-07-02", "PASAR BORONG", "300.00", "2"])

    def test_items_headers_and_ratio_column(self):
        rows = self.csvs["items"]
        self.assertEqual(rows[0], [
            "receipt_date", "merchant", "item", "qty", "price",
            "bill_total", "receipt_id", "line_sum_vs_total",
        ])
        by_item = {r[2]: r for r in rows[1:]}
        # 2 x 50 = 100 against a 100 total.
        self.assertEqual(by_item["AYAM"],
                         ["2026-07-20", "BESTARI FARM", "AYAM", "2", "50.00",
                          "100.00", "1", "1.00"])
        # 3 x 100 = 300 against a 300 total.
        self.assertEqual(by_item["IKAN"][3:], ["3", "100.00", "300.00", "2", "1.00"])

    def test_receipt_with_empty_items_contributes_no_item_rows(self):
        rows = self.csvs["items"][1:]
        self.assertEqual([r[6] for r in rows], ["1", "2"])  # receipt 3 absent

    def test_stats(self):
        stats = self.export["stats"]
        self.assertEqual(stats["bill_count"], 3)
        self.assertEqual(stats["total_spend"], 450.0)
        self.assertEqual(stats["reconciled_count"], 2)
        self.assertEqual(stats["unusable_count"], 1)  # the empty-items bill

    def test_csvs_are_utf8_with_a_bom_for_excel(self):
        for _name, blob in self.export["files"]:
            self.assertTrue(blob.startswith(b"\xef\xbb\xbf"))


class BuildExportEdgeCases(unittest.TestCase):
    def test_empty_month_produces_no_files(self):
        export = build_export(client_with(), "Bistro", 2026, 7)
        self.assertEqual(export["files"], [])
        self.assertEqual(export["stats"]["bill_count"], 0)
        self.assertEqual(export["stats"]["total_spend"], 0.0)

    def test_over_counted_lines_are_flagged_not_hidden(self):
        client = client_with(receipt(
            1, total=100.0,
            items=[{"name": "AYAM", "qty": 2, "price": 90}],
        ))
        export = build_export(client, "Bistro", 2026, 7)
        items = files_by_kind(export)["items"][1:]
        self.assertEqual(items[0][-1], "1.80")
        self.assertEqual(export["stats"]["reconciled_count"], 0)
        self.assertEqual(export["stats"]["unusable_count"], 0)

    def test_unusable_lines_leave_the_ratio_blank(self):
        client = client_with(receipt(
            1, total=100.0,
            items=[{"name": "AYAM", "qty": "banyak", "price": "RM90"}],
        ))
        export = build_export(client, "Bistro", 2026, 7)
        row = files_by_kind(export)["items"][1]
        self.assertEqual(row[3:5], ["", ""])       # qty, price
        self.assertEqual(row[-1], "")              # line_sum_vs_total
        self.assertEqual(export["stats"]["unusable_count"], 1)

    def test_missing_bill_total_is_blank_and_unusable(self):
        client = client_with(receipt(
            1, total=None, items=[{"name": "AYAM", "qty": 2, "price": 50}],
        ))
        export = build_export(client, "Bistro", 2026, 7)
        csvs = files_by_kind(export)
        self.assertEqual(csvs["bills"][1][2], "")
        self.assertEqual(csvs["items"][1][-1], "")
        self.assertEqual(export["stats"]["unusable_count"], 1)
        self.assertEqual(export["stats"]["total_spend"], 0.0)

    def test_string_totals_are_normalised(self):
        client = client_with(receipt(1, total="RM1,234.50", items=[]))
        export = build_export(client, "Bistro", 2026, 7)
        self.assertEqual(export["stats"]["total_spend"], 1234.5)
        self.assertEqual(files_by_kind(export)["bills"][1][2], "1234.50")

    def test_blank_merchant_falls_back_to_unknown(self):
        client = client_with(receipt(1, merchant=None, total=10.0, items=[]))
        rows = files_by_kind(build_export(client, "Bistro", 2026, 7))["suppliers"]
        self.assertEqual(rows[1][0], "UNKNOWN")

    def test_decimal_quantities_survive_the_round_trip(self):
        client = client_with(receipt(
            1, total=72.0, items=[{"name": "KAMBING", "qty": 7.2, "price": 10}],
        ))
        row = files_by_kind(build_export(client, "Bistro", 2026, 7))["items"][1]
        self.assertEqual(row[3], "7.2")

    def test_outlet_name_is_slugified_for_the_filename(self):
        client = client_with(receipt(1, outlet="Klang B.Emas", total=1.0, items=[]))
        export = build_export(client, "Klang B.Emas", 2026, 7)
        self.assertEqual(export["files"][0][0], "klang_b_emas_2026-07_suppliers.csv")

    def test_commas_quotes_and_newlines_in_names_stay_one_field(self):
        # OCR routinely produces names with commas; csv must quote them or
        # every downstream column shifts by one.
        client = client_with(receipt(
            1, merchant='PASAR "BORONG", KL', total=10.0,
            items=[{"name": "AYAM,\nPAHA", "qty": 1, "price": 10}],
        ))
        csvs = files_by_kind(build_export(client, "Bistro", 2026, 7))
        self.assertEqual(csvs["suppliers"][1][0], 'PASAR "BORONG", KL')
        self.assertEqual(csvs["items"][1][2], "AYAM,\nPAHA")
        self.assertEqual(len(csvs["items"][1]), len(csvs["items"][0]))

    def test_same_day_bills_keep_receipt_id_order(self):
        # The date sort is stable over an id-ordered read, so 2 comes before
        # 10 — a str() tiebreak on the id would have inverted them.
        client = client_with(
            receipt(2, receipt_date="2026-07-05", total=1.0, items=[]),
            receipt(10, receipt_date="2026-07-05", total=2.0, items=[]),
            receipt(3, receipt_date="2026-07-04", total=3.0, items=[]),
        )
        rows = files_by_kind(build_export(client, "Bistro", 2026, 7))["bills"][1:]
        self.assertEqual([r[3] for r in rows], ["3", "2", "10"])

    def test_date_objects_are_rendered_as_iso(self):
        # Defensive branch: PostgREST hands back ISO strings, but a date
        # object must not reach the CSV as "datetime.date(2026, 7, 9)".
        self.assertEqual(invoices_export._date_cell(date(2026, 7, 9)), "2026-07-09")
        self.assertEqual(invoices_export._date_cell("2026-07-09T00:00:00"), "2026-07-09")
        self.assertEqual(invoices_export._date_cell(None), "")

    def test_a_bill_with_several_usable_lines_sums_them_all(self):
        # Every other fixture has one line per receipt; the ratio has to be
        # right when the sum spans lines, including an unusable one.
        client = client_with(receipt(1, total=100.0, items=[
            {"name": "AYAM", "qty": 2, "price": 30},        # 60
            {"item": "IKAN", "quantity": "1", "price": "40"},  # 40
            {"name": "PLASTIK", "qty": "banyak", "price": "RM2"},  # unusable
        ]))
        export = build_export(client, "Bistro", 2026, 7)
        rows = files_by_kind(export)["items"][1:]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r[-1] for r in rows], ["1.00", "1.00", "1.00"])
        self.assertEqual(export["stats"]["reconciled_count"], 1)
        self.assertEqual(export["stats"]["unusable_count"], 0)

    def test_junk_rows_never_raise(self):
        client = client_with(
            receipt(1, total=10.0, items="not a list"),
            receipt(2, total=20.0, items=None),
            receipt(3, total=20.0, items=[None, "Ice"]),
        )
        export = build_export(client, "Bistro", 2026, 7)
        self.assertEqual(export["stats"]["bill_count"], 3)
        self.assertEqual(export["stats"]["unusable_count"], 3)


class BuildExportStreaming(unittest.TestCase):
    def _paged_export(self, rows, page_size):
        spy = RangeRecorder(client_with(*rows))
        original = invoices_export.PAGE_SIZE
        invoices_export.PAGE_SIZE = page_size
        try:
            return spy, build_export(spy, "Bistro", 2026, 7)
        finally:
            invoices_export.PAGE_SIZE = original

    def test_month_is_walked_in_pages_not_slurped(self):
        rows = [
            receipt(i, receipt_date=f"2026-07-{i:02d}", total=10.0,
                    items=[{"name": "AYAM", "qty": 1, "price": 10}])
            for i in range(1, 8)
        ]
        spy, export = self._paged_export(rows, page_size=2)
        # 7 rows, 2 per page: four windows, the short last one ends the walk.
        # An implementation that read the month in one shot records none.
        self.assertEqual(spy.ranges, [(0, 1), (2, 3), (4, 5), (6, 7)])
        self.assertEqual(export["stats"]["bill_count"], 7)
        self.assertEqual(len(files_by_kind(export)["items"]) - 1, 7)

    def test_rows_spanning_a_page_boundary_are_all_exported(self):
        # The bug a page walk hides: rows after the first window vanishing.
        rows = [receipt(i, receipt_date=f"2026-07-{i:02d}", total=float(i),
                        items=[{"name": "X", "qty": 1, "price": float(i)}])
                for i in range(1, 6)]
        _spy, export = self._paged_export(rows, page_size=2)
        bills = files_by_kind(export)["bills"][1:]
        self.assertEqual([r[3] for r in bills], ["1", "2", "3", "4", "5"])
        self.assertEqual(export["stats"]["total_spend"], 15.0)


# --- replies -----------------------------------------------------------------

class FormatOutletChoice(unittest.TestCase):
    def test_zero_matches_lists_what_is_on_record(self):
        text = format_outlet_choice("jakel", [], ["Bistro", "Vista"])
        self.assertIn('No outlet matches "jakel".', text)
        self.assertIn("• Bistro", text)
        self.assertIn("• Vista", text)

    def test_zero_matches_with_no_outlets_at_all(self):
        text = format_outlet_choice("jakel", [], [])
        self.assertEqual(text, 'No outlet matches "jakel".')
        self.assertNotIn("Outlets on record", text)

    def test_many_matches_lists_only_the_candidates(self):
        text = format_outlet_choice("bistro", ["Bistro", "One Bistro"],
                                    ["Bistro", "One Bistro", "Vista"])
        self.assertIn("matches 2 outlets", text)
        self.assertIn("• Bistro", text)
        self.assertIn("• One Bistro", text)
        self.assertNotIn("Vista", text)


class FormatSummary(unittest.TestCase):
    def test_reports_the_four_headline_numbers(self):
        text = format_summary("Bistro", 2026, 7, {
            "bill_count": 42, "total_spend": 12345.67,
            "reconciled_count": 30, "unusable_count": 5,
        })
        self.assertIn("Bistro • 2026-07", text)
        self.assertIn("Bills: 42", text)
        self.assertIn("RM12,345.67", text)
        self.assertIn("(±5%): 30 of 42", text)
        self.assertIn("No usable line data: 5 bill(s)", text)

    def test_missing_stats_do_not_raise(self):
        self.assertIn("Bills: 0", format_summary("Bistro", 2026, 7, {}))

    def test_empty_month_message(self):
        self.assertEqual(
            format_empty("Bistro", 2026, 7), "No receipts for Bistro in 2026-07."
        )


# --- bot.py wiring -----------------------------------------------------------

class InvoicesExportWiring(unittest.TestCase):
    """Source-level checks, like ``test_order_drafts_wiring``: bot.py needs
    telegram/supabase and env vars that aren't present in CI/dev."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()
        start = cls.src.index("async def invoices_export_command(")
        cls.block = cls.src[start:cls.src.index("\nasync def ", start + 1)]

    def test_command_is_registered(self):
        self.assertTrue(has_code_line(
            self.src,
            'app.add_handler(CommandHandler("invoices_export", invoices_export_command))',
        ), "registration missing or commented out")

    def test_documented_in_help(self):
        self.assertIn("/invoices_export <outlet> <YYYY-MM>", self.src)

    def test_gated_to_reviewers_like_the_other_admin_commands(self):
        self.assertTrue(has_code_line(
            self.block, "if not message or not is_reviewer(_command_owner_id(update)):"
        ), "admin gate missing or commented out")

    def test_ambiguous_outlet_stops_before_any_export(self):
        self.assertTrue(has_code_line(self.block, "if len(matches) != 1:"),
                        "ambiguity stop missing or commented out")
        choice = self.block.index("format_outlet_choice")
        build = self.block.index("invoices_export.build_export")
        self.assertLess(choice, build, "the export must not run on an ambiguous outlet")

    def test_db_work_runs_off_the_event_loop(self):
        self.assertIn("await asyncio.to_thread(invoices_export.distinct_outlets", self.block)
        self.assertIn("await asyncio.to_thread(\n            invoices_export.build_export", self.block)

    def test_files_are_sent_as_one_document_album_with_a_fallback(self):
        self.assertIn("InputMediaDocument(media=content, filename=name)", self.block)
        self.assertIn("await message.reply_media_group(media=media)", self.block)
        self.assertIn("await message.reply_document(document=content, filename=name)",
                      self.block)

    def test_summary_follows_the_files(self):
        send = self.block.index("reply_media_group")
        summary = self.block.index("invoices_export.format_summary")
        self.assertLess(send, summary)

    def test_read_failures_are_reported_not_swallowed(self):
        self.assertIn('await message.reply_text("Failed to read outlets from receipts.")',
                      self.block)
        self.assertIn('await message.reply_text("Failed to build the invoice export.")',
                      self.block)

    def test_module_is_imported(self):
        self.assertIn("\nimport invoices_export\n", self.src)
        self.assertIn("    InputMediaDocument,\n", self.src)


if __name__ == "__main__":
    unittest.main()
