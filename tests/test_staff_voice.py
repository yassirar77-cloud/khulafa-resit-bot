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
    def test_no_key_means_type_instead_with_reason(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(staff_ai.voice_provider(), "groq")
            res = staff_ai.transcribe(b"ogg", language="ta")
            self.assertEqual((res["ok"], res["reason"], res["language"]), (False, "no_key", "ta"))
            self.assertIsNone(sv.accept(res))
            self.assertEqual(sv.bounce(res), {"reason": "no_key", "detail": "", "level": "info"})
            self.assertIsNone(sv.accept(sv.transcribe(b"ogg", "tamil")))
        with mock.patch.dict("os.environ", {"STAFF_VOICE_AI": "whisper", "GROQ_API_KEY": "k"},
                             clear=True):
            res = staff_ai.transcribe(b"ogg", language="ta")
            self.assertEqual(res["reason"], "unknown_provider")
        self.assertIsNone(sv.accept(None))
        self.assertEqual(sv.bounce(None)["reason"], "no_transcript")
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
                if lang is None:
                    continue          # no /lang set: auto-detect, tested separately
                sv.transcribe(b"x", lang)
                self.assertEqual(client.audio.transcriptions.create.call_args.kwargs["language"],
                                 code, lang)

    def test_no_lang_set_means_auto_detect_for_that_call_only(self):
        client = _groq()
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=client), \
                self.assertLogs("staff_ai", level="INFO") as logs:
            out = sv.transcribe(b"OggS...", None)
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        self.assertNotIn("language", kwargs)                 # no parameter at all
        self.assertEqual(kwargs["model"], "whisper-large-v3-turbo")
        self.assertEqual(out["language"], "auto")
        self.assertEqual(out["text"], "esok ayam 40kg ikan 10kg")
        self.assertTrue(any("lang=auto" in line for line in logs.output))
        # The /lang path is unchanged: a set language is always sent.
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=client):
            sv.transcribe(b"x", "tamil")
        self.assertEqual(client.audio.transcriptions.create.call_args.kwargs["language"], "ta")
        # Failures during an auto call still say auto.
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(sv.transcribe(b"x", None)["language"], "auto")
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=_groq(error=RuntimeError("x"))):
            self.assertEqual(sv.transcribe(b"x", None)["language"], "auto")

    def test_api_error_carries_status_and_message_and_logs_warning(self):
        class ApiError(RuntimeError):
            status_code = 429
            message = "Rate limit reached for whisper-large-v3-turbo"
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=_groq(error=ApiError("x"))), \
                self.assertLogs("staff_ai", level="WARNING") as logs:
            res = sv.transcribe(b"x", "bm")
        self.assertEqual((res["ok"], res["reason"], res["status"]), (False, "api_error", 429))
        self.assertIn("Rate limit", res["message"])
        self.assertIsNone(sv.accept(res))
        why = sv.bounce(res)
        self.assertEqual((why["reason"], why["level"]), ("api_error", "warning"))
        self.assertIn("status=429", why["detail"])
        self.assertIn("Rate limit", why["detail"])
        self.assertTrue(any("api_error" in line and "status=429" in line for line in logs.output))
        # An error without a status code still says api_error.
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client",
                                  return_value=_groq(error=RuntimeError("connection reset"))):
            res = sv.transcribe(b"x", "bm")
        self.assertEqual((res["reason"], res["status"]), ("api_error", None))
        self.assertIn("status=? connection reset", sv.bounce(res)["detail"])

    def test_empty_text_and_no_audio_reasons(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True):
            with mock.patch.object(staff_ai, "_groq_client",
                                   return_value=_groq(_groq_response(text="  "))):
                res = sv.transcribe(b"x", "bm")
            self.assertEqual(res["reason"], "empty_text")
            self.assertEqual(sv.bounce(res), {"reason": "empty_text", "detail": "", "level": "info"})
            with mock.patch.object(staff_ai, "_groq_client", return_value=_groq()):
                self.assertEqual(staff_ai.transcribe(b"", language="ms")["reason"], "no_audio")

    def test_confidence_from_segments_and_the_floor(self):
        low = _groq_response(segments=[
            {"start": 0, "end": 3, "avg_logprob": -1.4, "no_speech_prob": 0.6}])
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_groq_client", return_value=_groq(low)):
            out = sv.transcribe(b"x", "bm")
        self.assertLess(out["confidence"], 0.2)
        self.assertIsNone(sv.accept(out))                 # below VOICE_MIN_CONFIDENCE
        why = sv.bounce(out)
        self.assertEqual((why["reason"], why["level"]), ("low_confidence", "info"))
        self.assertIn(f"score={out['confidence']} floor=0.6", why["detail"])
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
                         ("voice", "type_instead", ["no_transcript"]))
        self.assertEqual((row["facts"]["stt_language"], row["facts"]["duration_s"]), ("ms", 7))
        self.assertEqual(row["facts"]["reason"], "no_transcript")
        # A bounce keeps its reason and detail for /voice_stats and the log.
        api = {"ok": False, "reason": "api_error", "status": 401, "message": "Invalid API Key",
               "language": "ta"}
        row = sv.log_row(None, outlet_code="SEK7", chat_id=-7, language="tamil", file_id="x",
                         transcript=api, text="", accepted=False, duration=3)
        self.assertEqual(row["problems"], ["api_error"])
        self.assertEqual(row["facts"]["reason"], "api_error")
        self.assertIn("status=401 Invalid API Key", row["facts"]["reason_detail"])
        too_long = sv.failed("too_long", "125s", "bm")
        self.assertEqual(sv.bounce(too_long), {"reason": "too_long", "detail": "125s", "level": "info"})
        self.assertEqual(sv.bounce(sv.failed("download_error", "timeout", "bm"))["level"], "warning")


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


class StatsTests(unittest.TestCase):
    @staticmethod
    def _row(_self, code, source, reason=None, detail=None):
        return {"outlet_code": code, "source": source,
                "facts": {"reason": reason, "reason_detail": detail} if reason else {},
                "problems": [] if source == "voice" else [reason or "not transcribed"]}

    ROWS = [
        _row(None, "SEK7", "voice"), _row(None, "SEK7", "voice"),
        _row(None, "SEK7", "type_instead", "low_confidence", "score=0.41 floor=0.6"),
        _row(None, "SEK20", "type_instead", "api_error", "status=401 Invalid API Key"),
        _row(None, "SEK20", "type_instead", "api_error", "status=401 Invalid API Key"),
        _row(None, "SEK20", "type_instead", "no_key"),
        _row(None, "BISTRO7", "voice"),
        {"outlet_code": "BISTRO7", "source": "type_instead", "facts": {}, "problems": ["not transcribed"]},
    ]

    def test_counts_per_outlet_and_reasons(self):
        per = sv.stats(self.ROWS)
        self.assertEqual(per["SEK7"], {"transcribed": 2, "bounced": 1, "reasons": {"low_confidence": 1}})
        self.assertEqual(per["SEK20"], {"transcribed": 0, "bounced": 3,
                                        "reasons": {"api_error": 2, "no_key": 1}})
        self.assertEqual(per["BISTRO7"]["reasons"], {"unknown": 1})   # a pre-reason row

    def test_format(self):
        from datetime import date
        text = sv.format_stats(self.ROWS, lambda c: c.title(), since=date(2026, 9, 28))
        self.assertIn("🎤 Voice notes this week (since 2026-09-28)", text)
        self.assertIn("3 transcribed · 5 bounced", text)
        self.assertIn("• Sek7: 2 transcribed, 1 bounced — low_confidence ×1", text)
        self.assertIn("• Sek20: 0 transcribed, 3 bounced — api_error ×2, no_key ×1", text)
        self.assertIn("Bounce reasons: api_error ×2, low_confidence ×1, no_key ×1, unknown ×1", text)
        self.assertIn("api_error = Groq refused or failed", text)
        self.assertIn("no_key = GROQ_API_KEY", text)
        self.assertIn("low_confidence = below VOICE_MIN_CONFIDENCE (0.6)", text)
        # Busiest outlet first.
        self.assertLess(text.index("• Sek7"), text.index("• Bistro7"))
        self.assertIn("No voice notes yet.", sv.format_stats([]))
