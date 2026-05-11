"""Unit tests for ``outlet_mapping``.

Run with::

    python -m unittest tests.test_outlet_mapping
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from outlet_mapping import outlet_from_chat_title  # noqa: E402


class OutletFromChatTitleHappyPaths(unittest.TestCase):
    def test_bistro_title_maps_to_bistro7(self):
        self.assertEqual(outlet_from_chat_title("Khulafa bistro resit"), "BISTRO7")

    def test_sharfuddin_title_maps_to_sek6(self):
        self.assertEqual(
            outlet_from_chat_title("Hj sharfuddin sek 6 receipt"), "SEK6"
        )

    def test_sek_6_title_maps_to_sek6(self):
        self.assertEqual(outlet_from_chat_title("Khulafa sek 6"), "SEK6")

    def test_sek_14_title_maps_to_sek14(self):
        self.assertEqual(outlet_from_chat_title("Khulafa sek 14"), "SEK14")

    def test_sek_20_title_maps_to_sek20(self):
        self.assertEqual(outlet_from_chat_title("Khulafa sek 20 receipt"), "SEK20")

    def test_klang_title_maps_to_klang(self):
        self.assertEqual(outlet_from_chat_title("Klang resit"), "KLANG")

    def test_vista_title_maps_to_vista(self):
        self.assertEqual(outlet_from_chat_title("Vista alam"), "VISTA")

    def test_jakel_title_maps_to_jakel(self):
        self.assertEqual(outlet_from_chat_title("Jakel outlet"), "JAKEL")

    def test_damansara_title_maps_to_d(self):
        self.assertEqual(outlet_from_chat_title("Damansara receipt"), "D")

    def test_s_besi_with_space_maps_to_sbesi(self):
        self.assertEqual(outlet_from_chat_title("S Besi resit"), "SBESI")

    def test_sbesi_no_space_maps_to_sbesi(self):
        self.assertEqual(outlet_from_chat_title("SBESI receipt"), "SBESI")


class OutletFromChatTitleCaseInsensitive(unittest.TestCase):
    def test_uppercase_bistro_matches(self):
        self.assertEqual(outlet_from_chat_title("KHULAFA BISTRO RESIT"), "BISTRO7")

    def test_mixed_case_jakel_matches(self):
        self.assertEqual(outlet_from_chat_title("JaKeL Outlet"), "JAKEL")


class OutletFromChatTitleUnmapped(unittest.TestCase):
    def test_sek_15_intentionally_unmapped(self):
        self.assertIsNone(outlet_from_chat_title("Khulafa sek 15 receipt"))

    def test_random_group_unmapped(self):
        self.assertIsNone(outlet_from_chat_title("Random Group"))

    def test_none_input_returns_none(self):
        self.assertIsNone(outlet_from_chat_title(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(outlet_from_chat_title(""))

    def test_non_string_input_returns_none(self):
        self.assertIsNone(outlet_from_chat_title(12345))


if __name__ == "__main__":
    unittest.main()
