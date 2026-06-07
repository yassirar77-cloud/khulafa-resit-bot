"""Item classification + cheaper-alternate rules (order_items)."""
import unittest

import order_items


class ClassificationTests(unittest.TestCase):
    def test_perishable_vs_dry(self):
        self.assertEqual(order_items.kind_of("ayam"), order_items.PERISHABLE)
        self.assertEqual(order_items.kind_of("spices_saida"), order_items.DRY)

    def test_unknown_defaults_to_dry_not_dropped(self):
        self.assertEqual(order_items.kind_of("totally_new_item"), order_items.DRY)

    def test_exclude_non_order_lines(self):
        self.assertFalse(order_items.is_orderable("transport"))
        self.assertFalse(order_items.is_orderable("nasi_lemak"))
        self.assertTrue(order_items.is_orderable("ayam"))

    def test_display_and_unit(self):
        self.assertEqual(order_items.display_name("spices_saida"), "Rempah (Saida)")
        self.assertEqual(order_items.unit_noun("ayam"), "kg")
        # fallback title-cases unknown keys
        self.assertEqual(order_items.display_name("foo_bar"), "Foo Bar")


class AlternateTests(unittest.TestCase):
    def test_spice_alternate_fires_for_saida(self):
        alt = order_items.cheaper_alternate("spices_saida", "SAIDA ENTERPRISE")
        self.assertIsNotNone(alt)
        self.assertEqual(alt["alternate"], "Shree Map Jaya")

    def test_seafood_alternate_fires_for_fook_leong(self):
        alt = order_items.cheaper_alternate("udang", "FOOK LEONG SEAFOOD SDN BHD")
        self.assertIsNotNone(alt)
        self.assertEqual(alt["alternate"], "Quiwave Oceanic")

    def test_no_alternate_when_already_cheap_source(self):
        self.assertIsNone(order_items.cheaper_alternate("spices_saida", "Shree Map Jaya"))

    def test_no_alternate_for_unrelated_supplier(self):
        self.assertIsNone(order_items.cheaper_alternate("ayam", "BESTARI FARM"))


if __name__ == "__main__":
    unittest.main()
