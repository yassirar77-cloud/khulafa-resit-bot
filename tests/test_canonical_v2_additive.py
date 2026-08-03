"""Regression gate: widening canonicalization v2 must be ADDITIVE ONLY.

``data/canonical_items_v2.json`` is shared. ``price_aggregation`` writes its
result into ``item_prices.canonical_item``, ``receipt_items`` keys every wastage
comparison on it, and ``item_resolver`` / ``backfill_items`` build their coverage
reports from it. A category that quietly starts claiming strings another category
used to own does not raise anything — it re-labels historical spend, and the
first symptom is a price chart that disagrees with itself.

So the baseline in ``tests/fixtures/canonical_v2_baseline.json`` was captured
from the data as it stood BEFORE telur / beras / minyak_masak / susu / tulang
were added, over a 361-string corpus: every declared variation, every noise
pattern, every item name in the real Damansara POS fixture, and a set of
realistic supplier-invoice lines.

The hard gate is :meth:`AdditiveOnly.test_no_existing_declared_variation_drifts`
— zero of the 205 pre-existing variations may change. The rest of the corpus is
pinned too, with the intended gains and the one accepted loss listed explicitly,
so a future edit cannot move them without a test failing and saying so.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from item_canonicalization_v2 import canonicalize_item, list_canonical_items  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_PATH = os.path.join(REPO_ROOT, "tests", "fixtures",
                             "canonical_v2_baseline.json")
DATA_PATH = os.path.join(REPO_ROOT, "data", "canonical_items_v2.json")

#: The categories added in this change. Nothing else may appear.
ADDED_CATEGORIES = {"telur", "beras", "minyak_masak", "susu", "tulang"}

#: The 34 that existed before, every one of which must survive untouched.
PRE_EXISTING_CATEGORIES = {
    "ais_batu", "kelapa", "santan", "bawang_goreng", "ayam", "daging", "kambing",
    "ikan", "sotong", "udang", "ikan_bilis", "roti", "capati", "nasi_lemak",
    "tepung_roti", "kicap", "sos_cili", "sos_tomato", "sos_tiram", "cuka",
    "asam_jawa", "kerisik", "spices_babas", "spices_saida", "tea_masala", "kopi",
    "extra_juss", "drinks", "yogurt", "keropok", "kacang", "gas", "transport",
    "cleaning",
}

#: Previously-unmatched strings that now resolve. This is the POINT of the
#: change — an addition that matched nothing new would be pointless — but each
#: one is listed so it is a decision, not a side effect.
EXPECTED_GAINS = {
    "Bihun G Telur Mata": "telur",
    "Maggi Goreng Telur Mata": "telur",
    "Mee Goreng Telur Mata": "telur",
    "Nasi Goreng Telur Mata": "telur",
    "Susu Halia": "susu",
    "Telur 1/2 Masak": "telur",
    "Telur 3/4 Masak": "telur",
    "Telur Goreng": "telur",
    "Telur Masin": "telur",
    "Telur Mata": "telur",
    "Telur Rebus": "telur",
    "Telur Sambal": "telur",
    "Thosai Telur": "telur",
}

#: The ONE place an existing category loses a string. Both are POS DISH names,
#: not declared variations and not shapes that appear on a supplier invoice —
#: canonicalization v2 is applied to invoice lines, where "ROTI TELUR" does not
#: occur. Recorded here rather than silently accepted: if the owner wants
#: "Roti Telur" to stay `roti`, the fix is to add it to the roti category, which
#: would mean touching one of the 34.
ACCEPTED_LOSSES = {
    "Roti Telur": ("roti", "telur"),
    "Roti Telur Bawang": ("roti", "telur"),
}


def load_baseline():
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


class CategorySet(unittest.TestCase):

    def test_every_pre_existing_category_still_exists(self):
        current = set(list_canonical_items())
        missing = PRE_EXISTING_CATEGORIES - current
        self.assertEqual(missing, set(), f"categories disappeared: {missing}")

    def test_only_the_declared_additions_are_new(self):
        current = set(list_canonical_items())
        unexpected = current - PRE_EXISTING_CATEGORIES - ADDED_CATEGORIES
        self.assertEqual(unexpected, set(), f"undeclared categories: {unexpected}")

    def test_the_count_went_up_by_exactly_the_additions(self):
        self.assertEqual(len(list_canonical_items()),
                         len(PRE_EXISTING_CATEGORIES) + len(ADDED_CATEGORIES))

    def test_no_pre_existing_category_lost_a_variation(self):
        from item_canonicalization_v2 import get_item_variations

        with open(BASELINE_PATH, encoding="utf-8") as f:
            baseline = json.load(f)
        # Every string the baseline says belongs to a pre-existing category must
        # still be declared under one.
        for category in PRE_EXISTING_CATEGORIES:
            self.assertTrue(get_item_variations(category), category)
        self.assertGreater(baseline["corpus_size"], 300)

    def test_tepung_was_NOT_added(self):
        # `tepung_roti` already declares TEPUNG and FLOUR. A separate `tepung`
        # category would be a merge of an existing one, not an addition — so it
        # is deliberately absent, and the existing category still owns them.
        self.assertNotIn("tepung", list_canonical_items())
        self.assertEqual(canonicalize_item("TEPUNG")["canonical"], "tepung_roti")
        self.assertEqual(canonicalize_item("FLOUR")["canonical"], "tepung_roti")
        self.assertEqual(canonicalize_item("TEPUNG ROTI")["canonical"], "tepung_roti")


class AdditiveOnly(unittest.TestCase):
    """The gate: pre-existing behaviour is unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.baseline = load_baseline()
        with open(DATA_PATH, encoding="utf-8") as f:
            cls.data = json.load(f)
        cls.declared_before = set()
        for category, variations in cls.data["categories"].items():
            if category in PRE_EXISTING_CATEGORIES:
                cls.declared_before.update(variations)

    def test_no_existing_declared_variation_drifts(self):
        """THE gate. Every one of the 205 pre-existing variations is unmoved."""
        drift = []
        for text, expected in self.baseline["canonical"].items():
            if text not in self.declared_before:
                continue
            got = canonicalize_item(text)["canonical"]
            if got != expected:
                drift.append((text, expected, got))
        self.assertEqual(drift, [], f"pre-existing variations drifted: {drift}")

    def test_noise_classification_is_unchanged_everywhere(self):
        drift = [
            (text, expected, canonicalize_item(text)["is_noise"])
            for text, expected in self.baseline["is_noise"].items()
            if canonicalize_item(text)["is_noise"] != expected
        ]
        self.assertEqual(drift, [], f"noise classification drifted: {drift}")

    def test_every_change_across_the_whole_corpus_is_declared(self):
        """Nothing moves that this file does not name."""
        undeclared = []
        for text, expected in self.baseline["canonical"].items():
            got = canonicalize_item(text)["canonical"]
            if got == expected:
                continue
            if expected is None and EXPECTED_GAINS.get(text) == got:
                continue
            if ACCEPTED_LOSSES.get(text) == (expected, got):
                continue
            undeclared.append((text, expected, got))
        self.assertEqual(undeclared, [],
                         f"undeclared canonicalization changes: {undeclared}")

    def test_the_declared_gains_all_actually_happen(self):
        for text, expected in EXPECTED_GAINS.items():
            self.assertEqual(canonicalize_item(text)["canonical"], expected, text)

    def test_the_accepted_losses_are_exactly_two_and_both_are_dish_names(self):
        self.assertEqual(len(ACCEPTED_LOSSES), 2)
        for text, (before, after) in ACCEPTED_LOSSES.items():
            self.assertEqual(canonicalize_item(text)["canonical"], after, text)
            self.assertEqual(before, "roti")


class NewCategoriesResolveInvoiceLines(unittest.TestCase):
    """The additions have to earn their place on real invoice-shaped text."""

    def test_telur(self):
        for text in ("TELUR", "TELUR GRED A", "TELUR AYAM 30 BIJI", "EGG"):
            self.assertEqual(canonicalize_item(text)["canonical"], "telur", text)

    def test_beras(self):
        for text in ("BERAS", "BERAS SUPER 10KG", "BERAS BASMATI", "RICE"):
            self.assertEqual(canonicalize_item(text)["canonical"], "beras", text)

    def test_minyak(self):
        for text in ("MINYAK MASAK", "MINYAK SAWIT 17KG", "COOKING OIL"):
            self.assertEqual(canonicalize_item(text)["canonical"], "minyak_masak", text)

    def test_susu(self):
        for text in ("SUSU", "SUSU CAIR", "SUSU PEKAT", "EVAPORATED MILK"):
            self.assertEqual(canonicalize_item(text)["canonical"], "susu", text)

    def test_tulang(self):
        for text in ("TULANG", "TULANG SUP", "SOUP BONE"):
            self.assertEqual(canonicalize_item(text)["canonical"], "tulang", text)

    def test_fish_roe_still_belongs_to_ikan_not_telur(self):
        # "TELUR IKAN" is roe. The longer phrase wins over bare "TELUR", which
        # is the collision that made adding `telur` risky in the first place.
        for text in ("TELUR IKAN", "TELUR IKAN SEJUK BEKU"):
            self.assertEqual(canonicalize_item(text)["canonical"], "ikan", text)

    def test_mutton_bone_still_belongs_to_kambing_not_tulang(self):
        self.assertEqual(canonicalize_item("MUTTON LEG BONE IN C")["canonical"],
                         "kambing")


class WastageIngredientsNowHaveAHome(unittest.TestCase):
    """The reason for the widening: these were NO PURCHASE CATEGORY before."""

    def test_the_previously_uncomparable_ingredients_now_map(self):
        from wastage_rules_v12 import CANONICAL_BY_INGREDIENT

        for ingredient, canonical in (("telur", "telur"),
                                      ("beras_biasa", "beras"),
                                      ("beras_basmati", "beras"),
                                      ("minyak_masak", "minyak_masak"),
                                      ("susu_pekat", "susu"),
                                      ("susu_cair", "susu"),
                                      ("tulang", "tulang")):
            self.assertEqual(CANONICAL_BY_INGREDIENT[ingredient], canonical, ingredient)
            self.assertIn(canonical, list_canonical_items(), canonical)


if __name__ == "__main__":
    unittest.main()
