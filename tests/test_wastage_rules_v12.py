"""Tests for the v12 wastage rule set.

Every portion, keyword and exception here was given by the owner, so the tests
read as an executable transcript of the rules. Where a rule was NOT given —
udang, sotong and hati ayam have no portion — the test asserts that nothing is
invented, which is as load-bearing as the portions themselves.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wastage_rules_v12 import (  # noqa: E402
    CANONICAL_BY_INGREDIENT,
    COMPOSITE_MARKERS,
    CHICKEN_TYPE_BAWANG_REMPAH,
    CHICKEN_TYPE_DEFAULT,
    CHICKEN_TYPE_HATI,
    CHICKEN_TYPE_TANDOORI,
    PORTIONS,
    THAI_KEYWORDS,
    UNPORTIONED,
    chicken_pieces,
    chicken_type,
    drink_components,
    egg_count,
    is_thai_dish,
    main_protein,
    rice_component,
    is_composite_dish,
    seafood_components,
    servings_for_dish,
    usage_for_dish,
    uses_cooking_oil,
    uses_isi_ayam,
)


def amounts(name, qty=1.0):
    return {k: round(v["amount"], 4) for k, v in usage_for_dish(name, qty).items()}


class Portions(unittest.TestCase):
    """The owner-locked portion table, verbatim."""

    def test_every_locked_portion(self):
        expected = {
            "isi_ayam": (50.0, "g"),
            "kambing": (180.0, "g"),
            "daging_mysoor": (60.0, "g"),
            "daging_md_hani": (60.0, "g"),
            "tulang": (50.0, "g"),
            "ikan_tenggiri": (180.0, "g"),
            "beras_biasa": (120.0, "g"),
            "beras_basmati": (150.0, "g"),
            "serbuk_teh": (5.0, "g"),
            "serbuk_kopi": (5.0, "g"),
            "minyak_masak": (80.0, "ml"),
        }
        for key, value in expected.items():
            self.assertEqual(PORTIONS[key], value, key)

    def test_a_tin_of_susu_serves_nine_drinks(self):
        for key in ("susu_pekat", "susu_cair"):
            size, unit = PORTIONS[key]
            self.assertEqual(unit, "tin")
            self.assertAlmostEqual(size, 1.0 / 9.0, places=6)

    def test_daging_is_60g_whether_thai_or_mamak(self):
        self.assertEqual(PORTIONS["daging_mysoor"][0], PORTIONS["daging_md_hani"][0])

    def test_unportioned_ingredients_have_no_portion(self):
        for key in UNPORTIONED:
            self.assertNotIn(key, PORTIONS, key)


class ThaiKeywords(unittest.TestCase):

    def test_the_whole_locked_list_triggers(self):
        for keyword in THAI_KEYWORDS:
            self.assertTrue(is_thai_dish(f"Nasi Goreng {keyword.title()}"), keyword)

    def test_the_list_is_exactly_as_given(self):
        self.assertEqual(set(THAI_KEYWORDS), {
            "basa", "singapore", "seafood", "tomyam", "tom yam", "ladna", "sup",
            "kungfu", "kung fu", "pattaya", "hailam", "bandung", "cina", "kunyit",
            "merah", "paprik", "belacan", "cendawan", "cilipadi", "cili padi",
            "ikan masin", "kampung", "thai", "usa", "udang", "sotong",
        })

    def test_a_plain_mamak_dish_is_not_thai(self):
        for name in ("Ayam Goreng", "Nasi Kandar", "Teh Tarik", "Roti Canai"):
            self.assertFalse(is_thai_dish(name), name)


class Chicken(unittest.TestCase):

    def test_bawang_and_rempah_share_one_type(self):
        self.assertEqual(chicken_type("Ayam Bawang"), CHICKEN_TYPE_BAWANG_REMPAH)
        self.assertEqual(chicken_type("Ayam Rempah"), CHICKEN_TYPE_BAWANG_REMPAH)

    def test_tandoori_hati_and_default(self):
        self.assertEqual(chicken_type("Ayam Tandoori"), CHICKEN_TYPE_TANDOORI)
        self.assertEqual(chicken_type("Ayam Tandori"), CHICKEN_TYPE_TANDOORI)
        self.assertEqual(chicken_type("Hati Ayam"), CHICKEN_TYPE_HATI)
        self.assertEqual(chicken_type("Ayam Goreng"), CHICKEN_TYPE_DEFAULT)

    def test_one_piece_by_default(self):
        self.assertEqual(chicken_pieces("Ayam Goreng"), 1.0)

    def test_double_is_two_pieces(self):
        self.assertEqual(chicken_pieces("Ayam Goreng Double"), 2.0)
        self.assertEqual(chicken_pieces("Ayam Goreng DBL"), 2.0)

    def test_rendang_kurma_kari_are_a_third_of_a_piece(self):
        for style in ("Ayam Rendang", "Ayam Kurma", "Ayam Kari"):
            self.assertAlmostEqual(chicken_pieces(style), 1 / 3, places=6, msg=style)

    def test_double_and_third_compose(self):
        self.assertAlmostEqual(chicken_pieces("Ayam Rendang Double"), 2 / 3, places=6)

    def test_whole_cut_dishes_consume_pieces(self):
        self.assertEqual(amounts("Ayam Bawang")["ayam_whole"], 1.0)

    def test_liver_is_not_a_whole_bird(self):
        usage = usage_for_dish("Hati Ayam")
        self.assertIn("hati_ayam", usage)
        self.assertNotIn("ayam_whole", usage)
        self.assertFalse(usage["hati_ayam"]["portioned"])
        self.assertEqual(usage["hati_ayam"]["unit"], "dishes")


class IsiAyam(unittest.TestCase):

    def test_every_thai_dish_uses_fillet_including_the_protein_free_ones(self):
        for name in ("Maggi Sup", "Nasi Goreng Pattaya", "Kueyteow Kungfu",
                     "Maggi Goreng Basa"):
            self.assertTrue(uses_isi_ayam(name), name)
            self.assertEqual(amounts(name)["isi_ayam"], 50.0, name)

    def test_sayur_and_kosong_are_excluded(self):
        for name in ("Nasi Goreng Sayur Thai", "Sup Kosong", "Tomyam Sayur"):
            self.assertFalse(uses_isi_ayam(name), name)
            self.assertNotIn("isi_ayam", amounts(name), name)

    def test_another_main_protein_excludes_fillet(self):
        for name, protein in (("Tomyam Daging", "daging"),
                              ("Sup Kambing", "kambing"),
                              ("Tomyam Ikan", "ikan")):
            self.assertEqual(main_protein(name), protein, name)
            self.assertFalse(uses_isi_ayam(name), name)

    def test_ikan_masin_is_a_seasoning_not_the_main_protein(self):
        # Without stripping it, every Thai "ikan masin" dish would read as a
        # fish dish and lose its fillet.
        self.assertNotEqual(main_protein("Nasi Goreng Ikan Masin"), "ikan")
        self.assertTrue(uses_isi_ayam("Nasi Goreng Ikan Masin"))

    def test_a_mamak_dish_never_uses_fillet(self):
        self.assertFalse(uses_isi_ayam("Ayam Goreng"))

    def test_a_named_seafood_protein_means_no_fillet(self):
        for name in ("Sotong Goreng", "Nasi Goreng Sotong", "Nasi Goreng Udang",
                     "Udang Masak Merah"):
            self.assertFalse(uses_isi_ayam(name), name)
            self.assertNotIn("isi_ayam", amounts(name), name)


class SeafoodAsMainProtein(unittest.TestCase):
    """A named seafood is the main protein — unless the dish is a composite."""

    def test_sotong_and_udang_are_main_proteins(self):
        self.assertEqual(main_protein("Sotong Goreng"), "sotong")
        self.assertEqual(main_protein("Nasi Goreng Udang"), "udang")

    def test_sotong_dishes_take_sotong_only(self):
        for name in ("Sotong Goreng", "Nasi Goreng Sotong"):
            usage = amounts(name)
            self.assertIn("sotong", usage, name)
            self.assertNotIn("isi_ayam", usage, name)
            self.assertNotIn("udang", usage, name)

    def test_composite_markers(self):
        self.assertEqual(COMPOSITE_MARKERS, ("seafood", "campur", "special"))
        for name in ("Tomyam Seafood", "Nasi Campur", "Tomyam Special"):
            self.assertTrue(is_composite_dish(name), name)
        self.assertFalse(is_composite_dish("Sotong Goreng"))

    def test_a_seafood_dish_draws_fillet_AND_both_shellfish(self):
        for name in ("Tomyam Seafood", "Mee Goreng Seafood"):
            usage = amounts(name)
            self.assertEqual(usage["isi_ayam"], 50.0, name)
            self.assertIn("udang", usage, name)
            self.assertIn("sotong", usage, name)

    def test_a_composite_rescues_a_named_seafood(self):
        # "campur"/"special" mean several proteins, so the sotong does not
        # displace the chicken the way a plain "Nasi Goreng Sotong" does.
        usage = amounts("Nasi Goreng Sotong Campur")
        self.assertEqual(usage["isi_ayam"], 50.0)
        self.assertIn("sotong", usage)

    def test_composites_do_not_rescue_the_other_proteins(self):
        # The override is about seafood; a beef dish is still a beef dish.
        self.assertEqual(main_protein("Tomyam Daging Special"), "daging")
        self.assertFalse(uses_isi_ayam("Tomyam Daging Special"))

    def test_seafood_words_are_stripped_the_same_way_ikan_masin_is(self):
        # Both are "a protein word that is not THIS dish's main protein".
        self.assertIsNone(main_protein("Tomyam Seafood"))
        self.assertIsNone(main_protein("Nasi Goreng Ikan Masin"))


class ProteinSplit(unittest.TestCase):

    def test_mamak_beef_is_mysoor_and_thai_beef_is_md_hani(self):
        self.assertIn("daging_mysoor", amounts("Nasi Goreng Daging"))
        self.assertIn("daging_md_hani", amounts("Tomyam Daging"))

    def test_unspecified_beef_defaults_to_mysoor(self):
        self.assertIn("daging_mysoor", amounts("Daging Masak Kicap"))

    def test_both_beef_stocks_are_60g(self):
        self.assertEqual(amounts("Nasi Goreng Daging")["daging_mysoor"], 60.0)
        self.assertEqual(amounts("Tomyam Daging")["daging_md_hani"], 60.0)

    def test_thai_chicken_is_fillet_and_mamak_chicken_is_whole_cut(self):
        self.assertIn("isi_ayam", amounts("Ayam Paprik"))
        self.assertIn("ayam_whole", amounts("Ayam Bawang"))

    def test_kambing_and_ikan_portions(self):
        self.assertEqual(amounts("Nasi Kambing")["kambing"], 180.0)
        self.assertEqual(amounts("Ikan Goreng")["ikan_tenggiri"], 180.0)

    def test_soup_dishes_draw_on_bones(self):
        self.assertEqual(amounts("Sup Tulang")["tulang"], 50.0)


class Eggs(unittest.TestCase):

    def test_a_goreng_uses_one_egg(self):
        self.assertEqual(egg_count("Nasi Goreng"), 1.0)
        self.assertEqual(egg_count("Mee Goreng"), 1.0)

    def test_a_named_egg_dish_uses_one(self):
        for name in ("Telur Mata", "Telur Dadar", "Telur Bistik", "Roti Telur",
                     "Tosai Telur"):
            self.assertEqual(egg_count(name), 1.0, name)

    def test_goreng_plus_a_named_egg_dish_is_two(self):
        self.assertEqual(egg_count("Nasi Goreng Telur Mata"), 2.0)

    def test_double_on_an_egg_dish_is_two(self):
        self.assertEqual(egg_count("Telur Mata Double"), 2.0)

    def test_deep_fried_dishes_use_no_egg(self):
        for name in ("Ayam Goreng", "Ikan Goreng", "Kailan Goreng"):
            self.assertEqual(egg_count(name), 0.0, name)

    def test_a_bare_telur_in_a_name_is_not_a_named_egg_dish(self):
        # There is no generic "Nasi Goreng Telur" SKU, so a bare "telur" must
        # not silently add a second egg on top of the goreng baseline.
        self.assertEqual(egg_count("Nasi Goreng Telur"), 1.0)

    def test_a_non_goreng_non_egg_dish_uses_none(self):
        self.assertEqual(egg_count("Nasi Kandar"), 0.0)


class Drinks(unittest.TestCase):

    def test_condensed_milk_is_the_default(self):
        components = drink_components("Teh Tarik")
        self.assertEqual(components, {"serbuk_teh": 1.0, "susu_pekat": 1.0})

    def test_C_variants_use_evaporated_milk(self):
        self.assertIn("susu_cair", drink_components("Teh C"))
        self.assertIn("susu_cair", drink_components("Kopi C Ais"))
        self.assertNotIn("susu_pekat", drink_components("Teh C"))

    def test_O_variants_have_no_milk(self):
        for name in ("Teh O", "Kopi O", "Teh O Ais"):
            components = drink_components(name)
            self.assertNotIn("susu_pekat", components, name)
            self.assertNotIn("susu_cair", components, name)

    def test_powder_is_five_grams_per_cup(self):
        self.assertEqual(amounts("Teh Tarik")["serbuk_teh"], 5.0)
        self.assertEqual(amounts("Kopi O")["serbuk_kopi"], 5.0)

    def test_a_drink_consumes_a_ninth_of_a_tin(self):
        self.assertAlmostEqual(amounts("Teh Tarik")["susu_pekat"], 1 / 9, places=4)

    def test_nine_drinks_empty_one_tin(self):
        self.assertAlmostEqual(amounts("Teh Tarik", 9)["susu_pekat"], 1.0, places=6)

    def test_food_is_not_a_drink(self):
        self.assertEqual(drink_components("Nasi Goreng"), {})


class OilAndRice(unittest.TestCase):

    def test_oil_in_goreng_tandoori_and_roti(self):
        for name in ("Nasi Goreng", "Ayam Tandoori", "Roti Canai", "Ikan Fried"):
            self.assertTrue(uses_cooking_oil(name), name)
            self.assertEqual(amounts(name)["minyak_masak"], 80.0, name)

    def test_dry_breads_and_toast_are_excluded(self):
        for name in ("Capati", "Chapati", "Naan", "Roti Bakar"):
            self.assertFalse(uses_cooking_oil(name), name)
            self.assertNotIn("minyak_masak", amounts(name), name)

    def test_plain_rice_and_basmati(self):
        self.assertEqual(rice_component("Nasi Kandar"), "beras_biasa")
        self.assertEqual(rice_component("Briyani Ayam"), "beras_basmati")
        self.assertEqual(amounts("Nasi Kandar")["beras_biasa"], 120.0)
        self.assertEqual(amounts("Briyani Ayam")["beras_basmati"], 150.0)

    def test_a_non_rice_dish_uses_no_rice(self):
        self.assertIsNone(rice_component("Teh Tarik"))


class Seafood(unittest.TestCase):

    def test_a_seafood_dish_calls_for_both(self):
        self.assertEqual(sorted(seafood_components("Nasi Goreng Seafood")),
                         ["sotong", "udang"])

    def test_direct_dishes_call_for_one(self):
        self.assertEqual(seafood_components("Nasi Goreng Udang"), ["udang"])
        self.assertEqual(seafood_components("Sotong Goreng"), ["sotong"])

    def test_they_are_counted_in_dishes_never_weighed(self):
        usage = usage_for_dish("Nasi Goreng Seafood", 4)
        for key in ("udang", "sotong"):
            self.assertEqual(usage[key]["unit"], "dishes", key)
            self.assertEqual(usage[key]["amount"], 4.0, key)
            self.assertFalse(usage[key]["portioned"], key)


class MultiIngredientDishes(unittest.TestCase):

    def test_a_thai_fried_rice_consumes_four_things(self):
        self.assertEqual(amounts("Nasi Goreng Pattaya"), {
            "isi_ayam": 50.0, "beras_biasa": 120.0,
            "minyak_masak": 80.0, "telur": 1.0,
        })

    def test_quantities_scale(self):
        self.assertEqual(amounts("Nasi Goreng Pattaya", 10)["isi_ayam"], 500.0)

    def test_staff_meals_consume_nothing(self):
        self.assertEqual(servings_for_dish("Nasi Goreng Ayam Staff"), {})
        self.assertEqual(usage_for_dish("Nasi Goreng Ayam Staff", 50), {})

    def test_zero_and_negative_quantities(self):
        self.assertEqual(usage_for_dish("Nasi Goreng", 0), {})
        self.assertEqual(usage_for_dish("Nasi Goreng", -3), {})

    def test_junk_names(self):
        for name in (None, "", "   ", 42):
            self.assertEqual(usage_for_dish(name, 1), {}, name)


class PurchaseCategoryMapping(unittest.TestCase):

    def test_ingredients_that_map_to_a_canonical_item(self):
        self.assertEqual(CANONICAL_BY_INGREDIENT["isi_ayam"], "ayam")
        self.assertEqual(CANONICAL_BY_INGREDIENT["ayam_whole"], "ayam")
        self.assertEqual(CANONICAL_BY_INGREDIENT["daging_mysoor"], "daging")
        self.assertEqual(CANONICAL_BY_INGREDIENT["daging_md_hani"], "daging")
        self.assertEqual(CANONICAL_BY_INGREDIENT["ikan_tenggiri"], "ikan")
        self.assertEqual(CANONICAL_BY_INGREDIENT["serbuk_teh"], "tea_masala")
        self.assertEqual(CANONICAL_BY_INGREDIENT["serbuk_kopi"], "kopi")

    def test_the_staples_now_map_to_their_own_categories(self):
        # These reported NO PURCHASE CATEGORY until canonicalization v2 was
        # widened for them. Both rice grades share `beras` and both milks share
        # `susu`: an invoice line says "BERAS SUPER 10KG", not which dish it
        # will end up in.
        for key, canonical in (("telur", "telur"), ("beras_biasa", "beras"),
                               ("beras_basmati", "beras"),
                               ("minyak_masak", "minyak_masak"),
                               ("susu_pekat", "susu"), ("susu_cair", "susu"),
                               ("tulang", "tulang")):
            self.assertEqual(CANONICAL_BY_INGREDIENT[key], canonical, key)

    def test_unportioned_ingredients_still_have_no_portion(self):
        # Widening the purchase side does not invent a portion for them.
        for key in UNPORTIONED:
            self.assertNotIn(key, PORTIONS, key)

    def test_every_ingredient_the_rules_emit_has_a_mapping(self):
        emitted = set()
        for name in ("Ayam Goreng", "Ayam Bawang", "Hati Ayam", "Tomyam Daging",
                     "Nasi Goreng Daging", "Nasi Kambing", "Ikan Goreng",
                     "Sup Tulang", "Briyani Ayam", "Nasi Goreng Pattaya",
                     "Teh Tarik", "Teh C", "Kopi O", "Nasi Goreng Seafood",
                     "Telur Mata"):
            emitted.update(servings_for_dish(name))
        for key in emitted:
            self.assertIn(key, CANONICAL_BY_INGREDIENT, key)
            self.assertTrue(key in PORTIONS or key in UNPORTIONED, key)


if __name__ == "__main__":
    unittest.main()
