# Khulafa Stok Dapur (PWA)

Static site — no build step. Hosted as a Render static site (publish dir `stok_dapur`).

| Page | Who |
|---|---|
| `index.html` | Kitchen app (installable PWA, offline queue, PIN login) |
| `office.html` | Office dashboard (e-mail magic link; `stok_office_users`) |
| `panduan.html` | 1-page BM/Tamil/Bengali install guide to print |

Backend: Supabase project `khulafa-resit-bot` — migrations `0072_stok_dapur.sql`
(schema + RLS) and `0073_stok_items_seed.sql` (item list v2), Edge Function
`supabase/functions/stok-login` (PIN → scoped session). See `docs/stok_dapur.md`.

Tests: `node --test stok_dapur/tests/core.test.js` (also run by
`tests/test_stok_dapur_js.py`) and `tests/test_stok_sql.py` (set `STOK_TEST_PG`).
