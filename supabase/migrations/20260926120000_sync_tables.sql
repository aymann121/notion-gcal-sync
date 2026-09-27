-- State for the Supabase-hosted sync (supabase/functions/sync, poll-gtasks).
--
-- sync_state replaces the Python version's sync_state.json: one row per linked
-- Notion page. load_sync_state/save_sync_state speak exactly that file's JSON
-- shape ({page_id: {last_sync, kind, task_id?, tasklist_id?, event_id?}}), so
-- the one-time import is `select public.save_sync_state('<file contents>')`.
--
-- sync_control is a single row: the cutover switch, the lease that keeps one
-- pass running at a time, the dirty flag that queues one more, and the Google
-- Tasks poller's cursor.
--
-- Only the Edge Functions touch these (as service_role), so RLS is on with no
-- policies and every RPC is revoked from anon/authenticated.

create table public.sync_state (
  page_id text primary key,
  kind text not null check (kind in ('task', 'event')),
  last_sync timestamptz not null,
  task_id text,
  tasklist_id text,
  event_id text
);

create table public.sync_control (
  id int primary key default 1 check (id = 1),
  -- Everything no-ops until this is flipped at cutover (see "cron (deprecated)/README.md").
  enabled boolean not null default false,
  dirty boolean not null default false,
  locked_until timestamptz not null default 'epoch',
  gtasks_cursor timestamptz,
  gtasks_lists_sig text,
  last_pass_at timestamptz,
  last_error text
);
insert into public.sync_control (id) values (1);

alter table public.sync_state enable row level security;
alter table public.sync_control enable row level security;
revoke all on public.sync_state, public.sync_control from anon, authenticated;


-- A change arrived (webhook, poller, cron). Whoever holds the lease runs again.
create function public.mark_dirty()
returns void
language sql
security definer
set search_path = ''
as $$
  update public.sync_control set dirty = true where id = 1;
$$;


-- Take the lease if sync is enabled and no pass holds it. Clears dirty, since
-- the pass about to run covers everything marked so far. PostgREST pools
-- connections, so a lease row is used instead of an advisory lock; its expiry
-- frees it if a pass dies mid-run.
create function public.claim_sync_lease(ttl_seconds int)
returns boolean
language sql
security definer
set search_path = ''
as $$
  with claimed as (
    update public.sync_control
    set locked_until = now() + make_interval(secs => ttl_seconds),
        dirty = false
    where id = 1 and enabled and locked_until < now()
    returning 1
  )
  select exists (select 1 from claimed);
$$;


-- Drop the lease and record the outcome. Returns dirty, read in the same
-- statement, so a change marked during the pass is never lost: the caller
-- loops if it's true. A failed pass re-marks dirty so the poller retries it.
create function public.release_sync_lease(error text default null)
returns boolean
language sql
security definer
set search_path = ''
as $$
  update public.sync_control
  set locked_until = 'epoch',
      last_pass_at = now(),
      last_error = error,
      dirty = dirty or error is not null
  where id = 1
  returning dirty;
$$;


create function public.get_sync_control()
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select to_jsonb(c) - 'id' from public.sync_control c where id = 1;
$$;


-- The poller's bookkeeping: a new lists signature, and, when it saw no task
-- changes, a new cursor. A null cursor leaves the current one alone.
create function public.set_gtasks_poll(cursor timestamptz, lists_sig text)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.sync_control
  set gtasks_cursor = coalesce(cursor, gtasks_cursor), gtasks_lists_sig = lists_sig
  where id = 1;
$$;


create function public.load_sync_state()
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    jsonb_object_agg(
      page_id,
      jsonb_strip_nulls(jsonb_build_object(
        'last_sync', last_sync,
        'kind', kind,
        'task_id', task_id,
        'tasklist_id', tasklist_id,
        'event_id', event_id
      ))
    ),
    '{}'::jsonb
  )
  from public.sync_state;
$$;


-- Replace the whole state in one transaction, like rewriting sync_state.json:
-- a pass that fails part-way never calls this, so nothing half-done persists.
-- `cursor` (the pass's start time) advances the Google Tasks poller.
create function public.save_sync_state(state jsonb, cursor timestamptz default null)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  delete from public.sync_state where true;
  insert into public.sync_state (page_id, kind, last_sync, task_id, tasklist_id, event_id)
  select key,
         value->>'kind',
         (value->>'last_sync')::timestamptz,
         value->>'task_id',
         value->>'tasklist_id',
         value->>'event_id'
  from jsonb_each(state)
  where jsonb_typeof(value) = 'object';

  if cursor is not null then
    update public.sync_control set gtasks_cursor = cursor where id = 1;
  end if;
end;
$$;


revoke all on function
  public.mark_dirty(),
  public.claim_sync_lease(int),
  public.release_sync_lease(text),
  public.get_sync_control(),
  public.set_gtasks_poll(timestamptz, text),
  public.load_sync_state(),
  public.save_sync_state(jsonb, timestamptz)
from public, anon, authenticated;

grant execute on function
  public.mark_dirty(),
  public.claim_sync_lease(int),
  public.release_sync_lease(text),
  public.get_sync_control(),
  public.set_gtasks_poll(timestamptz, text),
  public.load_sync_state(),
  public.save_sync_state(jsonb, timestamptz)
to service_role;
