"""Tests for the Qwen SHADOW OCR comparison tooling.

Behavioural tests run against the pure helpers (response parsing, confidence
scoring, summary maths). The hard safety rails — feature-flag gating and the
guarantee that NO production module imports the shadow path — are checked
source-level, mirroring the project's other bot-gating tests.
"""

import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from qwen_ocr_shadow import parse_qwen_response, shadow_enabled  # noqa: E402

# scripts/ isn't a package; load the summary module by path for its pure helpers.
_summary_path = os.path.join(REPO_ROOT, "scripts", "qwen_shadow_summary.py")
_spec = importlib.util.spec_from_file_location("qwen_shadow_summary", _summary_path)
qwen_shadow_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qwen_shadow_summary)
summarize = qwen_shadow_summary.summarize


class ParseQwenResponseTests(unittest.TestCase):
    def test_parses_clean_json_full_confidence(self):
        # In-window date and a total that reconciles with the single item, so
        # no ocr_quality penalties fire and all four fields score.
        content = (
            '{"merchant": "BESTARI FARM", "date": "2026-05-20", "total": 50.0, '
            '"currency": "MYR", "items": [{"name": "Ayam", "qty": 1, "price": 50.0}], '
            '"raw_text": "BESTARI FARM\\nTarikh: 20/05/2026\\nTOTAL RM50.00"}'
        )
        parsed = parse_qwen_response(content)
        self.assertEqual(parsed["merchant"], "BESTARI FARM")
        self.assertEqual(parsed["total"], 50.0)
        self.assertEqual(parsed["receipt_date"], "2026-05-20")
        self.assertEqual(len(parsed["items"]), 1)
        # 30 merchant + 30 total + 20 date + 20 items, no penalties.
        self.assertEqual(parsed["confidence"], 100)

    def test_strips_markdown_code_fence(self):
        content = '```json\n{"merchant": "X", "total": 10, "items": [], "raw_text": ""}\n```'
        parsed = parse_qwen_response(content)
        self.assertEqual(parsed["merchant"], "X")
        self.assertEqual(parsed["total"], 10)

    def test_non_json_yields_zero_confidence(self):
        parsed = parse_qwen_response("sorry, I cannot read this image")
        self.assertEqual(parsed["confidence"], 0)
        self.assertIsNone(parsed["total"])
        self.assertEqual(parsed["raw_text"], "sorry, I cannot read this image")

    def test_bare_string_items_coerced_to_dicts(self):
        content = '{"merchant": "X", "total": 5, "items": ["Ice", "Water"], "raw_text": ""}'
        parsed = parse_qwen_response(content)
        self.assertEqual([i["name"] for i in parsed["items"]], ["Ice", "Water"])
        self.assertTrue(all(isinstance(i, dict) for i in parsed["items"]))

    def test_missing_fields_lower_confidence(self):
        content = '{"merchant": null, "total": null, "items": [], "raw_text": ""}'
        parsed = parse_qwen_response(content)
        self.assertEqual(parsed["confidence"], 0)


class SummarizeTests(unittest.TestCase):
    def _rows(self):
        return [
            # totals differ a lot, qwen more confident
            {"receipt_id": 1, "glm_total": 5000, "qwen_total": 50,
             "glm_confidence": 40, "qwen_confidence": 90,
             "glm_date": "2025-01-01", "qwen_date": "2025-01-01"},
            # totals agree (within tolerance), confidence equal
            {"receipt_id": 2, "glm_total": 100.00, "qwen_total": 100.001,
             "glm_confidence": 80, "qwen_confidence": 80,
             "glm_date": None, "qwen_date": None},
            # small disagreement, glm more confident
            {"receipt_id": 3, "glm_total": 42.00, "qwen_total": 40.00,
             "glm_confidence": 70, "qwen_confidence": 60,
             "glm_date": None, "qwen_date": None},
            # qwen produced no total -> missing, not disagreement
            {"receipt_id": 4, "glm_total": 20.00, "qwen_total": None,
             "glm_confidence": 30, "qwen_confidence": 0,
             "glm_date": None, "qwen_date": None},
        ]

    def test_counts(self):
        s = summarize(self._rows(), top=10, tolerance=0.01)
        self.assertEqual(s["compared"], 4)
        self.assertEqual(s["total_disagree"], 2)   # receipts 1 and 3
        self.assertEqual(s["total_agree"], 1)      # receipt 2
        self.assertEqual(s["missing_total"], 1)    # receipt 4
        self.assertEqual(s["qwen_more_confident"], 1)  # receipt 1

    def test_biggest_delta_ordering_and_top(self):
        s = summarize(self._rows(), top=1, tolerance=0.01)
        self.assertEqual(len(s["biggest_deltas"]), 1)
        self.assertEqual(s["biggest_deltas"][0]["receipt_id"], 1)
        self.assertAlmostEqual(s["biggest_deltas"][0]["delta"], 4950.0)


class FeatureFlagTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("QWEN_SHADOW_ENABLED")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("QWEN_SHADOW_ENABLED", None)
        else:
            os.environ["QWEN_SHADOW_ENABLED"] = self._saved

    def test_disabled_by_default(self):
        os.environ.pop("QWEN_SHADOW_ENABLED", None)
        self.assertFalse(shadow_enabled())

    def test_enabled_with_truthy_values(self):
        for val in ("1", "true", "YES", "on"):
            os.environ["QWEN_SHADOW_ENABLED"] = val
            self.assertTrue(shadow_enabled(), val)

    def test_disabled_with_falsy_values(self):
        for val in ("0", "false", "", "no"):
            os.environ["QWEN_SHADOW_ENABLED"] = val
            self.assertFalse(shadow_enabled(), val)


class SafetyRailTests(unittest.TestCase):
    """No production module may import the shadow OCR path."""

    PRODUCTION_MODULES = ["bot.py", "digest.py", "digest_data.py", "reparse.py"]

    def test_production_does_not_import_qwen(self):
        for name in self.PRODUCTION_MODULES:
            path = os.path.join(REPO_ROOT, name)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(
                "qwen_ocr_shadow", src,
                f"{name} must not import the shadow OCR module (live path stays GLM)",
            )
            self.assertNotIn("qwen_shadow", src, f"{name} must not reference shadow tooling")


if __name__ == "__main__":
    unittest.main()
