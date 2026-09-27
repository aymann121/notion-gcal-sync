-- Free-plan projects pause after ~7 days without database activity, and the
-- relay Edge Function never touches the database. sync.yml calls this once
-- per run so the project stays awake. It reads nothing and writes nothing.
create or replace function public.keepalive()
returns timestamptz
language sql
stable
set search_path = ''
as $$ select now() $$;

revoke all on function public.keepalive() from public;
grant execute on function public.keepalive() to anon;
