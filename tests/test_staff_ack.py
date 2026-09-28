"""Acknowledgements: what was understood, in the cashier's language, fact-checked."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_ack as ack

LANGS = ("bm", "tamil", "english", "indonesian", "bengali", "bm_tamil")
ORDER = {"is_answer": True, "clear": True, "status": "order", "summary_en": "x",
         "items": [{"item": "ayam", "qty": 12}, {"item": "bawang", "qty": 5, "unit": "kg"}]}


class UnderstoodTests(unittest.TestCase):
    def test_items_as_the_cashier_wrote_them(self):
        self.assertEqual(ack.items_text(ORDER["items"]), "12 ayam, 5 kg bawang")
        self.assertEqual(ack.items_text([{"item": "ikan", "qty": 2.5, "unit": "kilo"}]), "2.5 kg ikan")
        self.assertEqual(ack.items_text([{"item": "", "qty": 5}, {"item": "x", "qty": "abc"}]), "")
        many = [{"item": f"item{i}", "qty": i + 1} for i in range(8)]
        self.assertTrue(ack.items_text(many).endswith("(+2)"))

    def test_one_line_per_language(self):
        for lang in LANGS:
            text = ack.acknowledgement(ORDER, lang, slot="order")
            self.assertEqual(text, "✅ Noted: 12 ayam, 5 kg bawang", lang)
            self.assertEqual(text.count("\n"), 0)

    def test_status_phrases_and_slot_specific_ok(self):
        self.assertEqual(ack.acknowledgement({"status": "ok"}, "english", slot="wastage"),
                         "✅ Noted: no wastage yesterday")
        self.assertEqual(ack.acknowledgement({"status": "ok"}, "bm", slot="wastage"),
                         "✅ Noted: tiada buangan semalam")
        self.assertEqual(ack.acknowledgement({"status": "ok"}, "english", slot="night"),
                         "✅ Noted: all OK")
        self.assertEqual(ack.acknowledgement({"status": "finished"}, "tamil", slot="lunch"),
                         "✅ Noted: ஏதோ தீர்ந்துடுச்சு")
        self.assertEqual(ack.acknowledgement({"status": "handed_in"}, "bengali", slot="bills"),
                         "✅ Noted: bill boss ke deoa hoyeche")
        self.assertEqual(ack.acknowledgement({"status": "weird"}, "indonesian"),
                         "✅ Noted: jawaban kamu")
        self.assertEqual(ack.acknowledgement(None, "english"), "✅ Noted: your reply")
        # BM+Tamil readers get both, still on one line.
        both = ack.acknowledgement({"status": "problem"}, "bm_tamil", slot="open")
        self.assertEqual(both, "✅ Noted: ada masalah — பிரச்சனை இருக்கு")
        self.assertEqual(both.count("\n"), 0)

    def test_every_status_has_every_language(self):
        for status, table in ack._STATUS.items():
            for lang in ("bm", "tamil", "english", "indonesian", "bengali"):
                self.assertTrue(table.get(lang), (status, lang))
        for slot, table in ack._OK_BY_SLOT.items():
            for lang in ("bm", "tamil", "english", "indonesian", "bengali"):
                self.assertTrue(table.get(lang), (slot, lang))


class VoiceTests(unittest.TestCase):
    def test_transcript_on_second_line_so_it_can_be_corrected(self):
        text = ack.acknowledgement(ORDER, "tamil", slot="order",
                                   transcript="naalaikku ayam 12 bawang 5 kilo")
        lines = text.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "✅ Noted: 12 ayam, 5 kg bawang")
        self.assertEqual(lines[1], "🎤 நீங்க சொன்னது: “naalaikku ayam 12 bawang 5 kilo”")
        long = ack.acknowledgement({"status": "ok"}, "english", transcript="x " * 200)
        self.assertLessEqual(len(long.split("\n")[1]), len("🎤 You said: “”") + ack.MAX_TRANSCRIPT)
        self.assertTrue(long.split("\n")[1].endswith("…”"))
        # Never more than two lines, even for BM+Tamil.
        both = ack.acknowledgement(ORDER, "bm_tamil", slot="order", transcript="ayam 12 bawang 5kg")
        self.assertEqual(both.count("\n"), 1)


class FactCheckTests(unittest.TestCase):
    def test_numbers_and_items_come_only_from_the_parsed_items(self):
        vocab = {"ayam", "bawang", "sotong"}
        self.assertEqual(ack.acknowledgement(ORDER, "bm", slot="order", vocabulary=vocab),
                         "✅ Noted: 12 ayam, 5 kg bawang")
        # A phrase that smuggles a number or an item the reader never gave
        # falls back to the bare status phrase.
        import staff_chat
        problems = staff_chat.fact_check("✅ Noted: 12 ayam, 9 kg sotong",
                                         {"items": [{"item": "ayam", "qty": 12.0}], "transcript": ""},
                                         vocabulary=vocab, language="bm", slot=None)
        self.assertTrue(problems)
        bad = dict(ORDER, items=[{"item": "ayam", "qty": 12}])
        with unittest.mock.patch.object(ack, "understood", return_value="12 ayam, 9 kg sotong"):
            self.assertEqual(ack.acknowledgement(bad, "bm", slot="order", vocabulary=vocab),
                             "✅ Noted: order esok")

    def test_money_never_appears(self):
        with unittest.mock.patch.object(ack, "understood", return_value="RM40 ayam"):
            self.assertEqual(ack.acknowledgement({"status": "ok"}, "english"), "✅ Noted: all OK")


class UnmatchedTests(unittest.TestCase):
    def test_says_so_and_names_the_open_questions(self):
        threads = [{"slot": "order"}, {"slot": "bills"}, {"slot": "order"}]
        text = ack.unmatched("bm", threads)
        self.assertIn("tak pasti soalan mana", text)
        self.assertTrue(text.endswith("order esok, bil supplier"))
        self.assertEqual(text.count("\n"), 0)
        en = ack.unmatched("english", [{"slot": "lunch"}])
        self.assertIn("Which one is it for?", en)
        self.assertTrue(en.endswith("lunch crowd"))
        self.assertTrue(ack.unmatched("tamil", [{"slot": "night"}]).endswith("ராத்திரி"))
        self.assertTrue(ack.unmatched("bm_tamil", []).endswith("—"))
        for lang in LANGS:
            self.assertLessEqual(ack.unmatched(lang, threads).count("\n"), 1, lang)


class NothingOpenTests(unittest.TestCase):
    def test_one_line_in_every_language(self):
        self.assertEqual(ack.nothing_open("english"), "✅ Noted — nothing is being asked right now.")
        self.assertEqual(ack.nothing_open("bm"), "✅ Noted — tak ada soalan sekarang.")
        self.assertEqual(ack.nothing_open("bm_tamil"), "✅ Noted — tak ada soalan sekarang.")
        for lang in LANGS:
            text = ack.nothing_open(lang)
            self.assertTrue(text.startswith("✅ Noted"), lang)
            self.assertEqual(text.count("\n"), 0, lang)

    def test_voice_note_keeps_its_transcript(self):
        text = ack.nothing_open("tamil", transcript="காடிசியா மேவாட்டார் எப்பாவுக்குனான்")
        lines = text.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "✅ Noted — இப்போ எந்த கேள்வியும் இல்ல.")
        self.assertEqual(lines[1], "🎤 நீங்க சொன்னது: “காடிசியா மேவாட்டார் எப்பாவுக்குனான்”")
        self.assertTrue(ack.nothing_open("english", transcript="x " * 200).split("\n")[1].endswith("…”"))


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
