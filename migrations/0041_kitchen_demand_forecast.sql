-- Demand forecast log — what the bot told each kitchen to cook, and what the
-- day actually demanded.
--
-- Every morning `demand_forecast.gather_cook_plans` forecasts the day's demand
-- for each tracked kitchen item (from the trailing `kitchen_daily_usage`
-- history: POS dishes sold, or Used = Cooked − Left when POS is absent) and
-- recommends a cook quantity. One row per (outlet, business_date, item) is
-- written here at forecast time with actual_qty NULL.
--
-- The NEXT morning the same job scores yesterday's rows: `actual_qty` is the
-- demand that actually materialised, `abs_error` / `pct_error` measure the
-- forecast, and `sold_out` records whether the shop ran dry (left_qty = 0 —
-- meaning true demand was AT LEAST this, so the forecast may have been under
-- rather than exact). That scoring is what /forecast_accuracy reports, and it
-- is the only way the recommendation earns trust at the stove.
--
-- Nothing here feeds the Guna-vs-POS comparison or any money figure; the table
-- is advisory + self-measurement only. A missing table degrades gracefully:
-- the plan is still posted, it just isn't logged or scored.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0041_kitchen_demand_forecast.sql

CREATE TABLE IF NOT EXISTS public.kitchen_demand_forecast (
    id             bigserial PRIMARY KEY,
    outlet_code    text NOT NULL,           -- kitchen_daily_usage.outlet_code, e.g. 'SEK6'
    business_date  date NOT NULL,           -- the day being cooked for (the 18:00 date)
    item_code      text NOT NULL,           -- kitchen_usage.ITEM_BY_CODE key, e.g. 'ayam_goreng'
    unit           text NOT NULL CHECK (unit IN ('pcs', 'kg')),

    -- the model's output
    forecast_qty   numeric,                 -- expected demand (level x dow x trend)
    recommend_qty  numeric,                 -- what the kitchen was told to cook (forecast + safety)
    safety_pct     numeric,                 -- buffer applied, as a fraction (0.12 = +12%)
    dow_factor     numeric,                 -- weekday seasonality multiplier used
    trend_factor   numeric,                 -- recent-vs-prior trend multiplier used
    level_qty      numeric,                 -- the robust level (median) the factors multiplied
    samples        integer,                 -- data days behind the forecast
    confidence     text CHECK (confidence IN ('low', 'medium', 'high')),
    action         text CHECK (action IN ('CUT', 'RAISE', 'HOLD')),
    usual_cooked   numeric,                 -- what the shop has been cooking (median, recent days)

    -- filled the next morning by score_previous_day()
    actual_qty     numeric,                 -- demand that actually materialised
    abs_error      numeric,                 -- |actual − forecast|
    pct_error      numeric,                 -- abs_error / actual x 100 (NULL when actual = 0)
    sold_out       boolean,                 -- left_qty = 0 that day (demand was censored high)
    scored_at      timestamptz,

    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kitchen_demand_forecast_unique
        UNIQUE (outlet_code, business_date, item_code)
);

CREATE INDEX IF NOT EXISTS kitchen_demand_forecast_outlet_date_idx
    ON public.kitchen_demand_forecast (outlet_code, business_date);
CREATE INDEX IF NOT EXISTS kitchen_demand_forecast_date_idx
    ON public.kitchen_demand_forecast (business_date);
-- The accuracy report reads only scored rows.
CREATE INDEX IF NOT EXISTS kitchen_demand_forecast_scored_idx
    ON public.kitchen_demand_forecast (business_date)
    WHERE actual_qty IS NOT NULL;
