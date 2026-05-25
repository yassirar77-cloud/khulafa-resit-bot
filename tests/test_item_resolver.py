"""Tests for item normalisation (PR #32).

The matcher is shared with merchant_resolver (tested thoroughly there); these
tests cover the item-specific wiring: the seed migration content, resolve_item
(incl. fuzzy-auto recording via a fake client), and the owner-only /item_*
commands (source-level, since bot.py can't import in CI).
"""

import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import item_resolver as ir  # noqa: E402
from item_resolver import match_item, resolve_item  # noqa: E402
from merchant_resolver import substring_score  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALIASES = [
    {"id": 1, "alias_text": "AYAM BERSIH", "canonical_id": 1},
    {"id": 2, "alias_text": "ayam bersih", "canonical_id": 1},
    {"id": 3, "alias_text": "JINTAN PUTIH", "canonical_id": 17},
    {"id": 4, "alias_text": "SANTAN", "canonical_id": 36},
]
CANONICALS = [
    {"id": 1, "display_name": "ayam bersih"},
    {"id": 17, "display_name": "jintan putih"},
    {"id": 36, "display_name": "santan"},
]


class MatchItem(unittest.TestCase):
    def test_resolve_exact_match(self):
        self.assertEqual(match_item("AYAM BERSIH", ALIASES, CANONICALS), (1, 100))

    def test_resolve_case_insensitive(self):
        self.assertEqual(match_item("Jintan Putih", ALIASES, CANONICALS), (17, 95))

    def test_resolve_substring(self):
        # The headline item case: a noisy OCR line containing the canonical.
        self.assertEqual(match_item("AYAM BERSIH 30KG", ALIASES, CANONICALS), (1, 85))
        self.assertEqual(match_item("JINTAN PUTIH 1KG", ALIASES, CANONICALS), (17, 85))

    def test_resolve_word_boundary(self):
        # The substring tier must respect word boundaries: a lone partial word
        # or a boundary-less substring does not match a multi-word canonical.
        self.assertEqual(substring_score("ayam", "ayam bersih"), 0)
        self.assertEqual(substring_score("ayambersih", "ayam bersih"), 0)
        # A lone "AYAM" with no "AYAM" alias present resolves to nothing.
        self.assertEqual(match_item("AYAM", [], [{"id": 1, "display_name": "ayam bersih"}]), (None, 0))

    def test_resolve_unknown_returns_none(self):
        self.assertEqual(match_item("NASI LEMAK SPECIAL", ALIASES, CANONICALS), (None, 0))

    def test_empty_input(self):
        self.assertEqual(match_item("", ALIASES, CANONICALS), (None, 0))


# --- resolve_item with a recording fake client ------------------------------

class _FakeQuery:
    def __init__(self, parent, table):
        self.p, self.t, self.op, self.payload = parent, table, "select", None

    def select(self, *a, **k):
        self.op = "select"
        return self

    def eq(self, *a, **k):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def execute(self):
        if self.op == "insert":
            self.p.inserted.append((self.t, self.payload))
            return types.SimpleNamespace(data=[self.payload])
        if self.t == ir.ALIAS_TABLE:
            return types.SimpleNamespace(data=list(self.p.aliases))
        if self.t == ir.CANONICAL_TABLE:
            return types.SimpleNamespace(data=list(self.p.canonicals))
        return types.SimpleNamespace(data=[])


class FakeClient:
    def __init__(self, aliases, canonicals):
        self.aliases, self.canonicals, self.inserted = aliases, canonicals, []

    def table(self, name):
        return _FakeQuery(self, name)


class ResolveItem(unittest.TestCase):
    def test_resolve_records_fuzzy_alias_on_substring(self):
        client = FakeClient(ALIASES, CANONICALS)
        cid, conf = resolve_item("AYAM BERSIH 30KG", client)
        self.assertEqual((cid, conf), (1, 85))
        alias_inserts = [p for (t, p) in client.inserted if t == ir.ALIAS_TABLE]
        self.assertEqual(len(alias_inserts), 1)
        self.assertEqual(alias_inserts[0]["alias_text"], "AYAM BERSIH 30KG")
        self.assertEqual(alias_inserts[0]["created_via"], "fuzzy_auto")
        self.assertEqual(alias_inserts[0]["match_confidence"], 85)

    def test_resolve_exact_does_not_record(self):
        client = FakeClient(ALIASES, CANONICALS)
        cid, conf = resolve_item("AYAM BERSIH", client)
        self.assertEqual((cid, conf), (1, 100))
        self.assertEqual(client.inserted, [])

    def test_resolve_unknown_does_not_record(self):
        client = FakeClient(ALIASES, CANONICALS)
        self.assertEqual(resolve_item("NOBODY BUYS THIS", client), (None, 0))
        self.assertEqual(client.inserted, [])


# --- seed migration ---------------------------------------------------------

_CATEGORIES = (
    "protein_chicken|protein_meat|protein_seafood|protein_egg|rice|spices|"
    "oil_fats|vegetables_fresh|dairy_milk|beverages|packaging|cleaning_supplies|"
    "frozen_food|dry_goods|bakery|hardware|fuel|other"
)


class Migration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "migrations", "0010_item_normalisation.sql")) as f:
            cls.sql = f.read()
        cls.canonicals = re.findall(
            r"\(\s*'((?:[^']|'')+)'\s*,\s*'(" + _CATEGORIES + r")'\s*,\s*'[^']*'\s*\)",
            cls.sql,
        )

    def test_item_canonical_seeded(self):
        names = {r[0] for r in self.canonicals}
        self.assertGreaterEqual(len(names), 50)
        for expected in (
            "ayam bersih", "isi ayam", "jintan putih", "beras basmati",
            "curry powder fish", "santan", "tube ice", "lunch box",
            "minyak masak", "telur",
        ):
            self.assertIn(expected, names, f"{expected} missing from item seed")

    def test_categories_present(self):
        cats = {r[1] for r in self.canonicals}
        for c in ("protein_chicken", "spices", "packaging", "vegetables_fresh", "rice"):
            self.assertIn(c, cats)

    def test_alias_seeding(self):
        self.assertIn("SELECT display_name, id, 'seed' FROM public.item_canonical", self.sql)
        self.assertIn("CHICKEN WHOLE", self.sql)
        self.assertIn("KELAPA PARUT PUTIH", self.sql)
        self.assertIn("UNIQUE (receipt_id, item_index)", self.sql)  # item_resolutions
        self.assertIn("CREATE TABLE IF NOT EXISTS public.item_resolutions", self.sql)

    def test_does_not_populate_resolutions(self):
        # PR #32 creates item_resolutions empty; the backfill is PR #32b.
        self.assertNotIn("INSERT INTO public.item_resolutions", self.sql)


# --- bot wiring (source-level) ----------------------------------------------

class BotItemCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_telegram_commands_owner_only(self):
        for fn in (
            "item_list_command", "item_show_command", "item_coverage_command",
            "item_aliases_pending_command", "item_confirm_command",
            "item_reject_command", "item_add_alias_command",
        ):
            idx = self.src.index(f"async def {fn}(")
            body = self.src[idx:idx + 600]
            self.assertIn("is_reviewer(_command_owner_id(update))", body, f"{fn} not owner-gated")

    def test_commands_registered(self):
        for cmd, fn in (
            ("item_list", "item_list_command"),
            ("item_show", "item_show_command"),
            ("item_coverage", "item_coverage_command"),
            ("item_aliases_pending", "item_aliases_pending_command"),
            ("item_confirm", "item_confirm_command"),
            ("item_reject", "item_reject_command"),
            ("item_add_alias", "item_add_alias_command"),
        ):
            self.assertIn(f'CommandHandler("{cmd}", {fn})', self.src)


if __name__ == "__main__":
    unittest.main()
