-- Director Q&A (director_sql.py): a read-only SQL path for the bot.
--
-- 1. director_readonly: a role that can only SELECT from public tables
--    (and any table created later).
-- 2. director_sql(q text): the ONLY way the bot runs free SQL. SECURITY
--    DEFINER, but it immediately drops to director_readonly, marks the
--    transaction read-only and caps the statement at 5 seconds, then runs
--    the query wrapped in a jsonb_agg. It refuses anything that does not
--    start with SELECT or that carries a semicolon — the Python guard has
--    already checked much more; this is the belt to its braces.
-- 3. director_sql_log: every question, the SQL that ran, row count, time.
--
-- Apply once in Supabase SQL editor or via psql (as the postgres role):
--   psql "$SUPABASE_DB_URL" -f migrations/0055_director_sql.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly') THEN
        CREATE ROLE director_readonly NOLOGIN NOINHERIT;
    END IF;
END $$;

GRANT director_readonly TO postgres;
GRANT USAGE ON SCHEMA public TO director_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO director_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO director_readonly;

CREATE OR REPLACE FUNCTION public.director_sql(q text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    result jsonb;
BEGIN
    IF q IS NULL OR q !~* '^\s*select\M' THEN
        RAISE EXCEPTION 'director_sql: SELECT only';
    END IF;
    IF position(';' IN q) > 0 THEN
        RAISE EXCEPTION 'director_sql: one statement only';
    END IF;
    SET LOCAL statement_timeout = '5s';
    SET LOCAL transaction_read_only = on;
    SET LOCAL ROLE director_readonly;
    EXECUTE format('SELECT coalesce(jsonb_agg(t), ''[]''::jsonb) FROM (%s) AS t', q)
        INTO result;
    RETURN result;
END $$;

-- Only the service role (the bot) may call it; never the anon key.
REVOKE ALL ON FUNCTION public.director_sql(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.director_sql(text) FROM anon;
GRANT EXECUTE ON FUNCTION public.director_sql(text) TO service_role;

CREATE TABLE IF NOT EXISTS public.director_sql_log (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    chat_id     bigint,
    user_id     bigint,
    question    text NOT NULL,
    sql         text,
    raw_sql     text,
    row_count   integer NOT NULL DEFAULT 0,
    ok          boolean NOT NULL DEFAULT false,
    error       text,
    answer      text,
    ms          integer
);

CREATE INDEX IF NOT EXISTS idx_director_sql_log_created
    ON public.director_sql_log (created_at DESC);
