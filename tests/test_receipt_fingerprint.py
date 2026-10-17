"""receipt_fingerprint: image hash, content key, matching and the texts."""
import unittest
from datetime import datetime, timezone
from io import BytesIO

import receipt_fingerprint as rf
from tests.fake_supabase import FakeSupabase

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)


def _photo(seed: int, size=(240, 320), fmt="JPEG", quality=85) -> bytes:
    """A deterministic 'receipt' with a few dark bars; seed moves the bars."""
    img = Image.new("L", size, 235)
    d = ImageDraw.Draw(img)
    for i in range(8):
        y = 20 + i * 34 + (seed * 7) % 20
        d.rectangle([20 + (seed % 5) * 3, y, 200 - (i * 9) % 60, y + 10], fill=20 + i * 10)
    buf = BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return buf.getvalue()


def _other_photo(size=(240, 320)) -> bytes:
    """A visibly different receipt: vertical stripes and a dark footer."""
    img = Image.new("L", size, 240)
    d = ImageDraw.Draw(img)
    for x in range(10, 230, 24):
        d.rectangle([x, 10, x + 8, 250], fill=30)
    d.rectangle([0, 260, 240, 320], fill=60)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _fp(**kw):
    base = {"receipt_id": 1, "chat_id": -100, "outlet": "SEK20", "dhash": "0f0f0f0f0f0f0f0f",
            "merchant": "JAKEL", "receipt_date": "2026-10-14", "total": 181.0, "invoice_no": None}
    base.update(kw)
    return base


@unittest.skipIf(Image is None, "Pillow not installed")
class DHash(unittest.TestCase):
    def test_same_picture_recompressed_matches_closely(self):
        a = rf.dhash(_photo(1))
        b = rf.dhash(_photo(1, quality=40))                 # Telegram recompresses
        self.assertEqual(len(a), 16)
        self.assertLessEqual(rf.hamming(a, b), 4)

    def test_resized_copy_still_matches(self):
        a = rf.dhash(_photo(2))
        img = Image.open(BytesIO(_photo(2))).resize((120, 160))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=80)
        self.assertLessEqual(rf.hamming(a, rf.dhash(buf.getvalue())), 6)

    def test_different_pictures_are_far_apart(self):
        a, b = rf.dhash(_photo(1)), rf.dhash(_other_photo())
        self.assertGreater(rf.hamming(a, b), 10)

    def test_not_an_image_gives_none_never_raises(self):
        self.assertIsNone(rf.dhash(b"not a picture"))
        self.assertIsNone(rf.dhash(b""))
        self.assertIsNone(rf.dhash(None))


class Hamming(unittest.TestCase):
    def test_counts_bits(self):
        self.assertEqual(rf.hamming("00", "ff"), 8)
        self.assertEqual(rf.hamming("0f0f", "0f0e"), 1)

    def test_missing_or_malformed(self):
        self.assertIsNone(rf.hamming(None, "00"))
        self.assertIsNone(rf.hamming("zz", "00"))
        self.assertIsNone(rf.hamming("000", "00"))


class InvoiceNo(unittest.TestCase):
    def test_common_layouts(self):
        self.assertEqual(rf.invoice_no("JAKEL TRADING\nINVOICE NO: INV-2026/0042\nDate 14/10/26"), "INV20260042")
        self.assertEqual(rf.invoice_no("D.O. # 88123  Delivery to SEK 20"), "88123")
        self.assertEqual(rf.invoice_no("Resit No 004512"), "004512")
        self.assertEqual(rf.invoice_no("Tax Invoice: TI0098 Total RM 55.00"), "TI0098")
        self.assertEqual(rf.invoice_no("CASH BILL No. CB-7781"), "CB7781")

    def test_needs_three_digits_and_a_label(self):
        self.assertIsNone(rf.invoice_no("Bill 2 of 3"))
        self.assertIsNone(rf.invoice_no("Table 12  Total 45.00"))
        self.assertIsNone(rf.invoice_no(None))
        self.assertIsNone(rf.invoice_no(""))


class Matching(unittest.TestCase):
    def test_same_photo_in_another_chat_is_a_cross_chat_match(self):
        new = _fp(receipt_id=9, chat_id=-200, outlet="KLANG", dhash="0f0f0f0f0f0f0f0e")
        matches = rf.find_matches(new, [_fp()], max_dist=6, min_content_total=100)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["kind"], "same_photo")
        self.assertTrue(matches[0]["cross_chat"])
        self.assertEqual(matches[0]["distance"], 1)
        director, group = rf.split_matches(matches)
        self.assertEqual(len(director), 1)
        self.assertEqual(group, [])

    def test_same_photo_in_the_same_chat_goes_to_the_group_only(self):
        new = _fp(receipt_id=9, dhash="0f0f0f0f0f0f0f0f")
        matches = rf.find_matches(new, [_fp()], max_dist=6, min_content_total=100)
        director, group = rf.split_matches(matches)
        self.assertEqual(director, [])
        self.assertEqual([m["receipt_id"] for m in group], [1])

    def test_same_invoice_number_different_picture(self):
        old = _fp(invoice_no="INV20260042", dhash="0000000000000000")
        new = _fp(receipt_id=9, chat_id=-200, invoice_no="INV20260042", dhash="ffffffffffffffff",
                  total=181.5, receipt_date="2026-10-15")
        matches = rf.find_matches(new, [old], max_dist=6, min_content_total=100)
        self.assertEqual([m["kind"] for m in matches], ["same_invoice"])

    def test_different_invoice_numbers_never_match_on_content(self):
        old = _fp(invoice_no="INV0001", dhash="0000000000000000")
        new = _fp(receipt_id=9, chat_id=-200, invoice_no="INV0002", dhash="ffffffffffffffff")
        self.assertEqual(rf.find_matches(new, [old], max_dist=6, min_content_total=100), [])

    def test_same_bill_needs_date_total_and_min_total(self):
        old = _fp(dhash="0000000000000000")
        new = _fp(receipt_id=9, chat_id=-200, dhash="ffffffffffffffff", total=182.0)
        self.assertEqual([m["kind"] for m in rf.find_matches(new, [old], max_dist=6, min_content_total=100)],
                         ["same_bill"])
        # Below the floor: a daily roti bill of RM45 at two outlets is normal.
        small_old, small_new = dict(old, total=45.0), dict(new, total=45.0)
        self.assertEqual(rf.find_matches(small_new, [small_old], max_dist=6, min_content_total=100), [])
        # Different day or total off by more than the tolerance: no match.
        self.assertEqual(rf.find_matches(dict(new, receipt_date="2026-10-13"), [old], max_dist=6,
                                         min_content_total=100), [])
        self.assertEqual(rf.find_matches(dict(new, total=190.0), [old], max_dist=6,
                                         min_content_total=100), [])

    def test_content_checks_never_run_inside_one_chat(self):
        old = _fp(dhash="0000000000000000", invoice_no="INV0001")
        new = _fp(receipt_id=9, dhash="ffffffffffffffff", invoice_no="INV0001")
        self.assertEqual(rf.find_matches(new, [old], max_dist=6, min_content_total=100), [])

    def test_own_row_is_skipped_and_cross_chat_ranks_first(self):
        same = _fp(receipt_id=2, chat_id=-200, dhash="0f0f0f0f0f0f0f0f")
        own = _fp(receipt_id=9)
        local = _fp(receipt_id=3, dhash="0f0f0f0f0f0f0f0f")
        new = _fp(receipt_id=9, dhash="0f0f0f0f0f0f0f0f")
        got = rf.find_matches(new, [own, local, same], max_dist=6, min_content_total=100)
        self.assertEqual([m["receipt_id"] for m in got], [2, 3])


class Texts(unittest.TestCase):
    def test_director_alert_names_both_bills(self):
        new = _fp(receipt_id=9, chat_id=-200, outlet="KLANG", receipt_date="2026-10-15")
        m = {"kind": "same_photo", "receipt_id": 1, "chat_id": -100, "outlet": "SEK20",
             "receipt_date": "2026-10-14", "total": 181.0, "distance": 2, "cross_chat": True}
        text = rf.format_director_alert(new, [m])
        self.assertIn("Same bill at two outlets?", text)
        self.assertIn("JAKEL RM181.00", text)
        self.assertIn("#9 KLANG — this upload", text)
        self.assertIn("#1 SEK20, 14 Oct 2026, RM181.00 — same photo, distance 2", text)
        self.assertNotIn("same_bill =", text)

    def test_same_bill_alert_asks_to_check_the_photos(self):
        new = _fp(receipt_id=9, chat_id=-200, outlet="KLANG")
        m = {"kind": "same_bill", "receipt_id": 1, "chat_id": -100, "outlet": "SEK20",
             "receipt_date": "2026-10-14", "total": 181.0, "distance": None, "cross_chat": True}
        self.assertIn("check the two photos", rf.format_director_alert(new, [m]))

    def test_group_reply_has_no_money(self):
        text = rf.format_group_reply({"receipt_id": 1, "receipt_date": "2026-10-14"})
        self.assertIn("#1", text)
        self.assertIn("Tak perlu hantar lagi", text)
        self.assertNotIn("RM", text)

    def test_stats(self):
        self.assertIn("No bill submitted at two outlets", rf.format_stats([], 30))
        rows = [{"receipt_id": 9, "outlet": "KLANG", "merchant": "JAKEL", "total": 181,
                 "receipt_date": "2026-10-15", "matched_receipt_id": 1, "match_kind": "same_photo",
                 "match_outlet": "SEK20"}]
        text = rf.format_stats(rows, 30)
        self.assertIn("#9 KLANG ↔ #1 SEK20: JAKEL RM181.00, 15 Oct 2026 (same photo)", text)


class CheckAndRecord(unittest.TestCase):
    def test_records_and_reports_cross_chat_match(self):
        db = FakeSupabase()
        first = {"id": 1, "chat_id": -100, "outlet": "SEK20", "merchant": "Jakel  Trading",
                 "receipt_date": "2026-10-14", "total": "181.00"}
        out = rf.check_and_record(db, first, {"raw_text": "INVOICE NO: 4421"}, "0f0f0f0f0f0f0f0f", now=NOW)
        self.assertEqual(out["director"], [])
        row = db.rows(rf.TABLE)[0]
        self.assertEqual(row["merchant"], "JAKEL TRADING")
        self.assertEqual(row["invoice_no"], "4421")
        self.assertEqual(row["total"], 181.0)
        self.assertNotIn("matched_receipt_id", row)

        second = {"id": 2, "chat_id": -200, "outlet": "KLANG", "merchant": "JAKEL TRADING",
                  "receipt_date": "2026-10-15", "total": 181.0}
        out = rf.check_and_record(db, second, {"raw_text": "INVOICE NO: 4421"}, "ffffffffffffffff", now=NOW)
        self.assertEqual([m["kind"] for m in out["director"]], ["same_invoice"])
        self.assertEqual(out["group"], [])
        rows = db.rows(rf.TABLE)
        self.assertEqual(rows[1]["matched_receipt_id"], 1)
        self.assertEqual(rows[1]["match_kind"], "same_invoice")
        self.assertEqual(rows[1]["match_outlet"], "SEK20")
        self.assertEqual(len(rf.fetch_matched(db, NOW.replace(day=1))), 1)

    def test_receipt_without_id_is_not_saved(self):
        db = FakeSupabase()
        out = rf.check_and_record(db, {"chat_id": -100, "merchant": "X"}, None, "00", now=NOW)
        self.assertEqual(out["director"], [])
        self.assertEqual(db.rows(rf.TABLE), [])

    def test_fingerprint_row_tolerates_missing_fields(self):
        row = rf.fingerprint_row({"id": 5}, None, None, now=NOW)
        self.assertEqual(row["receipt_id"], 5)
        self.assertIsNone(row["merchant"])
        self.assertIsNone(row["receipt_date"])
        self.assertIsNone(row["total"])
        self.assertIsNone(row["invoice_no"])


if __name__ == "__main__":
    unittest.main()
