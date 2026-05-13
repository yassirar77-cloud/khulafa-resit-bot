"""Unit tests for ``price_aggregation``.

Covers both pure-Python extraction (``classify_and_extract_items``) and
the Supabase persistence wrapper (``save_item_prices``) — the latter is
exercised with a hand-rolled fake client so the tests stay hermetic.

Run with::

    python -m unittest tests.test_price_aggregation
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from price_aggregation import classify_and_extract_items, save_item_prices  # noqa: E402


class FakeSupabaseResult:
    def __init__(self, data):
        self.data = data


class FakeInsertChain:
    """Stand-in for the ``.table(...).insert(...).execute()`` chain."""

    def __init__(self, table_name, parent):
        self.table_name = table_name
        self.parent = parent
        self._payload = None

    def insert(self, payload):
        self._payload = payload
        self.parent.last_insert_payload = payload
        self.parent.last_table = self.table_name
        return self

    def execute(self):
        if self.parent.raise_on_execute is not None:
            raise self.parent.raise_on_execute
        # Mirror Supabase behavior: insert echoes back the rows inserted.
        return FakeSupabaseResult(self._payload)


class FakeSupabaseClient:
    def __init__(self):
        self.last_insert_payload = None
        self.last_table = None
        self.raise_on_execute = None

    def table(self, name):
        return FakeInsertChain(name, self)


class ClassifyAndExtractItems(unittest.TestCase):

    def test_clean_items_produce_full_records(self):
        items = [
            {"name": "Ayam", "qty": 30, "price": 19.80},
            {"name": "Telur", "qty": 2, "price": 15.0},
        ]
        result = classify_and_extract_items(items)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["raw_item_name"], "Ayam")
        self.assertAlmostEqual(result[0]["qty"], 30.0)
        self.assertAlmostEqual(result[0]["unit_price"], 19.80)
        self.assertAlmostEqual(result[0]["line_total"], 30 * 19.80)
        self.assertEqual(result[1]["raw_item_name"], "Telur")
        self.assertAlmostEqual(result[1]["line_total"], 30.0)

    def test_line_total_is_qty_times_unit_price(self):
        result = classify_and_extract_items(
            [{"name": "SOS CILI", "qty": 7, "price": 6.30}]
        )
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["line_total"], 44.10)

    def test_raw_item_name_preserves_original_ocr_string(self):
        # Mixed case + whitespace + supplier code -> kept verbatim.
        original = "  Li Agam 4 KG  "
        result = classify_and_extract_items(
            [{"name": original, "qty": 1, "price": 12.5}]
        )
        self.assertEqual(result[0]["raw_item_name"], original)

    def test_canonical_item_uses_canonicalize_item(self):
        # "AYAM" is a known canonical category in canonical_items_v2.json
        # — confirm we delegate to canonicalize_item() rather than echoing
        # the raw name.
        result = classify_and_extract_items(
            [{"name": "AYAM", "qty": 1, "price": 10.0}]
        )
        self.assertEqual(result[0]["canonical_item"], "ayam")

    def test_unknown_item_canonical_is_none_but_record_kept(self):
        # No canonical match -> canonical_item=None, but the row still
        # appears (we collect everything; PR #24 filters downstream).
        result = classify_and_extract_items(
            [{"name": "ZZZ_NONSENSE_PRODUCT", "qty": 1, "price": 1.0}]
        )
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["canonical_item"])
        self.assertEqual(result[0]["raw_item_name"], "ZZZ_NONSENSE_PRODUCT")

    def test_mixed_clean_and_messy_items(self):
        items = [
            {"name": "Ayam", "qty": 30, "price": 19.80},  # clean
            {"name": "Missing qty", "qty": None, "price": 5.0},  # dropped
            {"name": "Missing price", "qty": 3, "price": None},  # dropped
            {"name": None, "qty": 2, "price": 4.0},  # dropped (no name)
            {"name": "  ", "qty": 2, "price": 4.0},  # dropped (blank name)
            {"name": "Telur", "qty": 2, "price": 15.0},  # clean
        ]
        result = classify_and_extract_items(items)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["raw_item_name"], "Ayam")
        self.assertEqual(result[1]["raw_item_name"], "Telur")

    def test_empty_list_returns_empty_list(self):
        self.assertEqual(classify_and_extract_items([]), [])

    def test_all_null_items_returns_empty_list(self):
        items = [
            {"name": "A", "qty": None, "price": None},
            {"name": "B", "qty": None, "price": None},
        ]
        self.assertEqual(classify_and_extract_items(items), [])

    def test_non_list_input_returns_empty_list(self):
        self.assertEqual(classify_and_extract_items(None), [])
        self.assertEqual(classify_and_extract_items("string"), [])
        self.assertEqual(classify_and_extract_items({"name": "x"}), [])

    def test_non_dict_items_skipped(self):
        items = ["bare string", 42, None, {"name": "Ayam", "qty": 1, "price": 5.0}]
        result = classify_and_extract_items(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["raw_item_name"], "Ayam")

    def test_bool_qty_or_price_rejected(self):
        # ``True``/``False`` are int subclasses in Python; reject them so
        # a stray boolean doesn't masquerade as a real quantity.
        self.assertEqual(
            classify_and_extract_items(
                [{"name": "Ayam", "qty": True, "price": 5.0}]
            ),
            [],
        )
        self.assertEqual(
            classify_and_extract_items(
                [{"name": "Ayam", "qty": 5, "price": False}]
            ),
            [],
        )

    def test_receipt_total_argument_accepted_but_unused(self):
        # Currently unused but reserved for PR #24 reconciliation. Make
        # sure passing it doesn't change behavior or raise.
        items = [{"name": "Ayam", "qty": 1, "price": 5.0}]
        without = classify_and_extract_items(items)
        with_total = classify_and_extract_items(items, receipt_total=5.0)
        self.assertEqual(without, with_total)

    def test_never_raises_on_garbage_input(self):
        # The wrapper around canonicalize_item should swallow whatever
        # weirdness the OCR throws at us — assert no exception even on
        # nested junk.
        try:
            classify_and_extract_items(
                [{"name": "x", "qty": 1, "price": 1.0, "extra": object()}]
            )
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"classify_and_extract_items raised: {e}")


class SaveItemPrices(unittest.TestCase):

    def _records(self):
        return [
            {
                "raw_item_name": "Ayam",
                "canonical_item": "ayam",
                "qty": 30.0,
                "unit_price": 19.80,
                "line_total": 594.0,
            },
            {
                "raw_item_name": "Telur",
                "canonical_item": "telur",
                "qty": 2.0,
                "unit_price": 15.0,
                "line_total": 30.0,
            },
        ]

    def test_inserts_rows_into_item_prices(self):
        client = FakeSupabaseClient()
        count = save_item_prices(
            client,
            receipt_id=123,
            receipt_date="2026-05-13",
            outlet_code="SEK14",
            chat_id=-100123,
            merchant="BESTARI FARM",
            price_records=self._records(),
        )
        self.assertEqual(count, 2)
        self.assertEqual(client.last_table, "item_prices")
        payload = client.last_insert_payload
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["receipt_id"], 123)
        self.assertEqual(payload[0]["receipt_date"], "2026-05-13")
        self.assertEqual(payload[0]["outlet_code"], "SEK14")
        self.assertEqual(payload[0]["chat_id"], -100123)
        self.assertEqual(payload[0]["merchant"], "BESTARI FARM")
        self.assertEqual(payload[0]["raw_item_name"], "Ayam")
        self.assertEqual(payload[0]["canonical_item"], "ayam")
        self.assertAlmostEqual(payload[0]["qty"], 30.0)
        self.assertAlmostEqual(payload[0]["unit_price"], 19.80)
        self.assertAlmostEqual(payload[0]["line_total"], 594.0)

    def test_empty_records_short_circuits_to_zero(self):
        client = FakeSupabaseClient()
        with self.assertLogs("price_aggregation", level="WARNING"):
            count = save_item_prices(
                client,
                receipt_id=1,
                receipt_date="2026-05-13",
                outlet_code=None,
                chat_id=1,
                merchant="X",
                price_records=[],
            )
        self.assertEqual(count, 0)
        # Empty input should NOT touch the client at all.
        self.assertIsNone(client.last_insert_payload)

    def test_insert_failure_returns_zero_and_logs(self):
        client = FakeSupabaseClient()
        client.raise_on_execute = RuntimeError("connection refused")
        with self.assertLogs("price_aggregation", level="ERROR"):
            count = save_item_prices(
                client,
                receipt_id=42,
                receipt_date="2026-05-13",
                outlet_code="SEK14",
                chat_id=1,
                merchant="X",
                price_records=self._records(),
            )
        self.assertEqual(count, 0)

    def test_none_outlet_code_passes_through(self):
        # Unmapped chats return outlet_code=None — we still want the row.
        client = FakeSupabaseClient()
        count = save_item_prices(
            client,
            receipt_id=7,
            receipt_date="2026-05-13",
            outlet_code=None,
            chat_id=1,
            merchant="X",
            price_records=self._records()[:1],
        )
        self.assertEqual(count, 1)
        self.assertIsNone(client.last_insert_payload[0]["outlet_code"])

    def test_uses_mock_client_via_unittest_mock(self):
        # Sanity check that MagicMock works too (some downstream tests
        # in this repo prefer it over hand-rolled fakes).
        client = MagicMock()
        client.table.return_value.insert.return_value.execute.return_value = (
            FakeSupabaseResult([{"id": 1}, {"id": 2}])
        )
        count = save_item_prices(
            client,
            receipt_id=1,
            receipt_date="2026-05-13",
            outlet_code="SEK14",
            chat_id=1,
            merchant="X",
            price_records=self._records(),
        )
        self.assertEqual(count, 2)
        client.table.assert_called_once_with("item_prices")


if __name__ == "__main__":
    unittest.main()
