-- 0066: close the last anon read path found after 0062.
--
-- audit_responses (audit questions sent to outlet managers and their
-- replies) carried an "anon_read" SELECT USING (true) policy, so the anon key
-- (served publicly by /webapp May-July 2026) could still read it. The bot
-- uses the service_role key (service_role_all policy) and the director's
-- read-only role has audit_responses_director_read; neither changes.

BEGIN;

DROP POLICY IF EXISTS anon_read ON public.audit_responses;
REVOKE ALL ON public.audit_responses FROM anon, authenticated;

COMMIT;
