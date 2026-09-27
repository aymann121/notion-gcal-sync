-- Poll Google Tasks every 5 minutes instead of 10. cron.schedule with an
-- existing job name updates that job in place.
select cron.schedule('poll-gtasks', '*/5 * * * *', $$select public.invoke_sync_function('poll-gtasks')$$);
