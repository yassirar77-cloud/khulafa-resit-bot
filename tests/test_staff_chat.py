"""Natural staff chat: templates, facts from data, the fact check, preview."""

import json
import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_chat as sc

DRAFT = [
    {"item": "roti", "qty": 6, "pack": "kotak", "supplier": "DIAMOND BALL"},
    {"item": "sotong", "qty": 19, "pack": "kg", "supplier": "FOOK LEONG SEA PRODUCTS SDN BHD"},
    {"item": "ayam", "qty": 75, "pack": "kg", "supplier": "BESTARI FARM (M) SDN BHD"},
    {"item": "kelapa", "qty": 50, "pack": "biji", "supplier": "BESTARI KHAFA"},
]
FULL_DRAFT = DRAFT + [
    {"item": "garam", "qty": 2, "pack": "kg", "supplier": "BESTARI KHAFA"},
    {"item": "gula", "qty": 10, "pack": "kg", "supplier": "BESTARI KHAFA"},
]
FORECAST = [
    {"item_code": "ikan_kari", "unit": "pcs", "recommend_qty": 11.5,
     "usual_cooked": 15.5, "action": "CUT"},
    {"item_code": "ayam_kicap", "unit": "pcs", "recommend_qty": 9.72,
     "usual_cooked": 13.5, "action": "CUT"},
    {"item_code": "kambing", "unit": "kg", "recommend_qty": 3.1,
     "usual_cooked": 4.0, "action": "HOLD"},
]
VOCAB = {"ayam", "ikan", "sotong", "kambing", "roti", "kelapa", "ayam kicap", "telur"}


def _ai(text, english="EN"):
    return lambda system, user: {
        "data": {"text": text, "english": english},
        "provider": "deepseek", "model": "deepseek-flash",
        "tokens_in": 500, "tokens_out": 60,
    }


class SettingsTests(unittest.TestCase):
    def test_style_defaults_to_classic(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(sc.style(), sc.CLASSIC)
        with mock.patch.dict("os.environ", {"STAFF_CHAT_STYLE": "Preview"}):
            self.assertEqual(sc.style(), sc.PREVIEW)
        # "natural" isn't live yet: anything unknown stays classic.
        with mock.patch.dict("os.environ", {"STAFF_CHAT_STYLE": "natural"}):
            self.assertEqual(sc.style(), sc.CLASSIC)

    def test_language_aliases(self):
        self.assertEqual(sc.normalize_language("Bangla"), "bengali")
        self.assertEqual(sc.normalize_language("malay"), "bm")
        self.assertEqual(sc.normalize_language("indo"), "indonesian")
        self.assertIsNone(sc.normalize_language("hindi"))


class FactsTests(unittest.TestCase):
    def test_stock_picks_key_item_first(self):
        self.assertEqual(sc.stock_facts(DRAFT), {
            "item": "Ayam", "qty": "75", "pack": "kg", "supplier": "Bestari Farm",
        })

    def test_order_lists_top_three_and_counts_rest(self):
        facts = sc.order_facts(FULL_DRAFT)
        self.assertEqual([i["item"] for i in facts["items"]], ["Ayam", "Sotong", "Kelapa"])
        self.assertEqual(facts["more"], 3)
        self.assertNotIn("partial", facts)

    def test_short_draft_lists_every_line_as_partial(self):
        facts = sc.order_facts(DRAFT)            # 4 lines < 5
        self.assertTrue(facts["partial"])
        self.assertEqual([i["item"] for i in facts["items"]],
                         ["Ayam", "Sotong", "Kelapa", "Roti"])
        self.assertEqual(facts["more"], 0)

    def test_cook_picks_biggest_change_and_rounds(self):
        facts = sc.cook_facts(FORECAST)
        self.assertEqual(facts, {"item": "Ayam Kicap", "cook": "10", "unit": "pcs",
                                 "usual": "14", "action": "CUT"})

    def test_cook_skips_hold_and_equal_after_rounding(self):
        self.assertIsNone(sc.cook_facts([FORECAST[2]]))
        same = [{"item_code": "daging", "unit": "kg", "recommend_qty": 3.1,
                 "usual_cooked": 3.2, "action": "CUT"}]
        self.assertIsNone(sc.cook_facts(same))

    def test_bills_most_overdue(self):
        entries = [
            {"supplier": "JAYA GROCER", "last_date": date(2026, 9, 10),
             "days_missing": 14, "days_overdue": 3},
            {"supplier": "FOOK LEONG SEA PRODUCTS SDN BHD", "last_date": date(2026, 9, 5),
             "days_missing": 19, "days_overdue": 9},
        ]
        self.assertEqual(sc.bills_facts(entries),
                         {"supplier": "Fook Leong Sea Products", "days": "19", "last": "05/09"})

    def test_no_data_is_none(self):
        for fn in (sc.stock_facts, sc.order_facts, sc.cook_facts, sc.bills_facts):
            self.assertIsNone(fn([]))


class TemplateTests(unittest.TestCase):
    def test_every_language_and_slot_renders(self):
        facts = {
            "stock": sc.stock_facts(DRAFT), "order": sc.order_facts(DRAFT),
            "cook": sc.cook_facts(FORECAST),
            "bills": {"supplier": "Fook Leong", "days": "19", "last": "05/09"},
        }
        for lang in sc.LANGUAGES:
            for slot in sc.SLOTS:
                for variant in (0, 1):
                    text = sc.render_template(slot, lang, facts.get(slot, {}), variant)
                    self.assertTrue(text, (lang, slot))
                    self.assertNotIn("{", text, (lang, slot))
                    # Every plain template passes its own fact check.
                    self.assertEqual(
                        sc.fact_check(text, facts.get(slot, {}), vocabulary=VOCAB,
                                      language=lang, slot=slot), [],
                        (lang, slot, variant, text),
                    )

    def test_bm_tamil_is_two_lines(self):
        text = sc.render_template("open", sc.BM_TAMIL, {})
        self.assertEqual(len(text.split("\n")), 2)

    def test_order_template_content(self):
        text = sc.render_template("order", "bm", sc.order_facts(FULL_DRAFT))
        self.assertEqual(text, "Order esok: Ayam 75kg, Sotong 19kg, Kelapa 50 biji (+3 lagi). Ok atau nak tukar?")

    def test_partial_draft_says_main_items_not_full_order(self):
        facts = sc.order_facts(DRAFT)
        bm = sc.render_template("order", "bm", facts)
        self.assertEqual(bm, "Barang utama untuk esok: Ayam 75kg, Sotong 19kg, Kelapa 50 biji, "
                             "Roti 6 kotak. Ada lagi nak order? Bagitau ya.")
        self.assertIn("Anything else you need", sc.render_template("order", "english", facts))
        for lang in ("bm", "tamil", "english", "indonesian", "bengali", sc.BM_TAMIL):
            for variant in (0, 1):
                text = sc.render_template("order", lang, facts, variant)
                self.assertIn("Ayam 75kg", text, lang)
                self.assertEqual(sc.fact_check(text, facts, vocabulary=VOCAB, language=lang,
                                               slot="order"), [], (lang, text))
        self.assertIn("NOT the full order", sc._purpose("order", facts))


class FactCheckTests(unittest.TestCase):
    facts = {"item": "Ayam", "qty": "75", "pack": "kg", "supplier": "Bestari Farm"}

    def check(self, text, **kw):
        return sc.fact_check(text, self.facts, vocabulary=VOCAB, **kw)

    def test_clean_wording_passes(self):
        self.assertEqual(self.check("Ayam cukup ke sampai malam? Pagi ni Bestari Farm 75kg."), [])

    def test_invented_number_rejected(self):
        self.assertIn("number 80 not in data", self.check("Ayam 80kg cukup?"))

    def test_invented_item_rejected(self):
        self.assertIn("item 'sotong' not in data", self.check("Ayam dan sotong cukup?"))

    def test_money_rejected(self):
        self.assertIn("money figure", self.check("Ayam RM 300 cukup?"))
        self.assertIn("money figure", self.check("Ayam naik 10%?"))

    def test_other_script_digits_rejected(self):
        self.assertIn("non-0-9 digits", self.check("আজ ৭৫ kg ayam"))

    def test_other_cashier_name_rejected(self):
        self.assertTrue(self.check("Saddam, ayam cukup?", other_names=["Saddam"]))

    def test_labels_and_length_rejected(self):
        self.assertIn("system label", self.check("ALERT: ayam cukup?"))
        self.assertIn("too long", self.check("\n".join(["Ayam?"] * 5)))

    def test_item_word_inside_fact_label_allowed(self):
        facts = {"item": "Ayam Kicap", "cook": "10", "unit": "pcs", "usual": "14"}
        self.assertEqual(sc.fact_check("Ayam hari ni 10 cukup, biasa 14?", facts, vocabulary=VOCAB), [])


class BuildMessageTests(unittest.TestCase):
    facts = {"item": "Ayam", "qty": "75", "pack": "kg", "supplier": "Bestari Farm"}

    def test_ai_wording_used_when_it_passes(self):
        res = sc.build_message("stock", "bm", self.facts, vocabulary=VOCAB,
                               complete=_ai("Ayam 75kg Bestari Farm tu cukup sampai malam?"))
        self.assertEqual(res["source"], "ai")
        self.assertEqual(res["text"], "Ayam 75kg Bestari Farm tu cukup sampai malam?")
        self.assertEqual(res["english"], "EN")
        self.assertEqual(res["tokens_in"], 500)

    def test_template_used_when_fact_check_fails(self):
        res = sc.build_message("stock", "bm", self.facts, vocabulary=VOCAB,
                               complete=_ai("Ayam 90kg cukup?"))
        self.assertEqual(res["source"], "template")
        self.assertEqual(res["text"], res["template"])
        self.assertEqual(res["ai_text"], "Ayam 90kg cukup?")
        self.assertIn("number 90 not in data", res["problems"])
        self.assertEqual(res["english"], "")

    def test_template_used_when_ai_unavailable(self):
        res = sc.build_message("open", "tamil", {}, complete=lambda s, u: None)
        self.assertEqual(res["source"], "template")
        self.assertEqual(res["problems"], ["ai unavailable"])
        self.assertTrue(res["text"])

    def test_provider_exception_falls_back(self):
        def boom(s, u):
            raise RuntimeError("x")
        res = sc.build_message("night", "bm", {}, complete=boom)
        self.assertEqual(res["source"], "template")

    def test_prompt_carries_facts_language_and_reference(self):
        seen = {}

        def capture(system, user):
            seen["system"], seen["user"] = system, json.loads(user)
            return None
        sc.build_message("stock", "bengali", self.facts, seed="s1", complete=capture)
        self.assertIn("ONLY the facts", seen["system"])
        self.assertEqual(seen["user"]["facts"], self.facts)
        self.assertIn("Bengali", seen["user"]["language"])
        self.assertEqual(seen["user"]["variation_seed"], "s1")
        self.assertTrue(seen["user"]["reference_message"])


class PreviewTests(unittest.TestCase):
    def test_digest_shows_each_outlet_and_fallback_reason(self):
        ok = sc.build_message("open", "bm", {}, complete=_ai("Kedai dah buka? Ada kurang apa-apa?"))
        bad = sc.build_message("open", "bm", {}, complete=_ai("Ayam 5kg?"))
        text = sc.format_preview("open", [
            {"outlet_code": "BISTRO7", "cashier": "Rahim", "language": "bm", "result": ok},
            {"outlet_code": "SEK20", "cashier": "Syed", "language": "bm", "result": bad},
            {"outlet_code": "KLANG", "cashier": "Vasiullah", "language": "tamil",
             "skip": "no order draft for today — nothing to ask"},
        ])
        self.assertIn("Not sent to any group", text)
        self.assertIn("BISTRO7 · Rahim · bm\nRahim,\nKedai dah buka?", text)
        self.assertIn("↳ EN: EN", text)
        self.assertIn("SEK20 · Syed · bm ✏️ number 5 not in data", text)
        self.assertIn("KLANG · Vasiullah · tamil — no order draft", text)

    def test_log_row_keeps_facts_and_both_texts(self):
        res = sc.build_message("stock", "bm", {"item": "Ayam", "qty": "75", "pack": "kg",
                                               "supplier": "Bestari Farm"},
                               vocabulary=VOCAB, complete=_ai("Ayam 99kg?"))
        row = sc.log_row("stock", "BISTRO7", -1, "Rahim", "bm", {"item": "Ayam"}, res, "preview")
        self.assertEqual(row["facts"], {"item": "Ayam"})
        self.assertEqual(row["ai_text"], "Ayam 99kg?")
        self.assertEqual(row["final_text"], res["template"])
        self.assertEqual(row["source"], "template")
        self.assertEqual(row["mode"], "preview")

    def test_data_codes_include_aliases(self):
        self.assertEqual(sc.data_codes("DAMANSARA"), ["DAMANSARA", "D"])
        self.assertEqual(sc.data_codes("SBESI"), ["SBESI", "KLRAZAK"])
        self.assertEqual(sc.data_codes("SEK20"), ["SEK20"])


class StaffAiTests(unittest.TestCase):
    def test_no_key_returns_none(self):
        import staff_ai
        with mock.patch.dict("os.environ", {"STAFF_CHAT_AI": "deepseek"}, clear=True):
            self.assertIsNone(staff_ai.complete_json("s", "u"))

    def test_unknown_provider_returns_none(self):
        import staff_ai
        with mock.patch.dict("os.environ", {"STAFF_CHAT_AI": "other", "DEEPSEEK_API_KEY": "k"}):
            self.assertIsNone(staff_ai.complete_json("s", "u"))

    def test_deepseek_call_shape_and_parse(self):
        import staff_ai
        fake = mock.MagicMock()
        fake.chat.completions.create.return_value = mock.MagicMock(
            choices=[mock.MagicMock(message=mock.MagicMock(content='{"text": "Hi"}'))],
            usage=mock.MagicMock(prompt_tokens=10, completion_tokens=3),
        )
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_deepseek_client", return_value=fake):
            out = staff_ai.complete_json("sys", "usr")
        kwargs = fake.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-flash")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(out["data"], {"text": "Hi"})
        self.assertEqual((out["tokens_in"], out["tokens_out"]), (10, 3))

    def test_bad_json_returns_none(self):
        import staff_ai
        fake = mock.MagicMock()
        fake.chat.completions.create.return_value = mock.MagicMock(
            choices=[mock.MagicMock(message=mock.MagicMock(content="not json"))]
        )
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_deepseek_client", return_value=fake):
            self.assertIsNone(staff_ai.complete_json("s", "u"))


class ScriptAndRetryTests(unittest.TestCase):
    def test_script_must_match_the_cashiers_language(self):
        tamil = "கடை ரெடியா?"
        self.assertEqual(sc.fact_check(tamil, {}, vocabulary=VOCAB, language="tamil"), [])
        self.assertEqual(sc.fact_check(tamil, {}, vocabulary=VOCAB, language="bm_tamil"), [])
        self.assertIn("Tamil script for a non-Tamil cashier",
                      sc.fact_check(tamil, {}, vocabulary=VOCAB, language="bm"))
        self.assertIn("Tamil script for a non-Tamil cashier",
                      sc.fact_check(tamil, {}, vocabulary=VOCAB, language="bengali"))
        bengali = "দোকান রেডি?"
        for lang in ("tamil", "bengali", "bm"):
            self.assertIn("Bengali script (Bengali goes in English letters)",
                          sc.fact_check(bengali, {}, vocabulary=VOCAB, language=lang))

    def test_items_suppliers_numbers_must_stay_as_in_the_data(self):
        facts = {"item": "Ayam", "qty": "75", "pack": "kg", "supplier": "Bestari Farm"}
        ok = "இன்னைக்கு draft-ல Bestari Farm Ayam 75 kg. இரவு வரைக்கும் போதுமா?"
        self.assertEqual(sc.fact_check(ok, facts, vocabulary=VOCAB,
                                       language="tamil", slot="stock"), [])
        # Supplier transliterated into Tamil script / item translated: rejected.
        bad = "இன்னைக்கு பெஸ்டாரி ஃபார்ம் கோழி 75 kg போதுமா?"
        problems = sc.fact_check(bad, facts, vocabulary=VOCAB, language="tamil", slot="stock")
        self.assertIn("'Bestari Farm' not written as in the data", problems)
        self.assertIn("'Ayam' not written as in the data", problems)

    def test_order_items_must_all_appear(self):
        facts = sc.order_facts(DRAFT)
        text = sc.render_template("order", "tamil", facts)
        self.assertEqual(sc.fact_check(text, facts, vocabulary=VOCAB,
                                       language="tamil", slot="order"), [])
        dropped = text.replace("Sotong 19kg, ", "")
        self.assertTrue(sc.fact_check(dropped, facts, vocabulary=VOCAB,
                                      language="tamil", slot="order"))

    def test_templates_keep_facts_latin_and_tamil_in_script(self):
        import re
        facts = sc.stock_facts(DRAFT)
        text = sc.render_template("stock", "tamil", facts)
        self.assertTrue(re.search("[\u0B80-\u0BFF]", text))
        for v in ("Ayam", "75", "Bestari Farm"):
            self.assertIn(v, text)
        for lang in ("bm", "english", "indonesian", "bengali"):
            text = sc.render_template("stock", lang, facts)
            self.assertIsNone(re.search("[\u0980-\u09FF\u0B80-\u0BFF]", text), lang)

    def test_wording_varies_by_day(self):
        seeds = [sc.seed_for("open", "BISTRO7", date(2026, 9, d)) for d in range(20, 28)]
        texts = {sc.render_template("open", "tamil", {}, sc.variant_for(x)) for x in seeds}
        self.assertEqual(len(texts), 2)

    def test_recent_messages_passed_to_ai_to_avoid(self):
        seen = {}

        def capture(system, user):
            seen["user"] = json.loads(user)
            return None
        sc.build_message("open", "tamil", {}, complete=capture,
                         avoid=["நேத்து text", "", "முந்தாநேத்து"])
        self.assertEqual(seen["user"]["recent_messages_do_not_repeat"],
                         ["நேத்து text", "முந்தாநேத்து"])
        self.assertIn("never repeat", sc.SYSTEM_PROMPT)
        self.assertIn("TAMIL SCRIPT", seen["user"]["language"])
        self.assertIn("English letters and 0-9 digits", seen["user"]["language"])

    def _fake(self, *contents):
        fake = mock.MagicMock()
        fake.chat.completions.create.side_effect = [
            mock.MagicMock(choices=[mock.MagicMock(
                message=mock.MagicMock(content=c), finish_reason="stop")],
                usage=mock.MagicMock(prompt_tokens=1, completion_tokens=1))
            for c in contents
        ]
        return fake

    def test_empty_reply_retried_once_then_succeeds(self):
        import staff_ai
        fake = self._fake("", '```json\n{"text": "Ok?"}\n```')
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_deepseek_client", return_value=fake):
            out = staff_ai.complete_json("s", "u")
        self.assertEqual(out["data"], {"text": "Ok?"})
        self.assertEqual(fake.chat.completions.create.call_count, 2)
        kwargs = fake.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertGreaterEqual(kwargs["max_tokens"], 800)

    def test_two_empty_replies_give_none(self):
        import staff_ai
        fake = self._fake("", "   ")
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=True), \
                mock.patch.object(staff_ai, "_deepseek_client", return_value=fake):
            self.assertIsNone(staff_ai.complete_json("s", "u"))
        self.assertEqual(fake.chat.completions.create.call_count, 2)

    def test_json_with_text_around_it_parsed(self):
        import staff_ai
        self.assertEqual(staff_ai._parse('Here: {"text": "a"} thanks'), {"text": "a"})
        self.assertIsNone(staff_ai._parse("no json"))


def _scripted(write_text, back="Is the shop ready?", same=True, reason="same"):
    """Fake provider: answers the writer, translator and judge prompts."""
    calls = []

    def complete(system, user):
        calls.append(system)
        if system == sc.TRANSLATE_PROMPT:
            return {"data": {"english": back}} if back is not None else None
        if system == sc.JUDGE_PROMPT:
            return {"data": {"same_meaning": same, "reason": reason}}
        return {"data": {"text": write_text, "english": "EN"},
                "provider": "deepseek", "model": "deepseek-flash"}
    complete.calls = calls
    return complete


class MeaningAndToneTests(unittest.TestCase):
    # The back-translation judge runs for check-ins with data; the order
    # ask (no draft) is one of them and needs no facts in the text.
    TAMIL_OK = "நாளைக்கு என்ன order பண்ணணும்? சாமானும் அளவும் சொல்லுங்க."
    ASK = {"ask": True}

    def test_tamil_passes_when_back_translation_matches(self):
        fake = _scripted(self.TAMIL_OK)
        res = sc.build_message("order", "tamil", self.ASK, complete=fake)
        self.assertEqual(res["source"], "ai")
        self.assertTrue(res["meaning_ok"])
        self.assertEqual(res["back_translation"], "Is the shop ready?")
        self.assertEqual(fake.calls, [sc.SYSTEM_PROMPT, sc.TRANSLATE_PROMPT, sc.JUDGE_PROMPT])

    def test_translator_never_sees_the_intended_message(self):
        seen = {}

        def complete(system, user):
            if system == sc.TRANSLATE_PROMPT:
                seen["user"] = user
                return {"data": {"english": "x"}}
            if system == sc.JUDGE_PROMPT:
                seen["judge"] = json.loads(user)
                return {"data": {"same_meaning": True}}
            return {"data": {"text": self.TAMIL_OK}}
        sc.build_message("order", "tamil", self.ASK, complete=complete)
        self.assertEqual(seen["user"], self.TAMIL_OK)   # Tamil only, no intent
        self.assertIn("order", seen["judge"]["intended"])
        self.assertEqual(seen["judge"]["actually_written"], "x")

    def test_meaning_mismatch_falls_back_to_template(self):
        fake = _scripted("இன்னைக்கு stock எவ்வளவு மீதம் இருக்கு சொல்லுங்க?",
                         back="How much stock is left over today?", same=False,
                         reason="asks about leftover stock, not tomorrow's order")
        res = sc.build_message("order", "tamil", self.ASK, complete=fake)
        self.assertEqual(res["source"], "template")
        self.assertEqual(res["problems"],
                         ["meaning check: asks about leftover stock, not tomorrow's order"])
        self.assertFalse(res["meaning_ok"])

    def test_meaning_check_fails_closed(self):
        res = sc.build_message("order", "tamil", self.ASK,
                               complete=_scripted(self.TAMIL_OK, back=None))
        self.assertEqual(res["source"], "template")
        self.assertIn("meaning check: no back-translation", res["problems"])

    def test_no_meaning_check_for_bm(self):
        fake = _scripted("Esok nak order apa? Bagitau barang & berapa.")
        res = sc.build_message("order", "bm", self.ASK, complete=fake)
        self.assertEqual(res["source"], "ai")
        self.assertEqual(fake.calls, [sc.SYSTEM_PROMPT])

    def test_no_data_check_ins_skip_the_judge(self):
        cases = {
            "open": "காலை வணக்கம் 👋 கடை ரெடியா? ஏதாவது குறைவா இருக்கா?",
            "lunch": "மதியம் கூட்டம் எப்படி இருந்துச்சு? ஏதாவது கறி சீக்கிரமே தீர்ந்து போச்சா?",
            "night": "ராத்திரி எல்லாம் சரியா? ஏதாவது உடைஞ்சதா, தீர்ந்ததா?",
        }
        for slot, text in cases.items():
            # Even a judge that would say "different" is never asked.
            fake = _scripted(text, same=False, reason="finished quickly vs ran out")
            res = sc.build_message(slot, "tamil", {}, complete=fake)
            self.assertEqual(res["source"], "ai", slot)
            self.assertEqual(fake.calls, [sc.SYSTEM_PROMPT], slot)
            self.assertIsNone(res["meaning_ok"])

    def test_no_data_check_ins_still_use_hard_rules(self):
        for slot, text in (
            ("lunch", "Lunch முடிஞ்சுது, கூட்டம் எப்படி?"),
            ("open", "காலை வணக்கம், shift முடிஞ்சுது?"),
            ("open", "கடை ரெடியா? சொல்லு"),
            ("lunch", "மதியம் கூட்டம் எப்படி? Ayam Goreng 5 pcs தீர்ந்துச்சா?"),
        ):
            res = sc.build_message(slot, "tamil", {}, complete=_scripted(text),
                                   vocabulary={"ayam goreng"})
            self.assertEqual(res["source"], "template", text)

    def test_data_slots_keep_the_judge(self):
        self.assertEqual(set(sc.MEANING_SLOTS), {"stock", "cook", "order", "bills"})

    def test_judge_prompt_treats_ran_out_words_as_same(self):
        for word in ("finished", "ran out", "sold out", "habis", "தீர்ந்து"):
            self.assertIn(word, sc.JUDGE_PROMPT)
        self.assertIn("lunch or the shift is over", sc.JUDGE_PROMPT)
        self.assertIn("different topic", sc.JUDGE_PROMPT)

    def test_informal_tamil_rejected(self):
        for rude in ("என்ன வேணும் சொல்லு?", "stock பாத்தியா?", "நீ order போடு"):
            problems = sc.fact_check(rude, {}, vocabulary=VOCAB, language="tamil")
            self.assertTrue(any(p.startswith("informal Tamil") for p in problems), rude)
        polite = "என்ன வேணும் சொல்லுங்க? stock பாத்தீங்களா? சரியா?"
        self.assertEqual(sc.informal_tamil(polite), [])

    def test_tamil_prompt_demands_respectful_form(self):
        self.assertIn("RESPECTFUL", sc._LANG_PROMPT["tamil"])
        self.assertIn("respectful", sc._LANG_PROMPT[sc.BM_TAMIL])

    def test_preview_shows_back_translation(self):
        res = sc.build_message("order", "tamil", self.ASK, complete=_scripted(self.TAMIL_OK))
        text = sc.format_preview("order", [
            {"outlet_code": "SEK20", "cashier": "Syed", "language": "tamil", "result": res}])
        self.assertIn("↳ back-translated: Is the shop ready?", text)


class LunchWordingTests(unittest.TestCase):
    """The lunch check-in asks two things (crowd, what ran out early) and
    never says lunch is finished."""

    def test_rejects_lunch_is_finished(self):
        for text in (
            "மதிய Lunch முடிஞ்சுது, கூட்டம் எப்படி இருந்துச்சு? ஏதாவது item சீக்கிரம் தீர்ந்து போச்சா?",
            "Lunch dah habis, ramai tak tadi? Ada lauk habis awal?",
            "Lepas lunch ni, ramai tak? Ada lauk habis awal?",
            "Lunch is over — how was the crowd?",
        ):
            problems = sc.fact_check(text, {}, vocabulary=set(), slot="lunch",
                                     language="tamil" if "ம" in text else "bm")
            self.assertTrue(any("is over" in p for p in problems), text)

    def test_allows_the_two_questions(self):
        for text in (
            "மதியம் கூட்டம் எப்படி இருந்துச்சு? ஏதாவது கறி சீக்கிரமே தீர்ந்து போச்சா?",
            "மதியம் Lunch நல்லா நடந்துச்சா? ஏதாவது item சீக்கிரம் முடிஞ்சு போச்சா?",
            "Lunch tadi ramai? Ada lauk habis awal?",
        ):
            problems = sc.fact_check(text, {}, vocabulary=set(), slot="lunch",
                                     language="tamil" if "ம" in text else "bm")
            self.assertFalse(any("is over" in p for p in problems), (text, problems))

    def test_only_applies_to_no_data_check_ins(self):
        self.assertEqual(sc.said_over("Lunch dah habis"), ["Lunch dah habis"])
        self.assertEqual(sc.said_over("Shift is over"), ["Shift is over"])
        for slot in ("open", "night"):
            problems = sc.fact_check("Shift dah habis?", {}, vocabulary=set(), slot=slot,
                                     language="bm")
            self.assertTrue(any("is over" in p for p in problems), slot)
        problems = sc.fact_check("Lunch dah habis?", {}, vocabulary=set(), slot="bills",
                                 language="bm")
        self.assertFalse(any("is over" in p for p in problems))

    def test_templates_pass_their_own_rule(self):
        for lang in ("bm", "tamil", "english", "indonesian", "bengali", sc.BM_TAMIL):
            for slot in sc.OVER_SLOTS:
                for variant in (0, 1):
                    text = sc.render_template(slot, lang, {}, variant)
                    self.assertEqual(sc.said_over(text), [], (lang, slot, text))

    def test_purpose_asks_two_questions_not_finished(self):
        purpose = sc._purpose("lunch", {})
        self.assertIn("run out", purpose)
        self.assertIn("crowd", purpose)
        self.assertIn("Do not say", purpose)

    def test_samples_show_pass_rate_per_language(self):
        rows = [
            {"slot": "lunch", "outlet_code": "SEK6", "cashier": "A", "language": "tamil",
             "result": {"source": "ai", "problems": [], "text": "x"}},
            {"slot": "lunch", "outlet_code": "SEK6", "cashier": "A", "language": "bm",
             "result": {"source": "template", "problems": ["meaning"], "text": "y"}},
            {"slot": "lunch", "outlet_code": "KLANG", "cashier": "B", "language": "bm",
             "result": {"source": "ai", "problems": [], "text": "z"}},
        ]
        text = sc.format_samples(rows)
        self.assertIn("3 BM + Tamil samples", text)
        self.assertIn("Pass rate Tamil: 1/1", text)
        self.assertIn("Pass rate BM: 1/2", text)


if __name__ == "__main__":
    unittest.main()
