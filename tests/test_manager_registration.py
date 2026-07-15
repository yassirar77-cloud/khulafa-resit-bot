"""Tests for outlet-manager registration (PR #67, Phase 1, Part 1)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manager_registration as mr
from tests.fake_supabase import FakeSupabase

# Mirror of the outlet_canonical registry: 10 active outlets (incl. One Bistro /
# S-SEK15) + 3 inactive partnership outlets that must be excluded from codes.
_OUTLET_REGISTRY = [
    ("S-BISTRO7", "Bistro", True),
    ("S-DAMANSARA", "D.U", True),
    ("S-JAKEL", "Jakel", True),
    ("S-KLANG", "Klang B.Emas", True),
    ("S-SBESI", "SBESI", True),
    ("S-SEK14", "Signature", True),
    ("S-SEK15", "One Bistro", True),
    ("S-SEK20", "SEK-20", True),
    ("S-SEK6", "SEK-6", True),
    ("S-VISTA", "Vista", True),
    ("S-ST KHU", "ST Khulafa", False),
    ("S-MB", "MB", False),
    ("S-RAZAK", "K.L Razak", False),
]
_ACTIVE_COUNT = sum(1 for _c, _n, active in _OUTLET_REGISTRY if active)


def _seed_outlets(sb):
    sb.table(mr.OUTLET_CANONICAL_TABLE).insert(
        [{"code": c, "canonical_name": n, "active": active}
         for c, n, active in _OUTLET_REGISTRY]
    ).execute()
    return sb


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

    def test_only_active_outlets_are_sourced(self):
        # Single source of truth: outlet_canonical WHERE active=true. Inactive
        # partnership outlets must NOT get codes.
        sb = _seed_outlets(FakeSupabase())
        outlets = mr.load_active_outlets(sb)
        self.assertEqual(len(outlets), _ACTIVE_COUNT)
        self.assertNotIn("ST KHU", {o.code for o in outlets})

    def test_gen_codes_one_per_active_outlet_includes_one_bistro(self):
        sb = _seed_outlets(FakeSupabase())
        codes = mr.create_registration_codes(sb, rng=random.Random(42))
        self.assertEqual(len(codes), _ACTIVE_COUNT)  # all 10, not 9
        outlet_codes = {c["outlet_code"] for c in codes}
        # One Bistro (S-SEK15 -> SEK15) was the dropped 10th outlet — now present.
        self.assertIn("SEK15", outlet_codes)
        self.assertIn("One Bistro", {c["display"] for c in codes})
        all_codes = [c["code"] for c in codes]
        self.assertEqual(len(all_codes), len(set(all_codes)))

    def test_sales_code_prefix_is_stripped(self):
        # S-SEK20 -> SEK20 so codes read SEK20-7K2A.
        sb = _seed_outlets(FakeSupabase())
        codes = {c["outlet_code"]: c["code"]
                 for c in mr.create_registration_codes(sb, rng=random.Random(1))}
        self.assertTrue(codes["SEK20"].startswith("SEK20-"))

    def test_regenerating_invalidates_prior_unused_codes(self):
        sb = _seed_outlets(FakeSupabase())
        mr.create_registration_codes(sb, rng=random.Random(1))
        mr.create_registration_codes(sb, rng=random.Random(2))
        # Only one unused code per active outlet should survive.
        unused = [r for r in sb.rows(mr.CODES_TABLE) if not r["used"]]
        self.assertEqual(len(unused), _ACTIVE_COUNT)


class Registration(unittest.TestCase):
    def setUp(self):
        self.sb = _seed_outlets(FakeSupabase())
        self.codes = {
            c["outlet_code"]: c["code"]
            for c in mr.create_registration_codes(self.sb, rng=random.Random(7))
        }

    def test_valid_code_maps_outlet_to_manager(self):
        code = self.codes["SEK20"]
        res = mr.register_manager(self.sb, code, "Aiman", 111)
        self.assertTrue(res["ok"])
        self.assertEqual(res["outlet_display"], "SEK-20")
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

    def test_concurrent_redemption_only_first_wins(self):
        # 2026-07 audit: the code burn is a compare-and-swap on used=False, so
        # a redemption that read the code as unused but lost the burn race is
        # rejected — simulate by pre-burning between the read and the burn.
        code = self.codes["VISTA"]
        norm = mr.normalize_code(code)

        class RacingClient:
            """Delegates to the fake, but marks the code used just before the
            first CAS burn executes — as a concurrent winner would."""
            def __init__(self, sb):
                self._sb = sb
                self._raced = False

            def table(self, name):
                q = self._sb.table(name)
                if name == mr.CODES_TABLE and not self._raced:
                    orig_update = q.update

                    def update(payload):
                        if payload.get("used") is True and not self._raced:
                            self._raced = True
                            # The concurrent winner burns first.
                            self._sb.table(mr.CODES_TABLE).update(
                                {"used": True, "used_by_chat_id": 999}
                            ).eq("code", norm).execute()
                        return orig_update(payload)

                    q.update = update
                return q

        res = mr.register_manager(RacingClient(self.sb), code, "Loser", 111)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], mr.USED_CODE_MESSAGE)
        # The loser never wrote a manager row.
        self.assertIsNone(mr.get_manager(self.sb, "VISTA"))

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
        for o in mr.load_active_outlets(self.sb):
            self.assertNotIn(o.code.lower(), low)
            self.assertNotIn(o.display.lower(), low)
        self.assertIsNone(mr.get_manager(self.sb, "SEK20"))

    def test_empty_code_rejected(self):
        self.assertFalse(mr.register_manager(self.sb, "", "X", 1)["ok"])
        self.assertFalse(mr.register_manager(self.sb, None, "X", 1)["ok"])


if __name__ == "__main__":
    unittest.main()
