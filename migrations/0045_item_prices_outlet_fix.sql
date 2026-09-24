-- Re-tag item_prices rows from outlet groups the chat-title rules got wrong
-- (fixed in outlet_mapping, PR #125):
--   * Klang's group "Hj Sharfuddin Klang Bayumas" matched "sharfuddin" first
--     and every Klang bill was counted as SEK6;
--   * Signature (SEK14), SEK 15 (SEK15) and Kl Sg Besi (SBESI) matched
--     nothing, so their rows had no outlet_code and the order-draft job never
--     built a draft for them.
--
-- Keyed on the group's chat_id, not the title, so it touches only those four
-- groups. Every changed row is recorded first in item_prices_outlet_fix_0045
-- (id, old code, new code) so the change can be reversed:
--   UPDATE item_prices p SET outlet_code = f.old_code
--   FROM item_prices_outlet_fix_0045 f WHERE p.id = f.id;

CREATE TABLE IF NOT EXISTS public.item_prices_outlet_fix_0045 (
    id         bigint PRIMARY KEY,
    chat_id    bigint,
    old_code   text,
    new_code   text,
    fixed_at   timestamptz DEFAULT now()
);

WITH fix(chat_id, new_code) AS (VALUES
    (-5003341957::bigint, 'KLANG'),
    (-5245109363::bigint, 'SEK14'),
    (-5193031632::bigint, 'SEK15'),
    (-5163000846::bigint, 'SBESI')
)
INSERT INTO public.item_prices_outlet_fix_0045 (id, chat_id, old_code, new_code)
SELECT p.id, p.chat_id, p.outlet_code, f.new_code
FROM public.item_prices p JOIN fix f ON p.chat_id = f.chat_id
WHERE p.outlet_code IS DISTINCT FROM f.new_code
ON CONFLICT (id) DO NOTHING;

UPDATE public.item_prices p
SET outlet_code = f.new_code
FROM public.item_prices_outlet_fix_0045 f
WHERE p.id = f.id AND p.outlet_code IS DISTINCT FROM f.new_code;
