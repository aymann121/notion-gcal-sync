-- Schedules for the Supabase-hosted sync. Each job POSTs to an Edge Function
-- with the shared x-cron-secret header. Every function no-ops until
-- sync_control.enabled is flipped at cutover, so these are safe to create
-- while the GitHub Actions version is still live.
--
-- Two Vault secrets must exist before the jobs can succeed (created once by
-- hand, so no secret lives in a migration):
--   select vault.create_secret('https://<ref>.supabase.co', 'project_url');
--   select vault.create_secret('<same value as the CRON_SECRET function secret>', 'cron_secret');

create extension if not exists pg_cron;
create extension if not exists pg_net;

create function public.invoke_sync_function(fn text)
returns bigint
language sql
security definer
set search_path = ''
as $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'project_url')
           || '/functions/v1/' || fn,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-cron-secret',
      (select decrypted_secret from vault.decrypted_secrets where name = 'cron_secret')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 5000
  );
$$;

revoke all on function public.invoke_sync_function(text) from public, anon, authenticated;

-- Google Tasks has no push API: a cheap change check every 10 minutes.
select cron.schedule('poll-gtasks', '*/10 * * * *', $$select public.invoke_sync_function('poll-gtasks')$$);

-- Full pass once a day in case a webhook was missed: 09:17 UTC, i.e. 5:17am
-- EDT / 4:17am EST (pg_cron runs in UTC and ignores DST).
select cron.schedule('sync-safety-net', '17 9 * * *', $$select public.invoke_sync_function('sync')$$);

-- Calendar push channels last 7 days; renewing every 2 leaves slack.
select cron.schedule('watch-gcal', '23 12 */2 * *', $$select public.invoke_sync_function('watch-gcal')$$);
