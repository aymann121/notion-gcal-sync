-- pg_net was created in public; Supabase's linter wants extensions out of the
-- public schema. Its functions live in the `net` schema regardless, so
-- public.invoke_sync_function keeps working unchanged.
drop extension if exists pg_net;
create extension pg_net with schema extensions;
