"""Kitchen form and kitchen-report texts in the cashier's language.

Every message the kitchen flow posts in an outlet group (the forms, the
numpad, the pop-ups, the form reminders, the save confirmation, the Used vs
POS recaps and the wastage note) takes its words from here, in the language
set for that group and shift with ``/lang`` (cashier_names). Item names
(Ayam, Nasi …) and the trade words staff use every day (POS, form) stay as
they are. BM+Tamil readers get the BM line with the Tamil line under it.

No "!", no blame, never "Boss".
"""
from __future__ import annotations

import cashier_names

LANGS = ("bm", "tamil", "bengali", "english", "indonesian")

T: dict[str, dict[str, str]] = {
    # --- forms ------------------------------------------------------------
    "title_cooked": {
        "bm": "🍳 Rekod Masak — Petang",
        "tamil": "🍳 சமையல் பதிவு — மாலை",
        "bengali": "🍳 Ranna Record — Bikel",
        "english": "🍳 Cooked Record — Evening",
        "indonesian": "🍳 Catatan Masak — Sore",
    },
    "title_night": {
        "bm": "🌙 Rekod Masak Malam — Tambahan",
        "tamil": "🌙 இரவு சமையல் — கூடுதல்",
        "bengali": "🌙 Raater Ranna — Extra",
        "english": "🌙 Night Cooking — Extra",
        "indonesian": "🌙 Masak Malam — Tambahan",
    },
    "title_left": {
        "bm": "🌙 Rekod Baki — Tutup Kedai",
        "tamil": "🌙 மீதம் பதிவு — கடை மூடும்போது",
        "bengali": "🌙 Baki Record — Dokan Bondho",
        "english": "🌙 Leftover Record — Closing",
        "indonesian": "🌙 Catatan Sisa — Tutup Toko",
    },
    "prompt_cooked": {
        "bm": "berapa dimasak", "tamil": "எவ்வளவு சமைச்சீங்க",
        "bengali": "koto ranna holo", "english": "how much was cooked",
        "indonesian": "berapa dimasak",
    },
    "prompt_night": {
        "bm": "berapa tambah masak malam", "tamil": "இரவு எவ்வளவு கூடுதலா சமைச்சீங்க",
        "bengali": "raate koto extra ranna holo", "english": "how much extra cooked tonight",
        "indonesian": "berapa tambahan masak malam",
    },
    "prompt_left": {
        "bm": "berapa tinggal", "tamil": "எவ்வளவு மீதம் இருக்கு",
        "bengali": "koto baki ache", "english": "how much is left",
        "indonesian": "berapa sisa",
    },
    "instr_night": {
        "bm": "Tekan item untuk isi tambahan masak malam ({done} item). Kalau tiada, tak perlu isi.",
        "tamil": "Item-ஐ தட்டி இரவு கூடுதலா சமைச்சதை மட்டும் போடுங்க ({done} item). இல்லைன்னா விட்டுடுங்க.",
        "bengali": "Item-e tap kore raater extra ranna likhun ({done} item). Na thakle bad din.",
        "english": "Tap an item to enter tonight's extra cooking ({done} items). Skip if none.",
        "indonesian": "Tekan item untuk isi tambahan masak malam ({done} item). Kalau tidak ada, lewati saja.",
    },
    "instr": {
        "bm": "Tekan item untuk isi ({done}/{total}). Yang tak diisi = 0.",
        "tamil": "Item-ஐ தட்டி எண் போடுங்க ({done}/{total}). போடாதது = 0.",
        "bengali": "Item-e tap kore shongkha likhun ({done}/{total}). Na likhle = 0.",
        "english": "Tap an item to enter it ({done}/{total}). Not entered = 0.",
        "indonesian": "Tekan item untuk isi ({done}/{total}). Yang tidak diisi = 0.",
    },
    "fix": {
        "bm": "Tekan item sekali lagi untuk betulkan sebelum Hantar.",
        "tamil": "தப்பா போட்டா, Hantar-க்கு முன்னாடி item-ஐ மறுபடி தட்டி மாத்துங்க.",
        "bengali": "Bhul hole, Hantar-er age item-e abar tap kore thik korun.",
        "english": "Tap an item again to fix it before Hantar (send).",
        "indonesian": "Tekan item lagi untuk membetulkan sebelum Hantar.",
    },
    "send": {
        "bm": "📤 Hantar", "tamil": "📤 Hantar (அனுப்பு)", "bengali": "📤 Hantar (pathan)",
        "english": "📤 Hantar (send)", "indonesian": "📤 Hantar (kirim)",
    },
    "send_need": {
        "bm": "📤 Hantar (isi sekurang-kurangnya 1 item)",
        "tamil": "📤 Hantar (குறைஞ்சது 1 item போடுங்க)",
        "bengali": "📤 Hantar (kom pokkhe 1 item likhun)",
        "english": "📤 Hantar (enter at least 1 item)",
        "indonesian": "📤 Hantar (isi minimal 1 item)",
    },
    # --- numpad -----------------------------------------------------------
    "unit_kg": {
        "bm": "(kg, boleh 1 titik perpuluhan)", "tamil": "(kg, ஒரு புள்ளி போடலாம்)",
        "bengali": "(kg, ek dosomik chole)", "english": "(kg, 1 decimal allowed)",
        "indonesian": "(kg, boleh 1 desimal)",
    },
    "unit_pcs": {
        "bm": "(pcs, nombor bulat)", "tamil": "(pcs, முழு எண்)",
        "bengali": "(pcs, purno shongkha)", "english": "(pcs, whole number)",
        "indonesian": "(pcs, angka bulat)",
    },
    "numpad_now": {
        "bm": "Sekarang: {current}. Taip nombor baru → ✓. 🗑 untuk kosongkan.",
        "tamil": "இப்போ: {current}. புது எண் போட்டு → ✓. அழிக்க 🗑.",
        "bengali": "Ekhon: {current}. Notun shongkha likhun → ✓. Muchte 🗑.",
        "english": "Now: {current}. Type the new number → ✓. 🗑 to clear.",
        "indonesian": "Sekarang: {current}. Ketik angka baru → ✓. 🗑 untuk hapus.",
    },
    "btn_save": {
        "bm": "✓ Simpan", "tamil": "✓ Save", "bengali": "✓ Save",
        "english": "✓ Save", "indonesian": "✓ Simpan",
    },
    "btn_clear": {
        "bm": "🗑 Kosongkan", "tamil": "🗑 அழி", "bengali": "🗑 Muchun",
        "english": "🗑 Clear", "indonesian": "🗑 Hapus",
    },
    # --- tap pop-ups ------------------------------------------------------
    "session_over": {
        "bm": "Borang ini dah tamat. Tunggu borang baru.",
        "tamil": "இந்த form முடிஞ்சுது. புது form வரும் வரை காத்திருங்க.",
        "bengali": "Ei form shesh. Notun form-er jonno opekkha korun.",
        "english": "This form has closed. Please wait for the next one.",
        "indonesian": "Formulir ini sudah ditutup. Tunggu formulir baru.",
    },
    "already_sent": {
        "bm": "Borang ini dah dihantar.", "tamil": "இந்த form ஏற்கனவே அனுப்பியாச்சு.",
        "bengali": "Ei form age-i pathano hoyeche.", "english": "This form was already sent.",
        "indonesian": "Formulir ini sudah dikirim.",
    },
    "need_one": {
        "bm": "Isi sekurang-kurangnya 1 item tambahan dulu.",
        "tamil": "முதல்ல குறைஞ்சது 1 item போடுங்க.",
        "bengali": "Age kom pokkhe 1 item likhun.",
        "english": "Please enter at least 1 extra item first.",
        "indonesian": "Isi minimal 1 item tambahan dulu.",
    },
    "need_all": {
        "bm": "Isi semua item dulu sebelum Hantar.",
        "tamil": "Hantar-க்கு முன்னாடி எல்லா item-ஐயும் போடுங்க.",
        "bengali": "Hantar-er age shob item likhun.",
        "english": "Please fill in every item before Hantar.",
        "indonesian": "Isi semua item dulu sebelum Hantar.",
    },
    "save_failed": {
        "bm": "Tak dapat simpan. Cuba tekan Hantar sekali lagi — kalau masih tak jadi, maklumkan pejabat.",
        "tamil": "Save ஆகல. மறுபடி Hantar தட்டுங்க — இன்னும் ஆகலைன்னா office-க்கு சொல்லுங்க.",
        "bengali": "Save hoy nai. Abar Hantar tap korun — tao na hole office-ke janan.",
        "english": "Couldn't save. Please tap Hantar again — if it still fails, let the office know.",
        "indonesian": "Tidak bisa disimpan. Tekan Hantar sekali lagi — kalau masih gagal, beri tahu kantor.",
    },
    "saved": {
        "bm": "✅ Tersimpan — {title}", "tamil": "✅ Save ஆச்சு — {title}",
        "bengali": "✅ Save hoyeche — {title}", "english": "✅ Saved — {title}",
        "indonesian": "✅ Tersimpan — {title}",
    },
    # --- 02:00 save confirmation -------------------------------------------
    "save_head": {
        "bm": "✅ Rekod siap — Guna {outlet} {date}",
        "tamil": "✅ பதிவு முடிஞ்சுது — பயன்பாடு {outlet} {date}",
        "bengali": "✅ Record shesh — Byabohar {outlet} {date}",
        "english": "✅ Record done — Used {outlet} {date}",
        "indonesian": "✅ Catatan selesai — Terpakai {outlet} {date}",
    },
    "save_line": {
        "bm": "• {label}: masak {cooked}, baki {left}, guna {used} {unit}",
        "tamil": "• {label}: சமைச்சது {cooked}, மீதம் {left}, பயன்பாடு {used} {unit}",
        "bengali": "• {label}: ranna {cooked}, baki {left}, byabohar {used} {unit}",
        "english": "• {label}: cooked {cooked}, left {left}, used {used} {unit}",
        "indonesian": "• {label}: masak {cooked}, sisa {left}, terpakai {used} {unit}",
    },
    "save_foot": {
        "bm": "Guna vs POS akan keluar pagi nanti (lepas data POS masuk).",
        "tamil": "பயன்பாடு vs POS காலையில வரும் (POS data வந்த அப்புறம்).",
        "bengali": "Byabohar vs POS shokale ashbe (POS data ashar pore).",
        "english": "Used vs POS comes in the morning (after the POS data arrives).",
        "indonesian": "Terpakai vs POS keluar besok pagi (setelah data POS masuk).",
    },
    # --- form reminders ---------------------------------------------------
    "remind_head": {
        "bm": "⏰ Borang belum siap — {title} — {outlet} • {date}",
        "tamil": "⏰ Form இன்னும் முடியல — {title} — {outlet} • {date}",
        "bengali": "⏰ Form ekhono shesh hoy nai — {title} — {outlet} • {date}",
        "english": "⏰ Form not finished yet — {title} — {outlet} • {date}",
        "indonesian": "⏰ Formulir belum selesai — {title} — {outlet} • {date}",
    },
    "remind_all_filled": {
        "bm": "Semua item dah isi 👍 Cuma tekan Hantar pada borang di atas 🙏",
        "tamil": "எல்லா item-உம் போட்டாச்சு 👍 மேல form-ல Hantar மட்டும் தட்டுங்க 🙏",
        "bengali": "Shob item likha hoyeche 👍 Upore form-e shudhu Hantar tap korun 🙏",
        "english": "Every item is filled in 👍 Just tap Hantar on the form above 🙏",
        "indonesian": "Semua item sudah diisi 👍 Tinggal tekan Hantar di formulir atas 🙏",
    },
    "remind_missing": {
        "bm": "{missing} daripada {total} item belum diisi. Tekan item pada borang di atas, isi nombor, kemudian Hantar 🙏",
        "tamil": "{total} item-ல {missing} இன்னும் காலி. மேல form-ல item-ஐ தட்டி எண் போட்டு, அப்புறம் Hantar தட்டுங்க 🙏",
        "bengali": "{total}-ta item-er moddhe {missing}-ta ekhono khali. Upore form-e item tap kore shongkha likhun, tarpor Hantar 🙏",
        "english": "{missing} of {total} items are still empty. Tap them on the form above, enter the numbers, then Hantar 🙏",
        "indonesian": "{missing} dari {total} item belum diisi. Tekan item di formulir atas, isi angka, lalu Hantar 🙏",
    },
    "remind_many_head": {
        "bm": "⏰ {n} borang belum siap — {outlet}",
        "tamil": "⏰ {n} form இன்னும் முடியல — {outlet}",
        "bengali": "⏰ {n}-ta form ekhono shesh hoy nai — {outlet}",
        "english": "⏰ {n} forms not finished yet — {outlet}",
        "indonesian": "⏰ {n} formulir belum selesai — {outlet}",
    },
    "remind_state_filled": {
        "bm": "semua dah isi, tekan Hantar sahaja", "tamil": "எல்லாம் போட்டாச்சு, Hantar மட்டும் தட்டுங்க",
        "bengali": "shob likha, shudhu Hantar tap korun", "english": "all filled, just tap Hantar",
        "indonesian": "semua sudah diisi, tinggal tekan Hantar",
    },
    "remind_state_missing": {
        "bm": "{missing}/{total} item belum diisi", "tamil": "{total} item-ல {missing} இன்னும் காலி",
        "bengali": "{total}-ta item-er {missing}-ta khali", "english": "{missing}/{total} items still empty",
        "indonesian": "{missing}/{total} item belum diisi",
    },
    "remind_many_foot": {
        "bm": "Tekan item pada borang, isi nombor, kemudian Hantar 🙏",
        "tamil": "Form-ல item-ஐ தட்டி எண் போட்டு, அப்புறம் Hantar தட்டுங்க 🙏",
        "bengali": "Form-e item tap kore shongkha likhun, tarpor Hantar 🙏",
        "english": "Tap the items on each form, enter the numbers, then Hantar 🙏",
        "indonesian": "Tekan item di formulir, isi angka, lalu Hantar 🙏",
    },
    # --- Used vs POS recaps -------------------------------------------------
    "mini_head": {
        "bm": "📊 Ringkasan Guna vs POS", "tamil": "📊 பயன்பாடு vs POS சுருக்கம்",
        "bengali": "📊 Byabohar vs POS shongkkhep", "english": "📊 Used vs POS",
        "indonesian": "📊 Ringkasan Terpakai vs POS",
    },
    "mini_line": {
        "bm": "{mark} {label}: guna {used} vs POS {pos} {unit}",
        "tamil": "{mark} {label}: பயன்பாடு {used} vs POS {pos} {unit}",
        "bengali": "{mark} {label}: byabohar {used} vs POS {pos} {unit}",
        "english": "{mark} {label}: used {used} vs POS {pos} {unit}",
        "indonesian": "{mark} {label}: terpakai {used} vs POS {pos} {unit}",
    },
    "mini_line_buy": {
        "bm": "{mark} {label}: {used} {unit} guna vs {pos} {unit} beli",
        "tamil": "{mark} {label}: {used} {unit} பயன்பாடு vs {pos} {unit} வாங்கினது",
        "bengali": "{mark} {label}: {used} {unit} byabohar vs {pos} {unit} kena",
        "english": "{mark} {label}: {used} {unit} used vs {pos} {unit} bought",
        "indonesian": "{mark} {label}: {used} {unit} terpakai vs {pos} {unit} dibeli",
    },
    "mini_line_nobuy": {
        "bm": "➖ {label}: guna {used} {unit} vs tiada rekod beli",
        "tamil": "➖ {label}: பயன்பாடு {used} {unit}, வாங்கின பதிவு இல்ல",
        "bengali": "➖ {label}: byabohar {used} {unit}, kenar record nai",
        "english": "➖ {label}: used {used} {unit}, no purchase record",
        "indonesian": "➖ {label}: terpakai {used} {unit}, tidak ada catatan beli",
    },
    "mini_all_ok": {
        "bm": "Semua padan 👍", "tamil": "எல்லாம் சரியா இருக்கு 👍",
        "bengali": "Shob mile geche 👍", "english": "Everything matches 👍",
        "indonesian": "Semua cocok 👍",
    },
    "mini_legend": {
        "bm": "🔴 = guna lebih dari POS  ⚠️ = guna kurang dari POS (mungkin salah isi)",
        "tamil": "🔴 = POS-ஐ விட அதிகம் பயன்பாடு  ⚠️ = POS-ஐ விட குறைவு (தப்பா போட்டிருக்கலாம்)",
        "bengali": "🔴 = POS-er cheye beshi byabohar  ⚠️ = POS-er cheye kom (hoyto bhul likha)",
        "english": "🔴 = used more than POS  ⚠️ = used less than POS (maybe entered wrong)",
        "indonesian": "🔴 = terpakai lebih dari POS  ⚠️ = terpakai kurang dari POS (mungkin salah isi)",
    },
    "pos_wait_head": {
        "bm": "⏳ POS belum lengkap — {outlet} {date}",
        "tamil": "⏳ POS இன்னும் முழுசா வரல — {outlet} {date}",
        "bengali": "⏳ POS ekhono purno ashe nai — {outlet} {date}",
        "english": "⏳ POS not complete yet — {outlet} {date}",
        "indonesian": "⏳ POS belum lengkap — {outlet} {date}",
    },
    "pos_wait_none": {
        "bm": "Data POS belum masuk.", "tamil": "POS data இன்னும் வரல.",
        "bengali": "POS data ekhono ashe nai.", "english": "No POS data yet.",
        "indonesian": "Data POS belum masuk.",
    },
    "pos_wait_shift": {
        "bm": "Shift {shifts} belum masuk.", "tamil": "{shifts} shift இன்னும் வரல.",
        "bengali": "{shifts} shift ekhono ashe nai.", "english": "The {shifts} shift is not in yet.",
        "indonesian": "Shift {shifts} belum masuk.",
    },
    "pos_wait_summary": {
        "bm": "Ringkasan harian POS belum masuk.", "tamil": "POS daily summary இன்னும் வரல.",
        "bengali": "POS-er dainik shongkkhep ekhono ashe nai.", "english": "The POS daily summary is not in yet.",
        "indonesian": "Ringkasan harian POS belum masuk.",
    },
    "pos_wait_partial": {
        "bm": "Ringkasan harian POS baru sebahagian — tunggu email tutup pagi.",
        "tamil": "POS daily summary பாதி தான் வந்திருக்கு — காலை closing email-க்கு காத்திருக்கோம்.",
        "bengali": "POS-er dainik shongkkhep ekhono ardhek — shokaler closing email-er opekkha.",
        "english": "The POS daily summary only covers part of the day — waiting for the morning closing email.",
        "indonesian": "Ringkasan harian POS baru sebagian — menunggu email tutup pagi.",
    },
    "pos_wait_full": {
        "bm": "Menunggu data POS penuh.", "tamil": "முழு POS data-க்கு காத்திருக்கோம்.",
        "bengali": "Purno POS data-r opekkha.", "english": "Waiting for the full POS data.",
        "indonesian": "Menunggu data POS lengkap.",
    },
    "pos_wait_foot": {
        "bm": "Perbandingan Guna vs POS dibuat bila POS lengkap.",
        "tamil": "POS முழுசா வந்ததும் பயன்பாடு vs POS ஒப்பிடுவோம்.",
        "bengali": "POS purno hole byabohar vs POS milano hobe.",
        "english": "Used vs POS will be compared once the POS data is complete.",
        "indonesian": "Perbandingan Terpakai vs POS dibuat setelah POS lengkap.",
    },
    "shift_day": {"bm": "siang", "tamil": "பகல்", "bengali": "diner", "english": "day", "indonesian": "siang"},
    "shift_overnight": {"bm": "malam", "tamil": "இரவு", "bengali": "raater", "english": "night", "indonesian": "malam"},
    "daily_summary": {
        "bm": "ringkasan harian", "tamil": "daily summary", "bengali": "dainik shongkkhep",
        "english": "daily summary", "indonesian": "ringkasan harian",
    },
    "pos_missing": {
        "bm": "⚠️ POS {what} tidak masuk untuk {outlet} {date} — Guna vs POS tak dapat dibanding. Pejabat akan semak.",
        "tamil": "⚠️ {outlet} {date}-க்கு POS {what} வரல — பயன்பாடு vs POS ஒப்பிட முடியல. Office பாத்துக்கும்.",
        "bengali": "⚠️ {outlet} {date}-er POS {what} ashe nai — byabohar vs POS milano gelo na. Office dekhbe.",
        "english": "⚠️ POS {what} didn't arrive for {outlet} {date} — Used vs POS can't be compared. The office will check.",
        "indonesian": "⚠️ POS {what} tidak masuk untuk {outlet} {date} — Terpakai vs POS tidak bisa dibandingkan. Kantor akan cek.",
    },
    "pos_only_head": {
        "bm": "🧾 Jualan POS — {outlet} • {date}", "tamil": "🧾 POS விற்பனை — {outlet} • {date}",
        "bengali": "🧾 POS bikri — {outlet} • {date}", "english": "🧾 POS sales — {outlet} • {date}",
        "indonesian": "🧾 Penjualan POS — {outlet} • {date}",
    },
    "pos_only_intro": {
        "bm": "Rekod Masak/Baki tidak diisi hari tu, jadi Guna vs POS tak dapat dibanding. Ikut POS, jualan hari tu:",
        "tamil": "அன்னைக்கு சமையல்/மீதம் form fill ஆகல, அதனால பயன்பாடு vs POS ஒப்பிட முடியல. POS படி அன்னைக்கு விற்பனை:",
        "bengali": "Shedin ranna/baki form bhora hoy nai, tai byabohar vs POS milano gelo na. POS onujayi shediner bikri:",
        "english": "The cooked/leftover forms weren't filled that day, so Used vs POS can't be compared. The POS sold:",
        "indonesian": "Formulir masak/sisa tidak diisi hari itu, jadi Terpakai vs POS tidak bisa dibandingkan. Menurut POS, penjualan hari itu:",
    },
    "pos_only_line": {
        "bm": "• {label}: POS jual {pos} {unit}", "tamil": "• {label}: POS விற்பனை {pos} {unit}",
        "bengali": "• {label}: POS bikri {pos} {unit}", "english": "• {label}: POS sold {pos} {unit}",
        "indonesian": "• {label}: POS terjual {pos} {unit}",
    },
    "pos_only_foot": {
        "bm": "Tolong isi borang Masak/Baki setiap hari supaya Guna vs POS boleh dibanding 🙏",
        "tamil": "தினமும் சமையல்/மீதம் form fill பண்ணுங்க, அப்போ தான் பயன்பாடு vs POS சரியா வரும் 🙏",
        "bengali": "Protidin ranna/baki form bhorun, tahole byabohar vs POS thik ashbe 🙏",
        "english": "Please fill in the cooked/leftover forms every day so Used vs POS can be compared 🙏",
        "indonesian": "Tolong isi formulir masak/sisa setiap hari supaya Terpakai vs POS bisa dibandingkan 🙏",
    },
    # --- wastage note (after a recap with 🔴 items) -----------------------
    "waste_head": {
        "bm": "👨‍🍳 Untuk tukang masak — {outlet} • {date}",
        "tamil": "👨‍🍳 சமையல்காரருக்கு — {outlet} • {date}",
        "bengali": "👨‍🍳 Randhunir jonno — {outlet} • {date}",
        "english": "👨‍🍳 For the cook — {outlet} • {date}",
        "indonesian": "👨‍🍳 Untuk juru masak — {outlet} • {date}",
    },
    "waste_intro": {
        "bm": "Dapur guna lebih dari jualan POS:",
        "tamil": "POS-ல வித்ததை விட kitchen-ல அதிகமா பயன்படுத்தியிருக்கு:",
        "bengali": "POS-e ja bikri hoyeche tar cheye kitchen-e beshi byabohar hoyeche:",
        "english": "The kitchen used more than the POS sold:",
        "indonesian": "Dapur memakai lebih dari yang terjual di POS:",
    },
    "waste_line": {
        "bm": "• {label}: guna {used} {unit}, POS jual {pos} {unit} — lebih {over} {unit}",
        "tamil": "• {label}: பயன்பாடு {used} {unit}, POS விற்பனை {pos} {unit} — {over} {unit} அதிகம்",
        "bengali": "• {label}: byabohar {used} {unit}, POS bikri {pos} {unit} — {over} {unit} beshi",
        "english": "• {label}: used {used} {unit}, POS sold {pos} {unit} — {over} {unit} more",
        "indonesian": "• {label}: terpakai {used} {unit}, POS terjual {pos} {unit} — lebih {over} {unit}",
    },
    "waste_line_buy": {
        "bm": "• {label}: guna {used} {unit}, beli {pos} {unit} — lebih {over} {unit}",
        "tamil": "• {label}: பயன்பாடு {used} {unit}, வாங்கினது {pos} {unit} — {over} {unit} அதிகம்",
        "bengali": "• {label}: byabohar {used} {unit}, kena {pos} {unit} — {over} {unit} beshi",
        "english": "• {label}: used {used} {unit}, bought {pos} {unit} — {over} {unit} more",
        "indonesian": "• {label}: terpakai {used} {unit}, dibeli {pos} {unit} — lebih {over} {unit}",
    },
    "waste_foot": {
        "bm": "Mungkin termasak lebih dari keperluan. Tengok jualan dan masak ikut keperluan — kurang buang, kurang rugi 🙏",
        "tamil": "தேவைக்கு மேல சமைச்சிருக்கலாம். Sales பாத்து அளவா சமைச்சா wastage-உம் loss-உம் குறையும் 🙏",
        "bengali": "Hoyto proyojoner cheye beshi ranna hoyeche. Bikri dekhe mapa ranna korle wastage ar loss kome 🙏",
        "english": "Maybe more was cooked than needed. Cooking to the sales keeps waste and loss down 🙏",
        "indonesian": "Mungkin dimasak lebih dari kebutuhan. Masak sesuai penjualan supaya sisa dan rugi berkurang 🙏",
    },
}


def text(key: str, language: str, **values) -> str:
    """One kitchen text in ``language`` (BM+Tamil: both lines)."""
    table = T[key]
    if values:
        table = {lang: t.format(**values) for lang, t in table.items()}
    return cashier_names.pick(table, language)


def short(key: str, language: str, **values) -> str:
    """A single-line text (titles, buttons, pop-ups): BM+Tamil readers get
    the BM wording only, so it fits."""
    lang = language if language in LANGS else "bm"
    return T[key][lang].format(**values) if values else T[key][lang]
