"""Tests for merchant normalisation (PR #30).

The matching core is pure and tested directly. The DB-backed resolve and the
fuzzy auto-alias write use a recording fake client. Seed content is checked by
parsing the migration SQL, and the owner-only commands are checked source-level
(bot.py can't be imported in CI).
"""

import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import merchant_resolver as mr  # noqa: E402
from merchant_resolver import (  # noqa: E402
    compute_coverage,
    format_coverage_report,
    levenshtein,
    match_merchant,
    normalise_text,
    resolve_merchant,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALIASES = [
    {"id": 1, "alias_text": "EVEREST AISVARAM SDN BHD", "canonical_id": 1},
    {"id": 2, "alias_text": "EVEREST AISVARAM", "canonical_id": 1},
    {"id": 3, "alias_text": "EVEREST", "canonical_id": 1},
    {"id": 4, "alias_text": "BABAS PRODUCTS (M) SDN BHD", "canonical_id": 2},
    {"id": 5, "alias_text": "BABAS", "canonical_id": 2},
    {"id": 6, "alias_text": "MEWAH GROUP", "canonical_id": 3},
]
CANONICALS = [
    {"id": 1, "display_name": "EVEREST"},
    {"id": 2, "display_name": "BABAS"},
    {"id": 3, "display_name": "MEWAH"},
]


class Levenshtein(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(levenshtein("abc", "abc"), 0)

    def test_empty(self):
        self.assertEqual(levenshtein("", "abc"), 3)
        self.assertEqual(levenshtein("abc", ""), 3)

    def test_transposition_is_two(self):
        self.assertEqual(levenshtein("aisvaram", "aivsaram"), 2)


class NormaliseText(unittest.TestCase):
    def test_strips_punct_and_collapses_ws(self):
        self.assertEqual(normalise_text("BABAS  PRODUCTS  (M) SDN. BHD."), "babas products m sdn bhd")

    def test_empty(self):
        self.assertEqual(normalise_text(""), "")


class MatchMerchant(unittest.TestCase):
    def test_resolve_exact_match(self):
        self.assertEqual(match_merchant("BABAS PRODUCTS (M) SDN BHD", ALIASES, CANONICALS), (2, 100))

    def test_resolve_case_insensitive(self):
        self.assertEqual(match_merchant("babas products (m) sdn bhd", ALIASES, CANONICALS), (2, 95))

    def test_resolve_normalised_punctuation(self):
        self.assertEqual(match_merchant("BABAS PRODUCTS (M) SDN. BHD.", ALIASES, CANONICALS), (2, 90))

    def test_resolve_fuzzy_typo(self):
        # "AIVSARAM" is a 2-edit transposition of alias "AISVARAM".
        self.assertEqual(match_merchant("EVEREST AIVSARAM", ALIASES, CANONICALS), (1, 80))

    def test_resolve_unknown_returns_none(self):
        self.assertEqual(match_merchant("RANDOM XYZ VENDOR", ALIASES, CANONICALS), (None, 0))

    def test_levenshtein_threshold_2(self):
        # "MEWAH GRABS" is distance 3 from alias "MEWAH GROUP" (not <=2) and
        # distance 6 from display "MEWAH" (not <=5) -> no match.
        self.assertEqual(levenshtein(normalise_text("MEWAH GROUP"), normalise_text("MEWAH GRABS")), 3)
        self.assertEqual(match_merchant("MEWAH GRABS", ALIASES, CANONICALS), (None, 0))

    def test_fuzzy_canonical_display_name_tier(self):
        # 1-edit from display "MEWAH" but no close alias -> 60 via display tier.
        self.assertEqual(match_merchant("MEWAHH", ALIASES, CANONICALS), (3, 60))

    def test_empty_input(self):
        self.assertEqual(match_merchant("", ALIASES, CANONICALS), (None, 0))
        self.assertEqual(match_merchant("   ", ALIASES, CANONICALS), (None, 0))


# --- DB-backed resolve with a recording fake client -------------------------

class _FakeQuery:
    def __init__(self, parent, table):
        self.p = parent
        self.t = table
        self.op = "select"
        self.payload = None

    def select(self, *a, **k):
        self.op = "select"
        return self

    def eq(self, *a, **k):
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        return self

    def execute(self):
        if self.op == "insert":
            self.p.inserted.append((self.t, self.payload))
            return types.SimpleNamespace(data=[self.payload])
        if self.t == mr.ALIAS_TABLE:
            return types.SimpleNamespace(data=list(self.p.aliases))
        if self.t == mr.CANONICAL_TABLE:
            return types.SimpleNamespace(data=list(self.p.canonicals))
        return types.SimpleNamespace(data=[])


class FakeClient:
    def __init__(self, aliases, canonicals):
        self.aliases = aliases
        self.canonicals = canonicals
        self.inserted = []

    def table(self, name):
        return _FakeQuery(self, name)


class ResolveMerchant(unittest.TestCase):
    def test_resolve_creates_alias_on_fuzzy_match(self):
        client = FakeClient(ALIASES, CANONICALS)
        cid, conf = resolve_merchant("EVEREST AIVSARAM", client)
        self.assertEqual((cid, conf), (1, 80))
        alias_inserts = [p for (t, p) in client.inserted if t == mr.ALIAS_TABLE]
        self.assertEqual(len(alias_inserts), 1)
        self.assertEqual(alias_inserts[0]["alias_text"], "EVEREST AIVSARAM")
        self.assertEqual(alias_inserts[0]["created_via"], "fuzzy_auto")
        self.assertEqual(alias_inserts[0]["match_confidence"], 80)

    def test_exact_match_does_not_create_alias(self):
        client = FakeClient(ALIASES, CANONICALS)
        cid, conf = resolve_merchant("EVEREST AISVARAM SDN BHD", client)
        self.assertEqual((cid, conf), (1, 100))
        self.assertEqual(client.inserted, [])

    def test_unknown_does_not_create_alias(self):
        client = FakeClient(ALIASES, CANONICALS)
        cid, conf = resolve_merchant("TOTALLY UNKNOWN VENDOR", client)
        self.assertEqual((cid, conf), (None, 0))
        self.assertEqual(client.inserted, [])


class Coverage(unittest.TestCase):
    def test_compute_coverage_counts_and_top_unresolved(self):
        merchant_counts = [
            ("EVEREST AISVARAM SDN BHD", 10),  # exact -> resolved
            ("EVEREST AIVSARAM", 3),           # fuzzy -> resolved
            ("WEIRD VENDOR A", 7),             # unresolved
            ("WEIRD VENDOR B", 12),            # unresolved (highest count)
        ]
        summary = compute_coverage(merchant_counts, ALIASES, CANONICALS)
        self.assertEqual(summary["total_unique"], 4)
        self.assertEqual(summary["resolved"], 2)
        self.assertEqual(summary["unresolved"], 2)
        # Sorted by occurrence count, descending.
        self.assertEqual(summary["top_unresolved"][0], ("WEIRD VENDOR B", 12))
        self.assertEqual(summary["top_unresolved"][1], ("WEIRD VENDOR A", 7))

    def test_compute_coverage_caps_top_at_20(self):
        merchant_counts = [(f"UNKNOWN {i}", i) for i in range(30)]
        summary = compute_coverage(merchant_counts, ALIASES, CANONICALS)
        self.assertEqual(summary["unresolved"], 30)
        self.assertEqual(len(summary["top_unresolved"]), 20)

    def test_coverage_is_read_only(self):
        # compute_coverage must not write fuzzy aliases.
        client = FakeClient(ALIASES, CANONICALS)
        compute_coverage([("EVEREST AIVSARAM", 1)], client.aliases, client.canonicals)
        self.assertEqual(client.inserted, [])

    def test_format_coverage_report(self):
        summary = {
            "total_unique": 3, "resolved": 1, "unresolved": 2,
            "top_unresolved": [("VENDOR X", 5), ("VENDOR Y", 2)],
        }
        out = format_coverage_report(summary)
        self.assertIn("unique merchants: 3", out)
        self.assertIn("resolved (any confidence): 1", out)
        self.assertIn("unresolved (confidence 0): 2", out)
        self.assertIn("VENDOR X", out)


# --- seed content (parse the migration SQL) ---------------------------------

class MigrationSeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_ROOT, "migrations", "0007_merchant_normalisation.sql")
        with open(path) as f:
            cls.sql = f.read()
        # Canonical rows: ('DISPLAY', 'LEGAL', 'category') with an optional 4th
        # notes column: ('DISPLAY', 'LEGAL', 'category', 'notes').
        cls.canonical_rows = re.findall(
            r"\(\s*'((?:[^']|'')+)'\s*,\s*'((?:[^']|'')+)'\s*,\s*"
            r"'(supplier|utility|rent_license|internal_transfer|staff_advance|petty_cash|unknown)'"
            r"(?:\s*,\s*'(?:[^']|'')*')?\s*\)",
            cls.sql,
        )

    def test_merchant_canonical_seeded(self):
        names = {r[0] for r in self.canonical_rows}
        # 51 canonicals after the review additions (46 + 4 suppliers + 1 strata).
        self.assertEqual(len(names), 51)
        for expected in (
            "EVEREST", "BABAS", "MYMOON", "PVS SANTAN", "TNB", "AIR SELANGOR",
            "UNIFI", "KHULAFA BISTRO", "KHULAFA GROUP",
            "S. THAYANI", "RK MUBARAKA", "AKS SHAZZ", "SWEETTI FREEZEE",
            "VISTA ALAM JMB",
        ):
            self.assertIn(expected, names, f"{expected} missing from canonical seed")

    def test_categories_present(self):
        cats = {r[2] for r in self.canonical_rows}
        self.assertIn("supplier", cats)
        self.assertIn("utility", cats)
        self.assertIn("internal_transfer", cats)
        self.assertIn("rent_license", cats)

    def test_merchant_alias_seeded_for_every_canonical(self):
        # The two bulk INSERT...SELECT statements guarantee every canonical
        # gets its display_name (and legal_name) as a seed alias.
        self.assertIn("SELECT display_name, id, 'seed' FROM public.merchant_canonical", self.sql)
        self.assertIn("SELECT legal_name, id, 'seed' FROM public.merchant_canonical", self.sql)

    def test_known_typo_alias_seeded(self):
        self.assertIn("EVEREST AIVSARAM", self.sql)

    def test_receipts_column_added_not_populated(self):
        self.assertIn("ADD COLUMN IF NOT EXISTS merchant_canonical_id", self.sql)
        self.assertIn("idx_receipts_merchant_canonical", self.sql)
        # PR #30 must not UPDATE receipts (that's PR #31).
        self.assertNotIn("UPDATE public.receipts", self.sql)

    def test_alias_unique_and_created_via_constraint(self):
        self.assertIn("UNIQUE (alias_text)", self.sql)
        self.assertIn("'seed', 'manual', 'fuzzy_auto', 'fuzzy_confirmed'", self.sql)


# --- bot wiring (source-level) ----------------------------------------------

class BotMerchantCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_telegram_commands_owner_only(self):
        for fn in (
            "merchant_coverage_command", "merchant_list_command",
            "merchant_show_command", "merchant_aliases_pending_command",
            "merchant_confirm_command", "merchant_reject_command",
            "merchant_add_alias_command",
        ):
            idx = self.src.index(f"async def {fn}(")
            body = self.src[idx:idx + 600]
            self.assertIn(
                "is_reviewer(_command_owner_id(update))", body,
                f"{fn} is not owner-gated",
            )

    def test_commands_registered(self):
        for cmd, fn in (
            ("merchant_coverage", "merchant_coverage_command"),
            ("merchant_list", "merchant_list_command"),
            ("merchant_show", "merchant_show_command"),
            ("merchant_aliases_pending", "merchant_aliases_pending_command"),
            ("merchant_confirm", "merchant_confirm_command"),
            ("merchant_reject", "merchant_reject_command"),
            ("merchant_add_alias", "merchant_add_alias_command"),
        ):
            self.assertIn(f'CommandHandler("{cmd}", {fn})', self.src)


if __name__ == "__main__":
    unittest.main()
