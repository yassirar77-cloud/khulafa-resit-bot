"""Tests for outlet-manager registration (PR #67, Phase 1, Part 1)."""

import os
import random
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manager_registration as mr
from tests.fake_supabase import FakeSupabase


class CodeGeneration(unittest.TestCase):
    def test_code_format_is_prefix_dash_four_chars(self):
        # SEK20-7K2A shape: outlet prefix, dash, 4 unambiguous alnum chars.
        code = mr.generate_registration_code("SEK20", rng=random.Random(1))
        self.assertRegex(code, r"^SEK20-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}$")

    def test_code_excludes_ambiguous_characters(self):
        rng = random.Random(0)
        for _ in range(200):
            suffix = mr.generate_registration_code("VISTA", rng=rng).split("-")[1]
            self.assertFalse(set(suffix) & set("01OIL"))

    def test_gen_codes_one_per_outlet_all_unique(self):
        sb = FakeSupabase()
        codes = mr.create_registration_codes(sb, rng=random.Random(42))
        self.assertEqual(len(codes), len(mr.OUTLETS))
        self.assertEqual(
            {c["outlet_code"] for c in codes},
            {o.code for o in mr.OUTLETS},
        )
        all_codes = [c["code"] for c in codes]
        self.assertEqual(len(all_codes), len(set(all_codes)))

    def test_regenerating_invalidates_prior_unused_codes(self):
        sb = FakeSupabase()
        mr.create_registration_codes(sb, rng=random.Random(1))
        mr.create_registration_codes(sb, rng=random.Random(2))
        # Only one unused code per outlet should survive.
        unused = [r for r in sb.rows(mr.CODES_TABLE) if not r["used"]]
        self.assertEqual(len(unused), len(mr.OUTLETS))


class Registration(unittest.TestCase):
    def setUp(self):
        self.sb = FakeSupabase()
        self.codes = {
            c["outlet_code"]: c["code"]
            for c in mr.create_registration_codes(self.sb, rng=random.Random(7))
        }

    def test_valid_code_maps_outlet_to_manager(self):
        code = self.codes["SEK20"]
        res = mr.register_manager(self.sb, code, "Aiman", 111)
        self.assertTrue(res["ok"])
        self.assertEqual(res["outlet_code"], "SEK20")
        mgr = mr.get_manager(self.sb, "SEK20")
        self.assertEqual(mgr["chat_id"], 111)
        self.assertEqual(mgr["manager_name"], "Aiman")

    def test_code_is_case_insensitive_and_trimmed(self):
        code = self.codes["KLANG"].lower()
        res = mr.register_manager(self.sb, f"  {code}  ", "Bala", 222)
        self.assertTrue(res["ok"])
        self.assertEqual(res["outlet_code"], "KLANG")

    def test_used_code_is_rejected(self):
        code = self.codes["VISTA"]
        self.assertTrue(mr.register_manager(self.sb, code, "X", 1)["ok"])
        res = mr.register_manager(self.sb, code, "Y", 2)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], mr.USED_CODE_MESSAGE)
        # The first manager still stands; the replay did not overwrite.
        self.assertEqual(mr.get_manager(self.sb, "VISTA")["chat_id"], 1)

    def test_reregister_replaces_existing_manager(self):
        # Staff turnover: a NEW code for the same outlet swaps the manager out.
        first = self.codes["JAKEL"]
        self.assertTrue(mr.register_manager(self.sb, first, "OldMgr", 100)["ok"])
        new_code = mr.create_registration_codes(self.sb, rng=random.Random(9))
        jakel_new = next(c["code"] for c in new_code if c["outlet_code"] == "JAKEL")
        self.assertTrue(mr.register_manager(self.sb, jakel_new, "NewMgr", 200)["ok"])
        mgr = mr.get_manager(self.sb, "JAKEL")
        self.assertEqual(mgr["chat_id"], 200)
        self.assertEqual(mgr["manager_name"], "NewMgr")
        # Exactly one manager row for the outlet — no stacking.
        rows = [r for r in self.sb.rows(mr.MANAGERS_TABLE) if r["outlet_code"] == "JAKEL"]
        self.assertEqual(len(rows), 1)

    def test_bad_code_clean_error_no_outlet_leak(self):
        res = mr.register_manager(self.sb, "NOPE-XXXX", "Hacker", 999)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], mr.INVALID_CODE_MESSAGE)
        # The error must not leak any outlet name or code.
        low = res["error"].lower()
        for o in mr.OUTLETS:
            self.assertNotIn(o.code.lower(), low)
            self.assertNotIn(o.display.lower(), low)
        self.assertIsNone(mr.get_manager(self.sb, "SEK20"))

    def test_empty_code_rejected(self):
        self.assertFalse(mr.register_manager(self.sb, "", "X", 1)["ok"])
        self.assertFalse(mr.register_manager(self.sb, None, "X", 1)["ok"])


if __name__ == "__main__":
    unittest.main()
