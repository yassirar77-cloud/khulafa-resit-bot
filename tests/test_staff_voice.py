"""Voice replies: acceptance rules, type-instead text, same parse as typed."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_ai
import staff_live as sl
import staff_voice as sv


def _complete(data):
    return lambda system, user: {"data": data}


class AcceptTests(unittest.TestCase):
    def test_no_provider_means_type_instead(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(staff_ai.transcribe(b"ogg"))
            self.assertIsNone(sv.transcribe(b"ogg", "tamil"))
        with mock.patch.dict("os.environ", {"STAFF_VOICE_AI": "whisper"}, clear=True):
            self.assertIsNone(staff_ai.transcribe(b"ogg"))     # not wired yet
        self.assertIsNone(sv.accept(None))
        self.assertIsNone(sv.accept({"text": "   "}))

    def test_confidence_floor(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(sv.accept({"text": "ayam habis", "confidence": 0.9}), "ayam habis")
            self.assertIsNone(sv.accept({"text": "ayam habis", "confidence": 0.4}))
            self.assertEqual(sv.accept({"text": "ayam habis"}), "ayam habis")   # no score given
        with mock.patch.dict("os.environ", {"VOICE_MIN_CONFIDENCE": "0.3"}):
            self.assertEqual(sv.accept({"text": "x", "confidence": 0.4}), "x")
        self.assertTrue(sv.too_long(200))
        self.assertFalse(sv.too_long(30))

    def test_type_instead_in_every_language(self):
        for lang in ("bm", "tamil", "english", "indonesian", "bengali"):
            self.assertIn("🙏", sv.type_instead_text(lang), lang)
        self.assertEqual(sv.type_instead_text("bm_tamil").count("\n"), 1)
        self.assertEqual(sv.type_instead_text("unknown"), sv.type_instead_text("bm"))

    def test_language_hints(self):
        self.assertEqual(sv.LANGUAGE_HINTS["bm_tamil"], ("ms", "ta"))


class SameAsTypedTests(unittest.TestCase):
    def test_mocked_transcript_parses_like_the_typed_reply(self):
        typed = "esok ayam 40kg ikan 10kg"
        transcript = {"text": "esok ayam 40kg ikan 10kg", "confidence": 0.93,
                      "provider": "mock", "model": "mock-1"}
        spoken = sv.accept(transcript)
        seen = []

        def complete(system, user):
            seen.append(user)
            return {"data": {"is_answer": True, "clear": True, "summary_en": "Order",
                             "status": "order",
                             "items": [{"item": "ayam", "qty": 40, "unit": "kg"},
                                       {"item": "ikan", "qty": 10, "unit": "kg"}]}}
        a = sl.parse_reply("What to order?", "Esok nak order apa?", typed, complete)
        b = sl.parse_reply("What to order?", "Esok nak order apa?", spoken, complete)
        self.assertEqual(a, b)
        self.assertEqual(seen[0], seen[1])          # the reader saw the same words

    def test_log_row(self):
        thread = {"id": 4, "slot": "order", "cashier": "Kalai"}
        row = sv.log_row(thread, outlet_code="SEK7", chat_id=-7, language="tamil",
                         file_id="AwACAg", transcript={"text": "ayam habis", "confidence": 0.8,
                                                       "provider": "mock", "model": "m"},
                         text="ayam habis", accepted=True)
        self.assertEqual((row["kind"], row["slot"], row["voice_file_id"], row["transcript"]),
                         ("voice", "order", "AwACAg", "ayam habis"))
        self.assertEqual((row["source"], row["provider"], row["facts"]["thread_id"]),
                         ("voice", "mock", 4))
        row = sv.log_row(None, outlet_code="SEK7", chat_id=-7, language="bm", file_id="x",
                         transcript=None, text="", accepted=False)
        self.assertEqual((row["slot"], row["source"], row["problems"]),
                         ("voice", "type_instead", ["not transcribed"]))


class HealthTests(unittest.TestCase):
    def test_status_counts_calls_and_tokens(self):
        staff_ai.reset_status()
        fake = mock.MagicMock()
        fake.chat.completions.create.return_value = mock.MagicMock(
            choices=[mock.MagicMock(message=mock.MagicMock(content='{"text": "Hi"}'))],
            usage=mock.MagicMock(prompt_tokens=100, completion_tokens=20),
        )
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_deepseek_client", return_value=fake):
            staff_ai.complete_json("s", "u")
            staff_ai.complete_json("s", "u")
            st = staff_ai.status()
        self.assertEqual((st["calls_today"], st["tokens_today"], st["tokens_in_today"]),
                         (2, 240, 200))
        self.assertTrue(st["last_ok_at"])
        self.assertEqual(st["provider"], "deepseek")
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_deepseek_client", return_value=fake):
            fake.chat.completions.create.return_value = mock.MagicMock(
                choices=[mock.MagicMock(message=mock.MagicMock(content="no json"))])
            staff_ai.complete_json("s", "u")
            st = staff_ai.status()
        self.assertEqual((st["calls_today"], st["failures_today"]), (2, 1))
        staff_ai.reset_status()
        self.assertIsNone(staff_ai.status()["last_ok_at"])


if __name__ == "__main__":
    unittest.main()
