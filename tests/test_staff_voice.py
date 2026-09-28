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


def _groq_response(text="esok ayam 40kg ikan 10kg", segments=None, duration=4.2):
    """A verbose_json transcription as the openai client returns it."""
    resp = mock.MagicMock()
    resp.text = text
    resp.duration = duration
    resp.segments = segments if segments is not None else [
        {"start": 0.0, "end": 2.0, "avg_logprob": -0.10, "no_speech_prob": 0.02},
        {"start": 2.0, "end": 4.2, "avg_logprob": -0.20, "no_speech_prob": 0.05},
    ]
    return resp


def _groq(resp=None, error=None):
    client = mock.MagicMock()
    if error is not None:
        client.audio.transcriptions.create.side_effect = error
    else:
        client.audio.transcriptions.create.return_value = resp or _groq_response()
    return client


class GroqTranscribeTests(unittest.TestCase):
    def test_no_key_means_type_instead(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(staff_ai.voice_provider(), "groq")
            self.assertIsNone(staff_ai.transcribe(b"ogg", language="ta"))
            self.assertIsNone(sv.transcribe(b"ogg", "tamil"))
        with mock.patch.dict("os.environ", {"STAFF_VOICE_AI": "whisper", "GROQ_API_KEY": "k"},
                             clear=True):
            self.assertIsNone(staff_ai.transcribe(b"ogg", language="ta"))   # unknown provider
        self.assertIsNone(sv.accept(None))
        self.assertIsNone(sv.accept({"text": "   "}))

    def test_call_shape_model_and_language_never_auto(self):
        client = _groq()
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=client):
            out = sv.transcribe(b"OggS...", "tamil")
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "whisper-large-v3-turbo")
        self.assertEqual(kwargs["language"], "ta")
        self.assertEqual(kwargs["response_format"], "verbose_json")
        self.assertEqual(kwargs["file"], ("voice.ogg", b"OggS...", "audio/ogg"))
        self.assertEqual(out["text"], "esok ayam 40kg ikan 10kg")
        self.assertEqual((out["provider"], out["model"], out["language"], out["duration"]),
                         ("groq", "whisper-large-v3-turbo", "ta", 4.2))
        self.assertGreater(out["confidence"], 0.8)
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k", "GROQ_STT_MODEL": "whisper-large-v3"},
                             clear=True), mock.patch.object(staff_ai, "_groq_client", return_value=client):
            sv.transcribe(b"x", "bm")
        self.assertEqual(client.audio.transcriptions.create.call_args.kwargs["model"], "whisper-large-v3")

    def test_every_lang_setting_maps_to_a_code(self):
        codes = {"tamil": "ta", "bm": "ms", "bengali": "bn", "english": "en", "indonesian": "id",
                 "bm_tamil": "ms", "unknown": "ms", None: "ms"}
        for lang, code in codes.items():
            self.assertEqual(sv.language_code(lang), code, lang)
        client = _groq()
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=client):
            for lang, code in codes.items():
                sv.transcribe(b"x", lang)
                self.assertEqual(client.audio.transcriptions.create.call_args.kwargs["language"],
                                 code, lang)

    def test_api_error_or_empty_text_means_type_instead(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True):
            with mock.patch.object(staff_ai, "_groq_client",
                                   return_value=_groq(error=RuntimeError("429 rate limit"))):
                self.assertIsNone(sv.transcribe(b"x", "bm"))
            with mock.patch.object(staff_ai, "_groq_client",
                                   return_value=_groq(_groq_response(text="  "))):
                self.assertIsNone(sv.transcribe(b"x", "bm"))
            with mock.patch.object(staff_ai, "_groq_client", return_value=_groq()):
                self.assertIsNone(staff_ai.transcribe(b"", language="ms"))

    def test_confidence_from_segments_and_the_floor(self):
        low = _groq_response(segments=[
            {"start": 0, "end": 3, "avg_logprob": -1.4, "no_speech_prob": 0.6}])
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=_groq(low)):
            out = sv.transcribe(b"x", "bm")
        self.assertLess(out["confidence"], 0.2)
        self.assertIsNone(sv.accept(out))                 # below VOICE_MIN_CONFIDENCE
        none = _groq_response(segments=[])
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=_groq(none)):
            out = sv.transcribe(b"x", "bm")
        self.assertIsNone(out["confidence"])
        self.assertEqual(sv.accept(out), "esok ayam 40kg ikan 10kg")   # no score: accepted


class AcceptTests(unittest.TestCase):
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

    def test_log_row_keeps_transcript_language_and_duration(self):
        thread = {"id": 4, "slot": "order", "cashier": "Kalai"}
        row = sv.log_row(thread, outlet_code="SEK7", chat_id=-7, language="tamil",
                         file_id="AwACAg", transcript={"text": "ayam habis", "confidence": 0.8,
                                                       "provider": "groq", "model": "m",
                                                       "language": "ta", "duration": 3.4},
                         text="ayam habis", accepted=True, duration=3)
        self.assertEqual((row["kind"], row["slot"], row["voice_file_id"], row["transcript"]),
                         ("voice", "order", "AwACAg", "ayam habis"))
        self.assertEqual((row["source"], row["provider"], row["facts"]["thread_id"]),
                         ("voice", "groq", 4))
        self.assertEqual((row["facts"]["stt_language"], row["facts"]["duration_s"]), ("ta", 3.4))
        row = sv.log_row(None, outlet_code="SEK7", chat_id=-7, language="bm_tamil", file_id="x",
                         transcript=None, text="", accepted=False, duration=7)
        self.assertEqual((row["slot"], row["source"], row["problems"]),
                         ("voice", "type_instead", ["not transcribed"]))
        self.assertEqual((row["facts"]["stt_language"], row["facts"]["duration_s"]), ("ms", 7))


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
