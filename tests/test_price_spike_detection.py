"""Unit tests for ``price_spike_detection``.

Hermetic — uses a hand-rolled ``FakeSupabaseClient`` (extending the
pattern from ``test_price_aggregation``) with select/eq/neq filter
support so we can exercise the historical-average query without a
live Supabase instance.

Run with::

    python -m unittest tests.test_price_spike_detection
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from price_spike_detection import (  # noqa: E402
    detect_spikes,
    format_spike_message,
    get_historical_average,
)


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Stand-in for ``.table(...).select(...).eq(...).neq(...).execute()``."""

    def __init__(self, parent, table_name):
        self.parent = parent
        self.table_name = table_name
        self._eq: dict = {}
        self._neq: dict = {}

    def select(self, *_cols):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def neq(self, col, val):
        self._neq[col] = val
        return self

    def execute(self):
        if self.parent.raise_on_execute is not None:
            raise self.parent.raise_on_execute
        rows = list(self.parent.rows.get(self.table_name, []))
        out = []
        for row in rows:
            if not all(row.get(k) == v for k, v in self._eq.items()):
                continue
            if not all(row.get(k) != v for k, v in self._neq.items()):
                continue
            out.append(row)
        return FakeResult(out)


class FakeSupabaseClient:
    def __init__(self):
        self.rows: dict = {}
        self.raise_on_execute = None

    def add_rows(self, table_name, rows):
        self.rows.setdefault(table_name, []).extend(rows)

    def table(self, name):
        return FakeQuery(self, name)


def _row(canonical, merchant, unit_price, receipt_id):
    return {
        "canonical_item": canonical,
        "merchant": merchant,
        "unit_price": unit_price,
        "receipt_id": receipt_id,
    }


def _seed(client, prices, canonical="ayam", merchant="BESTARI", start_id=1):
    client.add_rows(
        "item_prices",
        [_row(canonical, merchant, p, start_id + i) for i, p in enumerate(prices)],
    )


class GetHistoricalAverage(unittest.TestCase):

    def test_sufficient_merchant_samples_returns_merchant_scope(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0, 10.0, 10.0, 10.0, 10.0])
        result = get_historical_average(client, "ayam", merchant="BESTARI")
        self.assertIsNotNone(result)
        self.assertEqual(result["scope"], "merchant")
        self.assertEqual(result["sample_count"], 5)
        self.assertAlmostEqual(result["avg_price"], 10.0)

    def test_insufficient_merchant_falls_back_to_global(self):
        client = FakeSupabaseClient()
        # 3 BESTARI samples, 5 from another merchant -> 8 global, 3 merchant
        _seed(client, [10.0, 10.0, 10.0], merchant="BESTARI", start_id=1)
        _seed(client, [12.0] * 5, merchant="OTHER", start_id=10)
        result = get_historical_average(client, "ayam", merchant="BESTARI")
        self.assertIsNotNone(result)
        self.assertEqual(result["scope"], "global")
        self.assertEqual(result["sample_count"], 8)

    def test_insufficient_data_returns_none(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0, 10.0, 10.0])  # only 3, no fallback
        self.assertIsNone(
            get_historical_average(client, "ayam", merchant="BESTARI")
        )

    def test_excludes_current_receipt_id(self):
        client = FakeSupabaseClient()
        # 5 rows with receipt_id 100 (the "current" receipt) - excluded
        client.add_rows(
            "item_prices",
            [_row("ayam", "BESTARI", 99.0, 100) for _ in range(5)],
        )
        # 5 historical rows
        _seed(client, [10.0] * 5, start_id=1)
        result = get_historical_average(
            client, "ayam", merchant="BESTARI", exclude_receipt_id=100
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["sample_count"], 5)
        self.assertAlmostEqual(result["avg_price"], 10.0)

    def test_min_max_from_merchant_scope(self):
        client = FakeSupabaseClient()
        _seed(client, [8.0, 10.0, 12.0, 14.0, 16.0])
        result = get_historical_average(client, "ayam", merchant="BESTARI")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["min_price"], 8.0)
        self.assertAlmostEqual(result["max_price"], 16.0)
        self.assertAlmostEqual(result["avg_price"], 12.0)

    def test_min_max_from_global_fallback(self):
        client = FakeSupabaseClient()
        # only 1 BESTARI row - merchant scope insufficient
        _seed(client, [10.0], merchant="BESTARI", start_id=1)
        # global has prices 5..15
        _seed(client, [5.0, 7.0, 9.0, 11.0, 13.0, 15.0],
              merchant="OTHER", start_id=10)
        result = get_historical_average(client, "ayam", merchant="BESTARI")
        self.assertIsNotNone(result)
        self.assertEqual(result["scope"], "global")
        # global pulls all 7 rows including the BESTARI one
        self.assertAlmostEqual(result["min_price"], 5.0)
        self.assertAlmostEqual(result["max_price"], 15.0)

    def test_skips_zero_or_negative_prices(self):
        client = FakeSupabaseClient()
        client.add_rows("item_prices", [
            _row("ayam", "BESTARI", 10.0, 1),
            _row("ayam", "BESTARI", 11.0, 2),
            _row("ayam", "BESTARI", 0.0, 3),    # skipped
            _row("ayam", "BESTARI", -5.0, 4),   # skipped
            _row("ayam", "BESTARI", 12.0, 5),
            _row("ayam", "BESTARI", 13.0, 6),
            _row("ayam", "BESTARI", 14.0, 7),
        ])
        result = get_historical_average(client, "ayam", merchant="BESTARI")
        self.assertIsNotNone(result)
        self.assertEqual(result["sample_count"], 5)
        self.assertAlmostEqual(result["avg_price"], 12.0)

    def test_no_merchant_goes_directly_to_global(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0, 10.0, 10.0], merchant="A", start_id=1)
        _seed(client, [11.0, 11.0, 11.0], merchant="B", start_id=10)
        result = get_historical_average(client, "ayam", merchant=None)
        self.assertIsNotNone(result)
        self.assertEqual(result["scope"], "global")
        self.assertEqual(result["sample_count"], 6)

    def test_query_failure_returns_none_no_raise(self):
        client = FakeSupabaseClient()
        client.raise_on_execute = RuntimeError("boom")
        self.assertIsNone(get_historical_average(client, "ayam", merchant="X"))

    def test_garbage_canonical_returns_none(self):
        client = FakeSupabaseClient()
        self.assertIsNone(get_historical_average(client, None))
        self.assertIsNone(get_historical_average(client, ""))
        self.assertIsNone(get_historical_average(client, "   "))
        self.assertIsNone(get_historical_average(client, 42))


class DetectSpikes(unittest.TestCase):

    def test_spike_above_110_percent_returned(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0] * 5)  # avg 10.0, threshold > 11.0
        records = [{
            "canonical_item": "ayam", "raw_item_name": "Ayam",
            "qty": 1.0, "unit_price": 12.0, "line_total": 12.0,
        }]
        spikes = detect_spikes(client, records, receipt_id=999, merchant="BESTARI")
        self.assertEqual(len(spikes), 1)
        spike = spikes[0]
        self.assertEqual(spike["canonical_item"], "ayam")
        self.assertEqual(spike["raw_item_name"], "Ayam")
        self.assertAlmostEqual(spike["historical_avg"], 10.0)
        self.assertAlmostEqual(spike["current_price"], 12.0)
        self.assertAlmostEqual(spike["percent_increase"], 20.0)
        self.assertEqual(spike["scope"], "merchant")
        self.assertEqual(spike["sample_count"], 5)
        self.assertEqual(spike["merchant"], "BESTARI")

    def test_under_10_percent_not_a_spike(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0] * 5)
        records = [{
            "canonical_item": "ayam", "raw_item_name": "Ayam",
            "qty": 1.0, "unit_price": 10.5, "line_total": 10.5,  # +5%
        }]
        self.assertEqual(
            detect_spikes(client, records, receipt_id=999, merchant="BESTARI"),
            [],
        )

    def test_exactly_110_percent_not_a_spike(self):
        # Strict ``>``, not ``>=``.
        client = FakeSupabaseClient()
        _seed(client, [10.0] * 5)
        records = [{
            "canonical_item": "ayam", "raw_item_name": "Ayam",
            "qty": 1.0, "unit_price": 11.0, "line_total": 11.0,
        }]
        self.assertEqual(
            detect_spikes(client, records, receipt_id=999, merchant="BESTARI"),
            [],
        )

    def test_under_5_samples_no_alert(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0, 10.0])  # only 2
        records = [{
            "canonical_item": "ayam", "raw_item_name": "Ayam",
            "qty": 1.0, "unit_price": 100.0, "line_total": 100.0,
        }]
        self.assertEqual(
            detect_spikes(client, records, receipt_id=999, merchant="BESTARI"),
            [],
        )

    def test_multiple_items_mixed_spike_and_normal(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0] * 5, canonical="ayam", start_id=1)
        _seed(client, [5.0] * 5, canonical="telur", start_id=20)
        records = [
            {"canonical_item": "ayam", "raw_item_name": "Ayam",
             "qty": 1, "unit_price": 13.0, "line_total": 13.0},  # +30% spike
            {"canonical_item": "telur", "raw_item_name": "Telur",
             "qty": 1, "unit_price": 5.2, "line_total": 5.2},  # +4%, no spike
        ]
        spikes = detect_spikes(client, records, receipt_id=999, merchant="BESTARI")
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["canonical_item"], "ayam")

    def test_empty_price_records_returns_empty(self):
        client = FakeSupabaseClient()
        self.assertEqual(
            detect_spikes(client, [], receipt_id=1, merchant="X"), []
        )

    def test_skips_records_without_canonical_item(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0] * 5)
        records = [
            {"canonical_item": None, "raw_item_name": "Mystery",
             "qty": 1, "unit_price": 100.0, "line_total": 100.0},
            {"canonical_item": "", "raw_item_name": "Blank",
             "qty": 1, "unit_price": 100.0, "line_total": 100.0},
        ]
        self.assertEqual(
            detect_spikes(client, records, receipt_id=999, merchant="BESTARI"),
            [],
        )

    def test_falls_back_to_global_scope_in_spike(self):
        client = FakeSupabaseClient()
        # Only 2 BESTARI samples but 5 OTHER -> total 7 global
        _seed(client, [10.0] * 2, merchant="BESTARI", start_id=1)
        _seed(client, [10.0] * 5, merchant="OTHER", start_id=10)
        records = [{
            "canonical_item": "ayam", "raw_item_name": "Ayam",
            "qty": 1, "unit_price": 13.0, "line_total": 13.0,
        }]
        spikes = detect_spikes(client, records, receipt_id=999, merchant="BESTARI")
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["scope"], "global")
        self.assertEqual(spikes[0]["sample_count"], 7)

    def test_excludes_current_receipt_id_from_history(self):
        client = FakeSupabaseClient()
        # Same receipt_id as current - polluting RM50 rows must be excluded
        client.add_rows(
            "item_prices",
            [_row("ayam", "BESTARI", 50.0, 999) for _ in range(5)],
        )
        _seed(client, [10.0] * 5, start_id=1)
        records = [{
            "canonical_item": "ayam", "raw_item_name": "Ayam",
            "qty": 1, "unit_price": 12.0, "line_total": 12.0,
        }]
        spikes = detect_spikes(client, records, receipt_id=999, merchant="BESTARI")
        # avg should be 10 (exclusion worked); 12 -> +20% spike
        self.assertEqual(len(spikes), 1)
        self.assertAlmostEqual(spikes[0]["historical_avg"], 10.0)

    def test_deduplicates_same_canonical_within_one_receipt(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0] * 5)
        records = [
            {"canonical_item": "ayam", "raw_item_name": "Ayam Hidup",
             "qty": 1, "unit_price": 13.0, "line_total": 13.0},
            {"canonical_item": "ayam", "raw_item_name": "Ayam Daging",
             "qty": 1, "unit_price": 14.0, "line_total": 14.0},
        ]
        spikes = detect_spikes(client, records, receipt_id=999, merchant="BESTARI")
        self.assertEqual(len(spikes), 1)

    def test_garbage_input_returns_empty_no_raise(self):
        client = FakeSupabaseClient()
        try:
            self.assertEqual(detect_spikes(client, None, 1, "X"), [])
            self.assertEqual(detect_spikes(client, "string", 1, "X"), [])
            self.assertEqual(detect_spikes(client, [None, 42, "x"], 1, "X"), [])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"detect_spikes raised: {e}")

    def test_query_failure_returns_empty_no_raise(self):
        client = FakeSupabaseClient()
        client.raise_on_execute = RuntimeError("connection refused")
        records = [{
            "canonical_item": "ayam", "raw_item_name": "Ayam",
            "qty": 1, "unit_price": 100.0, "line_total": 100.0,
        }]
        try:
            spikes = detect_spikes(client, records, 1, "BESTARI")
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"detect_spikes raised: {e}")
        self.assertEqual(spikes, [])

    def test_skips_non_positive_current_price(self):
        client = FakeSupabaseClient()
        _seed(client, [10.0] * 5)
        records = [
            {"canonical_item": "ayam", "raw_item_name": "Ayam",
             "qty": 1, "unit_price": 0, "line_total": 0},
            {"canonical_item": "ayam", "raw_item_name": "Ayam",
             "qty": 1, "unit_price": -3, "line_total": -3},
        ]
        self.assertEqual(
            detect_spikes(client, records, receipt_id=999, merchant="BESTARI"),
            [],
        )


class FormatSpikeMessage(unittest.TestCase):

    def test_style_a_output_exact(self):
        spike = {
            "canonical_item": "ayam",
            "raw_item_name": "Ayam Hidup",
            "current_price": 12.50,
            "historical_avg": 10.00,
            "min_price": 8.00,
            "max_price": 11.50,
            "sample_count": 7,
            "scope": "merchant",
            "percent_increase": 25.0,
            "merchant": "BESTARI FARM",
        }
        expected = (
            "⚠️ Price increase detected\n"
            "\n"
            "Ayam — BESTARI FARM\n"
            "Previous average: RM10.00 (from 7 receipts, merchant scope)\n"
            "Range: RM8.00 - RM11.50\n"
            "Today: RM12.50 (+25.0%)\n"
            "\n"
            "Did you ask supplier?"
        )
        self.assertEqual(format_spike_message(spike), expected)

    def test_global_scope_message(self):
        spike = {
            "canonical_item": "telur",
            "raw_item_name": "Telur",
            "current_price": 18.00,
            "historical_avg": 15.00,
            "min_price": 12.00,
            "max_price": 17.00,
            "sample_count": 12,
            "scope": "global",
            "percent_increase": 20.0,
            "merchant": "PASAR",
        }
        out = format_spike_message(spike)
        self.assertIn("Telur — PASAR", out)
        self.assertIn("12 receipts, global scope", out)
        self.assertIn("Range: RM12.00 - RM17.00", out)
        self.assertIn("Today: RM18.00 (+20.0%)", out)

    def test_garbage_input_returns_empty_string_no_raise(self):
        try:
            self.assertEqual(format_spike_message(None), "")
            self.assertEqual(format_spike_message("string"), "")
            self.assertEqual(format_spike_message({}), "")
            self.assertEqual(
                format_spike_message({"canonical_item": "ayam"}), ""
            )
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"format_spike_message raised: {e}")


if __name__ == "__main__":
    unittest.main()
