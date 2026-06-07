"""Field-by-field Qwen-vs-GLM shadow scoring (ocr_shadow_fields)."""
import unittest

import ocr_shadow_fields as osf


class UnitExtractionTests(unittest.TestCase):
    def test_extracts_pack_unit_from_name(self):
        self.assertEqual(osf.extract_unit("AYAM 4 KG"), "kg")
        self.assertEqual(osf.extract_unit("KICAP 2.4 KG"), "kg")
        self.assertEqual(osf.extract_unit("PETRONAS 14KG"), "kg")
        self.assertEqual(osf.extract_unit("SUSU 1 TIN"), "tin")
        self.assertIsNone(osf.extract_unit("SANTAN"))
        self.assertIsNone(osf.extract_unit(None))


class AlignTests(unittest.TestCase):
    def test_pairs_by_canonical(self):
        glm = [{"name": "AYAM 4 KG", "qty": 4, "price": 36.0}]
        qwen = [{"name": "AYAM SEGAR", "qty": 4, "price": 36.0}]
        pairs = osf.align_items(glm, qwen)
        self.assertEqual(len(pairs), 1)
        self.assertIsNotNone(pairs[0][0])
        self.assertIsNotNone(pairs[0][1])

    def test_unpaired_lines_visible(self):
        glm = [{"name": "AYAM", "qty": 1, "price": 9.0}]
        qwen = [{"name": "UDANG", "qty": 1, "price": 30.0}]
        pairs = osf.align_items(glm, qwen)
        # both appear, each unpaired (one side None)
        self.assertEqual(len(pairs), 2)
        self.assertTrue(any(g is not None and q is None for g, q in pairs))
        self.assertTrue(any(g is None and q is not None for g, q in pairs))


class FieldRowTests(unittest.TestCase):
    def test_agreement_rows_without_manual(self):
        glm = [{"name": "AYAM 4 KG", "qty": 4, "price": 36.0}]
        qwen = [{"name": "AYAM 4 KG", "qty": 4, "price": 36.0}]
        rows = osf.field_rows(1, glm, qwen)
        self.assertEqual(len(rows), 4)  # item, qty, unit, price
        self.assertTrue(all(r["match"] == osf.MATCH_AGREE for r in rows))

    def test_disagreement_on_qty(self):
        glm = [{"name": "AYAM", "qty": 4, "price": 36.0}]
        qwen = [{"name": "AYAM", "qty": 5, "price": 36.0}]
        rows = osf.field_rows(1, glm, qwen)
        qty_row = next(r for r in rows if r["field"] == "qty")
        self.assertEqual(qty_row["match"], osf.MATCH_DISAGREE)

    def test_manual_picks_winner(self):
        glm = [{"name": "AYAM", "qty": 5, "price": 36.0}]
        qwen = [{"name": "AYAM", "qty": 4, "price": 36.0}]
        manual = [{"name": "AYAM", "qty": 4, "price": 36.0}]
        rows = osf.field_rows(1, glm, qwen, manual_items=manual)
        qty_row = next(r for r in rows if r["field"] == "qty")
        self.assertEqual(qty_row["match"], osf.MATCH_QWEN)


class ScoreTests(unittest.TestCase):
    def test_no_manual_verdict(self):
        rows = osf.field_rows(1,
                              [{"name": "AYAM", "qty": 4, "price": 9.0}],
                              [{"name": "AYAM", "qty": 4, "price": 9.0}])
        s = osf.score(rows)
        self.assertEqual(s["verdict"], "NO_MANUAL")
        self.assertEqual(s["per_field"]["qty"]["agreement_rate"], 1.0)

    def test_qwen_wins_with_manual(self):
        glm = [{"name": "AYAM", "qty": 5, "price": 9.0}]
        qwen = [{"name": "AYAM", "qty": 4, "price": 9.0}]
        manual = [{"name": "AYAM", "qty": 4, "price": 9.0}]
        s = osf.score(osf.field_rows(1, glm, qwen, manual_items=manual))
        self.assertEqual(s["verdict"], "QWEN_WINS")
        self.assertIn("switch", s["decision"].lower())

    def test_qwen_loses_with_manual(self):
        glm = [{"name": "AYAM", "qty": 4, "price": 9.0}]
        qwen = [{"name": "AYAM", "qty": 7, "price": 9.0}]
        manual = [{"name": "AYAM", "qty": 4, "price": 9.0}]
        s = osf.score(osf.field_rows(1, glm, qwen, manual_items=manual))
        self.assertEqual(s["verdict"], "QWEN_LOSES")


if __name__ == "__main__":
    unittest.main()
