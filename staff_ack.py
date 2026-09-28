"""Acknowledge a staff reply: one line saying what was understood.

After the reply reader has parsed a cashier's message (typed or spoken)
and the answer is saved, the bot replies in the cashier's language with
what it took from it — "Noted: 12 ayam, 5 kg bawang", "Noted: no wastage
yesterday" — so a wrong reading is caught on the spot. A voice note adds
the transcript on a second line so the words themselves can be corrected.

When the reader says the message is not an answer to the open question,
the bot says so and asks which question it is for, naming the questions
still open in that group.

Plain templates, no AI. The one line that carries facts (items, numbers)
goes through ``staff_chat.fact_check`` against those very facts; if it
fails, the bare status phrase goes instead. Never more than two lines.

Pure: no Telegram, no database.
"""
from __future__ import annotations

import staff_chat
import staff_nudge
import staff_orders

MAX_TRANSCRIPT = 120
MAX_ITEMS = 6

_NOTED = {"bm": "✅ Noted: {what}", "tamil": "✅ Noted: {what}", "english": "✅ Noted: {what}",
          "indonesian": "✅ Noted: {what}", "bengali": "✅ Noted: {what}"}

# What was understood, per reply status, in the cashier's language.
_STATUS = {
    "ok": {"bm": "semua OK", "tamil": "எல்லாம் சரி", "english": "all OK",
           "indonesian": "semua oke", "bengali": "shob thik"},
    "short": {"bm": "ada yang tak cukup", "tamil": "ஏதோ போதாது", "english": "something is short",
              "indonesian": "ada yang kurang", "bengali": "kichu kom ache"},
    "finished": {"bm": "ada yang habis", "tamil": "ஏதோ தீர்ந்துடுச்சு", "english": "something ran out",
                 "indonesian": "ada yang habis", "bengali": "kichu shesh hoyeche"},
    "problem": {"bm": "ada masalah", "tamil": "பிரச்சனை இருக்கு", "english": "there is a problem",
                "indonesian": "ada masalah", "bengali": "shomossha ache"},
    "order": {"bm": "order esok", "tamil": "நாளைய order", "english": "tomorrow's order",
              "indonesian": "order besok", "bengali": "kalker order"},
    "handed_in": {"bm": "bil dah bagi bos", "tamil": "bill boss-கிட்ட கொடுத்தாச்சு",
                  "english": "bill given to the boss", "indonesian": "nota sudah kasih bos",
                  "bengali": "bill boss ke deoa hoyeche"},
    "mismatch_explained": {"bm": "sebab bil tak sama dah dicatat",
                           "tamil": "bill வித்தியாசத்துக்கு காரணம் குறிச்சாச்சு",
                           "english": "reason for the bill difference noted",
                           "indonesian": "alasan nota beda sudah dicatat",
                           "bengali": "bill-er alada hoar karon likha hoyeche"},
    "other": {"bm": "jawapan awak", "tamil": "உங்க பதில்", "english": "your reply",
              "indonesian": "jawaban kamu", "bengali": "apnar uttor"},
}

# "All OK" means something more specific on some check-ins.
_OK_BY_SLOT = {
    "wastage": {"bm": "tiada buangan semalam", "tamil": "நேத்து wastage இல்ல",
                "english": "no wastage yesterday", "indonesian": "tidak ada buangan kemarin",
                "bengali": "gotokal kichu fela hoy nai"},
    "leftover": {"bm": "tiada baki", "tamil": "மீதி இல்ல", "english": "no leftovers",
                 "indonesian": "tidak ada sisa", "bengali": "baki nai"},
    "lunch": {"bm": "lunch OK, tiada yang habis awal", "tamil": "lunch சரி, சீக்கிரம் எதுவும் தீரல",
              "english": "lunch OK, nothing ran out early", "indonesian": "makan siang oke, tidak ada yang habis",
              "bengali": "lunch thik, kichu taratari shesh hoy nai"},
    "order": {"bm": "order esok OK", "tamil": "நாளைய order சரி", "english": "tomorrow's order OK",
              "indonesian": "order besok oke", "bengali": "kalker order thik"},
    "stock": {"bm": "stok cukup", "tamil": "stock போதும்", "english": "stock is enough",
              "indonesian": "stok cukup", "bengali": "stock jothesto"},
    "cook": {"bm": "plan masak OK", "tamil": "சமையல் plan சரி", "english": "cook plan OK",
             "indonesian": "rencana masak oke", "bengali": "ranna plan thik"},
    "bills": {"bm": "bil akan di-upload", "tamil": "bill upload பண்ணுவீங்க", "english": "bill will be uploaded",
              "indonesian": "nota akan di-upload", "bengali": "bill upload hobe"},
}

_YOU_SAID = {"bm": "🎤 Awak cakap: “{t}”", "tamil": "🎤 நீங்க சொன்னது: “{t}”",
             "english": "🎤 You said: “{t}”", "indonesian": "🎤 Kamu bilang: “{t}”",
             "bengali": "🎤 Apni bolechen: “{t}”"}

_UNMATCHED = {
    "bm": "Maaf, saya tak pasti soalan mana yang awak jawab. Untuk soalan mana? Reply pada mesej soalan tu ya: {open}",
    "tamil": "மன்னிக்கணும், எந்த கேள்விக்கு பதில்னு தெரியல. எந்த கேள்வி? அந்த கேள்வி message-க்கு reply பண்ணுங்க: {open}",
    "english": "Sorry, I'm not sure which question that answers. Which one is it for? Reply to that question's message: {open}",
    "indonesian": "Maaf, saya tidak yakin pertanyaan mana yang dijawab. Untuk yang mana? Balas ke pesan pertanyaannya ya: {open}",
    "bengali": "Sorry, bujhte parlam na kon proshner uttor eta. Kon-ta? Oi proshner message-e reply korun: {open}",
}


def _lang(language) -> str:
    language = str(language or "").lower()
    if language == staff_chat.BM_TAMIL:
        return "bm"
    return language if language in _STATUS["ok"] else "bm"


def _pick(table: dict, language) -> str:
    """One line. BM+Tamil readers get both, separated by a dash — the whole
    acknowledgement must stay within two lines."""
    if str(language or "").lower() == staff_chat.BM_TAMIL:
        return f"{table['bm']} — {table['tamil']}"
    return table.get(_lang(language)) or table["bm"]


def items_text(items) -> str:
    """"12 ayam, 5 kg bawang": the reader's items, as the cashier wrote them."""
    parts = []
    for it in staff_orders.clean_items(items)[:MAX_ITEMS]:
        qty = f"{it['qty']:g}"
        unit = f" {it['unit']}" if it.get("unit") else ""
        parts.append(f"{qty}{unit} {it['item']}".strip())
    more = max(0, len(staff_orders.clean_items(items)) - MAX_ITEMS)
    return ", ".join(parts) + (f" (+{more})" if more else "")


def understood(parsed: dict | None, language, slot=None) -> str:
    """The phrase after "Noted:" in the cashier's language."""
    status = str((parsed or {}).get("status") or "other")
    items = (parsed or {}).get("items") or []
    if items:
        text = items_text(items)
        if text:
            return text
    if status == "ok" and slot in _OK_BY_SLOT:
        return _pick(_OK_BY_SLOT[slot], language)
    return _pick(_STATUS.get(status, _STATUS["other"]), language)


def _fact_facts(parsed, transcript) -> dict:
    return {"items": staff_orders.clean_items((parsed or {}).get("items") or []),
            "transcript": transcript or ""}


def acknowledgement(parsed: dict | None, language, *, slot=None, transcript=None,
                    vocabulary=None) -> str:
    """The reply to send: "Noted: <what>" and, for a voice note, the
    transcript. The fact line is checked against the reader's own items
    (numbers, item words); if it fails, the bare status phrase is used."""
    what = understood(parsed, language, slot)
    line = _pick(_NOTED, language).format(what=what) if str(language or "") != staff_chat.BM_TAMIL \
        else f"✅ Noted: {what}"
    facts = _fact_facts(parsed, transcript)
    problems = staff_chat.fact_check(line, facts, vocabulary=vocabulary, language=language, slot=None)
    if problems:
        status = str((parsed or {}).get("status") or "other")
        safe = _pick(_STATUS.get(status, _STATUS["other"]), language)
        line = f"✅ Noted: {safe}"
    lines = [line]
    if transcript:
        t = str(transcript).strip().replace("\n", " ")
        if len(t) > MAX_TRANSCRIPT:
            t = t[:MAX_TRANSCRIPT - 1] + "…"
        lines.append(_pick(_YOU_SAID, language).format(t=t) if str(language or "") != staff_chat.BM_TAMIL
                     else _YOU_SAID["bm"].format(t=t))
    return "\n".join(lines[:2])


def open_questions_text(threads, language) -> str:
    """The open questions in the group, named in the cashier's language."""
    names = []
    words = staff_nudge._CHECKIN_WORDS.get(_lang(language), {})
    for t in threads or []:
        slot = str(t.get("slot") or "")
        name = words.get(slot) or staff_nudge.CHECKIN_NAMES.get(slot, slot)
        if name and name not in names:
            names.append(name)
    return ", ".join(names) if names else "—"


def unmatched(language, threads) -> str:
    """The message is not an answer to the open question: say so, ask which
    one it is for. Two lines at most."""
    text = _pick(_UNMATCHED, language) if str(language or "") != staff_chat.BM_TAMIL \
        else _UNMATCHED["bm"]
    return text.format(open=open_questions_text(threads, language))
