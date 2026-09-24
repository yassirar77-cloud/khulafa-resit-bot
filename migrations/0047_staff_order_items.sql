-- What cashiers said they need, from their replies to the 20:05 order
-- check-in ("Esok nak order apa?" / "tomorrow I need ayam 40kg, ikan 10kg").
--
-- One row per item per reply. staff_orders.py merges these into the
-- order history (order_generator + order_sanity) as buying days for
-- ``order_for``, so outlets with thin receipt history get real drafts sooner.
-- A receipt for the same item within a day of ``order_for`` wins; the staff
-- row is then ignored (no double counting).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0047_staff_order_items.sql

CREATE TABLE IF NOT EXISTS public.staff_order_items (
    id              bigserial PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    outlet_code     text NOT NULL,          -- registry code (BISTRO7, SBESI ...)
    order_for       date NOT NULL,          -- the day the goods are for
    raw_item        text NOT NULL,          -- as the cashier wrote it
    canonical_item  text,                   -- item_canonicalization_v2; NULL = unknown
    qty             numeric(12, 3) NOT NULL CHECK (qty > 0),
    unit            text,                   -- kg / pcs / ekor / kotak ...
    cashier         text,
    thread_id       bigint REFERENCES public.staff_chat_thread(id) ON DELETE SET NULL,
    reply_text      text                    -- the whole reply, for audit
);

CREATE INDEX IF NOT EXISTS idx_staff_order_items_outlet_day
    ON public.staff_order_items (outlet_code, order_for);
CREATE INDEX IF NOT EXISTS idx_staff_order_items_thread
    ON public.staff_order_items (thread_id);
