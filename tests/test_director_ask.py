"""Unit tests for ``director_ask`` — the plain-language item search.

Hermetic: the parser is pure (the item vocabulary is a local JSON file)
and the report builders run against the shared in-memory ``FakeSupabase``
double. Covers the questions the director actually types, in English,
Malay and the mix, plus the two guarantees the feature rests on:

* it never guesses an item (a miss offers near names instead), and
* it never raises, whatever is typed at it.

Run with::

    python -m unittest tests.test_director_ask
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402

import director_ask  # noqa: E402
from director_ask import (  # noqa: E402
    build_item_report,
    INTENT_BRANCH,
    INTENT_ITEMS,
    INTENT_LAST,
    INTENT_NONE,
    INTENT_SHOP,
    INTENT_SPEND,
    answer_question,
    build_item_index,
    build_last_purchases,
    build_spend_summary,
    detect_intent,
    extract_item_text,
    parse_question,
    parse_window,
)

TODAY = date(2026, 9, 22)


def _row(canonical, shop, price, receipt_id, days_ago=1, qty=10,
         item="BERAS SIAM 10KG", outlet="SEK6"):
    return {
        "canonical_item": canonical,
        "merchant": shop,
        "unit_price": price,
        "qty": qty,
        "receipt_id": receipt_id,
        "receipt_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "raw_item_name": item,
        "outlet_code": outlet,
    }


def _client(rows):
    client = FakeSupabase()
    for row in rows:
        client.table("item_prices").insert(row).execute()
    return client


class IntentDetection(unittest.TestCase):
    def test_where_questions_are_shop_questions(self):
        for text in (
            "beras buy from where",
            "beras beli kat mana",
            "where we buy beras",
            "kedai mana jual beras",
            "beras cheapest",
            "harga beras",
        ):
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text)[0], INTENT_SHOP)

    def test_last_purchase_questions(self):
        for text in (
            "bila last beli beras",
            "when did we last buy beras",
            "beras last purchase",
            "beras terakhir beli bila",
        ):
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text)[0], INTENT_LAST)

    def test_spend_questions(self):
        for text in (
            "berapa banyak beras kita beli bulan ni",
            "how much did we spend on beras",
            "belanja beras bulan lepas",
            "berapa kg ayam bulan ni",
        ):
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text)[0], INTENT_SPEND)

    def test_spend_beats_last_when_both_words_appear(self):
        # "last month" must not turn a money question into a purchase list.
        self.assertEqual(
            detect_intent("how much did we spend on beras last month")[0],
            INTENT_SPEND,
        )

    def test_branch_questions_win_over_price_words(self):
        for text in (
            "which branch pays most for beras",
            "outlet mana beli beras paling mahal",
            "compare beras price per outlet",
        ):
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text)[0], INTENT_BRANCH)

    def test_vocabulary_question(self):
        self.assertEqual(detect_intent("what items can i ask")[0], INTENT_ITEMS)
        self.assertEqual(detect_intent("senarai item")[0], INTENT_ITEMS)

    def test_chatter_names_no_intent(self):
        for text in ("ok", "sudah hantar bill", "terima kasih boss", ""):
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text)[0], INTENT_NONE)


class ItemExtraction(unittest.TestCase):
    def test_question_words_are_stripped(self):
        self.assertEqual(extract_item_text("beras buy from where?"), "beras")
        self.assertEqual(extract_item_text("bila last beli telur"), "telur")
        self.assertEqual(
            extract_item_text("berapa kita belanja untuk minyak bulan ni"),
            "minyak",
        )

    def test_the_cut_survives(self):
        # "paha ayam" must stay a cut — collapsing it to "ayam" would
        # answer about whole birds.
        self.assertEqual(extract_item_text("paha ayam beli kat mana"), "paha ayam")

    def test_numbers_and_period_words_go(self):
        self.assertEqual(extract_item_text("beras 30 hari"), "beras")
        self.assertEqual(extract_item_text("spend beras last month"), "beras")


class WindowParsing(unittest.TestCase):
    def test_default_is_the_last_30_days(self):
        window = parse_window("belanja beras", today=TODAY)
        self.assertFalse(window["explicit"])
        self.assertEqual(window["end"], TODAY)
        self.assertEqual((window["end"] - window["start"]).days, 29)

    def test_this_month_is_month_to_date(self):
        window = parse_window("belanja beras bulan ni", today=TODAY)
        self.assertTrue(window["explicit"])
        self.assertEqual(window["start"], date(2026, 9, 1))
        self.assertEqual(window["end"], TODAY)

    def test_last_month_is_the_whole_previous_month(self):
        window = parse_window("spend beras last month", today=TODAY)
        self.assertEqual(window["start"], date(2026, 8, 1))
        self.assertEqual(window["end"], date(2026, 8, 31))

    def test_explicit_counts(self):
        self.assertEqual(parse_window("beras 7 hari", today=TODAY)["start"],
                         TODAY - timedelta(days=6))
        self.assertEqual(parse_window("beras last 2 weeks", today=TODAY)["start"],
                         TODAY - timedelta(days=13))

    def test_today_and_yesterday(self):
        self.assertEqual(parse_window("beras hari ni", today=TODAY)["start"], TODAY)
        yesterday = parse_window("beras semalam", today=TODAY)
        self.assertEqual((yesterday["start"], yesterday["end"]),
                         (TODAY - timedelta(days=1), TODAY - timedelta(days=1)))


class QuestionParsing(unittest.TestCase):
    def test_the_headline_question_resolves(self):
        parsed = parse_question("beras buy from where", today=TODAY)
        self.assertEqual(parsed["intent"], INTENT_SHOP)
        self.assertEqual(parsed["canonical"], "beras")
        self.assertTrue(parsed["confident"])

    def test_english_synonyms_resolve(self):
        self.assertEqual(parse_question("chicken price", today=TODAY)["canonical"], "ayam")
        self.assertEqual(parse_question("where we buy cooking oil", today=TODAY)["canonical"],
                         "minyak_masak")

    def test_an_item_hidden_in_a_long_sentence_still_resolves(self):
        parsed = parse_question(
            "boss nak tanya, telur ni kita beli dari kedai mana ya?", today=TODAY
        )
        self.assertEqual(parsed["canonical"], "telur")
        self.assertEqual(parsed["intent"], INTENT_SHOP)

    def test_chatter_is_never_confident(self):
        for text in ("ok", "sudah hantar bill", "terima kasih", "bill dah upload"):
            with self.subTest(text=text):
                self.assertFalse(parse_question(text, today=TODAY)["confident"])

    def test_a_bare_item_name_is_not_confident_enough_for_a_group(self):
        # "beras" alone resolves, but nothing marks it as a question —
        # the group handler must stay silent on it.
        parsed = parse_question("beras", today=TODAY)
        self.assertEqual(parsed["canonical"], "beras")
        self.assertFalse(parsed["confident"])

    def test_a_question_mark_is_enough(self):
        self.assertTrue(parse_question("beras?", today=TODAY)["confident"])

    def test_unknown_item_offers_near_names(self):
        parsed = parse_question("where we buy zzzqqq", today=TODAY)
        self.assertIsNone(parsed["canonical"])
        self.assertFalse(parsed["confident"])

    def test_a_typo_is_offered_but_never_auto_selected(self):
        # "ayamm" shares no substring with any canonical key, so without
        # the spelling rescue the director gets a dead end. It must be a
        # suggestion, never a silent answer about ayam.
        parsed = parse_question("where we buy ayamm", today=TODAY)
        self.assertIsNone(parsed["canonical"])
        self.assertIn("ayam", parsed["suggestions"])
        self.assertIn("telur", parse_question("tellur beli kat mana")["suggestions"])

    def test_never_raises_on_junk(self):
        for junk in (None, "", 12345, [], "🙃🙃🙃", "?" * 500):
            with self.subTest(junk=junk):
                parsed = parse_question(junk, today=TODAY)
                self.assertIn("intent", parsed)


class LastPurchases(unittest.TestCase):
    def test_newest_first_with_shop_price_and_outlet(self):
        client = _client([
            _row("beras", "BALAJI ENTERPRISE", 3.10, 1, days_ago=2, qty=20),
            _row("beras", "MYMOON'S KITCHEN", 3.40, 2, days_ago=9, qty=10),
            _row("beras", "BALAJI ENTERPRISE", 3.00, 3, days_ago=40, qty=15),
        ])
        text = build_last_purchases(client, "beras", today=TODAY)
        self.assertIn("Beras", text)
        self.assertIn("RM3.10", text)
        self.assertIn("BALAJI", text)
        self.assertIn("SEK6", text)
        # Newest purchase leads.
        self.assertLess(text.index("RM3.10"), text.index("RM3.40"))
        self.assertIn("Last bought 2 days ago", text)

    def test_limit_is_respected(self):
        client = _client([
            _row("beras", "BALAJI ENTERPRISE", 3.00 + i / 100, i + 1, days_ago=i + 1)
            for i in range(10)
        ])
        text = build_last_purchases(client, "beras", limit=3, today=TODAY)
        self.assertIn("last 3 purchase(s)", text)
        self.assertEqual(text.count("BALAJI"), 3 + 1)  # 3 rows + the footer shop line

    def test_nothing_bought_says_so(self):
        text = build_last_purchases(_client([]), "beras", today=TODAY)
        self.assertIn("No purchase of Beras", text)

    def test_an_explicit_period_clips_both_ends(self):
        # "bila last beli beras bulan lepas" must not answer with a
        # September receipt.
        client = _client([
            _row("beras", "BALAJI ENTERPRISE", 3.10, 1, days_ago=2),
            _row("beras", "CHOP TONG HENG", 2.95, 2, days_ago=40),
        ])
        window = parse_window("bulan lepas", today=TODAY)
        text = build_last_purchases(
            client, "beras", lookback_days=120, today=TODAY, window=window
        )
        self.assertIn("CHOP TONG HENG", text)
        self.assertNotIn("BALAJI", text)
        # …and the heading says which period it answered for.
        self.assertIn("Aug 2026", text)

    def test_an_explicit_period_with_no_purchase_names_the_period(self):
        client = _client([_row("beras", "BALAJI ENTERPRISE", 3.10, 1, days_ago=2)])
        window = parse_window("bulan lepas", today=TODAY)
        text = build_last_purchases(
            client, "beras", lookback_days=120, today=TODAY, window=window
        )
        self.assertIn("No purchase of Beras in Aug 2026", text)

    def test_never_raises(self):
        self.assertIsInstance(build_last_purchases(None, "beras", today=TODAY), str)


class SpendSummary(unittest.TestCase):
    def test_totals_money_and_quantity_per_shop(self):
        client = _client([
            _row("beras", "BALAJI ENTERPRISE", 3.00, 1, days_ago=2, qty=20),
            _row("beras", "BALAJI ENTERPRISE", 3.00, 2, days_ago=5, qty=10),
            _row("beras", "MYMOON'S KITCHEN", 4.00, 3, days_ago=6, qty=5),
        ])
        text = build_spend_summary(client, "beras", today=TODAY)
        # 20*3 + 10*3 + 5*4 = 110
        self.assertIn("RM110.00", text)
        self.assertIn("Qty: 35", text)
        self.assertIn("By shop:", text)
        # Biggest spender first.
        self.assertLess(text.index("BALAJI"), text.index("MYMOON"))

    def test_window_is_honoured_at_both_ends(self):
        client = _client([
            _row("beras", "BALAJI ENTERPRISE", 3.00, 1, days_ago=2, qty=10),
            _row("beras", "BALAJI ENTERPRISE", 3.00, 2, days_ago=40, qty=100),
        ])
        window = parse_window("bulan ni", today=TODAY)
        text = build_spend_summary(client, "beras", window=window, today=TODAY)
        self.assertIn("RM30.00", text)
        self.assertNotIn("RM300.00", text)

    def test_lines_without_a_quantity_are_excluded_and_declared(self):
        client = _client([
            _row("beras", "BALAJI ENTERPRISE", 3.00, 1, days_ago=2, qty=10),
            _row("beras", "BALAJI ENTERPRISE", 3.00, 2, days_ago=3, qty=None),
        ])
        text = build_spend_summary(client, "beras", today=TODAY)
        self.assertIn("RM30.00", text)
        self.assertIn("no quantity", text)

    def test_outlet_split_only_when_more_than_one(self):
        single = _client([_row("beras", "BALAJI", 3.00, 1, days_ago=2, outlet="SEK6")])
        self.assertNotIn("By outlet:", build_spend_summary(single, "beras", today=TODAY))
        multi = _client([
            _row("beras", "BALAJI", 3.00, 1, days_ago=2, outlet="SEK6"),
            _row("beras", "BALAJI", 3.00, 2, days_ago=3, outlet="KLANG"),
        ])
        text = build_spend_summary(multi, "beras", today=TODAY)
        self.assertIn("By outlet:", text)
        self.assertIn("KLANG", text)

    def test_nothing_in_the_window_says_so(self):
        text = build_spend_summary(_client([]), "beras", today=TODAY)
        self.assertIn("No Beras bought", text)

    def test_never_raises(self):
        self.assertIsInstance(build_spend_summary(None, "beras", today=TODAY), str)


class ItemReport(unittest.TestCase):
    """The regression this report exists for.

    ``item_variant`` is the raw receipt line with the pack size stripped,
    so every OCR spelling becomes its own "cut". Grouping the answer by
    that turned one item into dozens of one-shop blocks, capped the blocks,
    and reported "only one supplier has priced this item" while the
    director's other suppliers sat behind "+38 more type(s)".
    """

    def _mixed_shop_client(self):
        return _client([
            # One wholesaler under three different OCR line texts …
            _row("beras", "BESTARI WHOLESALE", 39.80, 1, days_ago=4,
                 item="BERAS REBUS SAGA 10KG"),
            _row("beras", "BESTARI WHOLESALE", 33.90, 2, days_ago=4,
                 item="BASMATI KING JASMINE 5KG"),
            _row("beras", "BESTARI WHOLESALE", 110.00, 3, days_ago=9,
                 item="BERAS IDLY 10KG"),
            # … and the shops that used to be invisible behind them.
            _row("beras", "PASARAYA MINI JAYA", 42.00, 4, days_ago=6,
                 item="BERAS SIAM WANGI 10KG", outlet="KLANG"),
            _row("beras", "KEDAI RUNCIT AHMAD", 41.00, 5, days_ago=11,
                 item="BERAS SIAM WANGI 10KG", outlet="SEK20"),
        ])

    def test_every_shop_is_listed_not_just_the_first_few_cuts(self):
        text = build_item_report(self._mixed_shop_client(), "beras", today=TODAY)
        for shop in ("BESTARI WHOLESALE", "PASARAYA MINI JAYA", "KEDAI RUNCIT AHMAD"):
            self.assertIn(shop, text)
        self.assertIn("3 shop(s)", text)
        # The old report's verdict on exactly this data.
        self.assertNotIn("Only one supplier", text)

    def test_one_shop_appears_once_however_many_line_spellings_it_has(self):
        text = build_item_report(self._mixed_shop_client(), "beras", today=TODAY)
        shop_block = text[text.index("🏪"):text.index("🧾")]
        self.assertEqual(shop_block.count("BESTARI WHOLESALE"), 1)
        # …and its spread across those lines is shown rather than hidden.
        self.assertIn("range RM33.90–RM110.00", shop_block)

    def test_it_carries_dates_prices_quantities_and_the_outlet(self):
        text = build_item_report(self._mixed_shop_client(), "beras", today=TODAY)
        self.assertIn("🧾 Recent purchases", text)
        self.assertIn("RM42.00", text)
        self.assertIn("KLANG", text)
        self.assertIn("🏬 Outlets buying it:", text)
        self.assertIn("SEK20", text)

    def test_cheapest_is_only_claimed_within_one_comparable_cut(self):
        text = build_item_report(self._mixed_shop_client(), "beras", today=TODAY)
        compare = text[text.index("💡"):]
        # Both shops sold BERAS SIAM WANGI, so that one compares.
        self.assertIn("Beras Siam Wangi", compare)
        self.assertIn("🥇 KEDAI RUNCIT AHMAD", compare)
        # RM110 idly rice is never ranked against RM33.90 basmati.
        self.assertNotIn("Beras Idly", compare)

    def test_nothing_comparable_says_so_instead_of_blaming_the_data(self):
        client = _client([
            _row("beras", "BESTARI WHOLESALE", 39.80, 1, days_ago=4,
                 item="BERAS REBUS SAGA 10KG"),
            _row("beras", "JUTA RIA", 33.90, 2, days_ago=6,
                 item="BASMATI KING JASMINE 5KG"),
        ])
        text = build_item_report(client, "beras", today=TODAY)
        self.assertIn("nothing", text.lower())
        # Both shops are still named — that is the answer to the question.
        self.assertIn("BESTARI WHOLESALE", text)
        self.assertIn("JUTA RIA", text)

    def test_it_widens_to_a_year_rather_than_reporting_one_shop(self):
        client = _client([
            _row("beras", "BESTARI WHOLESALE", 39.80, 1, days_ago=4),
            _row("beras", "JUTA RIA", 38.50, 2, days_ago=200),
        ])
        text = build_item_report(client, "beras", today=TODAY)
        self.assertIn("JUTA RIA", text)
        self.assertIn("widened to a year", text)

    def test_filtered_rows_are_declared_not_silent(self):
        client = _client([
            _row("beras", "BESTARI WHOLESALE", 39.80, 1, days_ago=4),
            _row("beras", "JUTA RIA", 38.50, 2, days_ago=6),
            # An own-outlet transfer: correctly dropped, but the director
            # must be able to see that it WAS dropped.
            _row("beras", "RESTORAN KHULAFA", 1.70, 3, days_ago=5),
        ])
        text = build_item_report(client, "beras", today=TODAY)
        self.assertNotIn("KHULAFA", text.upper())
        self.assertIn("Left out", text)

    def test_debug_shows_the_read_and_kept_counts(self):
        text = build_item_report(self._mixed_shop_client(), "beras debug", today=TODAY)
        self.assertIn("read 5 row(s)", text)

    def test_unknown_item_still_never_guesses(self):
        text = build_item_report(self._mixed_shop_client(), "ayamm", today=TODAY)
        self.assertIn("Did you mean", text)
        self.assertNotIn("BESTARI", text)

    def test_nothing_bought_says_so(self):
        self.assertIn("No supplier bought Beras",
                      build_item_report(_client([]), "beras", today=TODAY))

    def test_never_raises(self):
        self.assertIsInstance(build_item_report(None, "beras", today=TODAY), str)
        self.assertIsInstance(build_item_report(_client([]), None, today=TODAY), str)


class AnswerRouting(unittest.TestCase):
    def setUp(self):
        self.client = _client([
            _row("beras", "BALAJI ENTERPRISE", 3.10, 1, days_ago=2, qty=20),
            _row("beras", "MYMOON'S KITCHEN", 3.40, 2, days_ago=5, qty=10),
        ])

    def test_where_do_we_buy_it_lists_every_shop(self):
        text = answer_question(self.client, "beras buy from where", today=TODAY)
        self.assertIn("🏪 Shops we buy it from", text)
        self.assertIn("BALAJI", text)
        self.assertIn("MYMOON", text)
        self.assertIn("2 shop(s)", text)

    def test_malay_phrasing_reaches_the_same_answer(self):
        english = answer_question(self.client, "where we buy beras", today=TODAY)
        malay = answer_question(self.client, "beras beli kat mana", today=TODAY)
        self.assertEqual(english, malay)

    def test_last_purchase_question_routes_to_the_history(self):
        text = answer_question(self.client, "bila last beli beras", today=TODAY)
        self.assertIn("last", text.lower())
        self.assertIn("BALAJI", text)

    def test_spend_question_routes_to_the_totals(self):
        text = answer_question(self.client, "berapa banyak beras bulan ni", today=TODAY)
        self.assertIn("Spend:", text)

    def test_vocabulary_question_lists_items(self):
        text = answer_question(self.client, "what items can i ask", today=TODAY)
        self.assertIn("Beras", text)
        self.assertIn("Ayam", text)

    def test_unknown_item_suggests_instead_of_guessing(self):
        text = answer_question(self.client, "where we buy ayamm", today=TODAY)
        self.assertNotIn("BALAJI", text)
        self.assertIn("Did you mean", text)
        self.assertIn("Ayam", text)

    def test_an_item_we_do_not_track_at_all_gets_the_examples(self):
        text = answer_question(self.client, "where we buy zzzqqq", today=TODAY)
        self.assertIn("don't track", text)
        self.assertIn("beras beli kat mana", text)

    def test_empty_question_shows_the_examples(self):
        self.assertEqual(answer_question(self.client, "   ", today=TODAY),
                         director_ask.HELP_TEXT)

    def test_never_raises_on_a_dead_client(self):
        self.assertIsInstance(
            answer_question(None, "beras beli kat mana", today=TODAY), str
        )

    def test_never_raises_on_junk_input(self):
        for junk in (None, 12345, [], "🙃"):
            with self.subTest(junk=junk):
                self.assertIsInstance(answer_question(self.client, junk, today=TODAY), str)


class ItemIndex(unittest.TestCase):
    def test_lists_the_canonical_vocabulary_with_examples(self):
        text = build_item_index()
        self.assertIn("Beras", text)
        self.assertIn("Minyak Masak", text)
        self.assertIn("beli kat mana", text)


if __name__ == "__main__":
    unittest.main()
