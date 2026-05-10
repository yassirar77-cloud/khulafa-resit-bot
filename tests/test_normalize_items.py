"""Unit tests for ``items_utils.normalize_items``.

Run with::

    python -m unittest tests.test_normalize_items
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from items_utils import normalize_items  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
