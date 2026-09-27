# Notion ↔ Google Tasks + Calendar Sync

Keeps a Notion **Tasks Tracker** database in sync with Google Tasks and Google
Calendar. Runs entirely on Supabase.

| Notion `Sync As` | Syncs to | What syncs |
|---|---|---|
| `Task` or empty | Google Tasks | Title, due date, done/not done. Course → task list |
| `Event` | Google Calendar | Title + due date as an all-day event |

## How it works

- **Notion and Calendar edits** reach the `relay` function as webhooks and sync within seconds.
- **Google Tasks edits** are picked up by a poller every 5 minutes (Google Tasks can't send webhooks).
- **A full sync** also runs once a day, in case anything was missed.
- If both sides changed, **Notion wins**. Deleting on one side deletes on the other.

## Setup

You need the [Supabase CLI](https://supabase.com/docs/guides/cli) and a
`token.json` from `get_google_token.py` in the legacy folder.

1. **Set secrets**
   ```bash
   supabase secrets set NOTION_TOKEN=... NOTION_DATABASE_ID=... \
     GOOGLE_TOKEN_JSON="$(cat token.json)" GOOGLE_CALENDAR_ID=primary \
     CRON_SECRET=$(openssl rand -hex 32)
   ```
2. **Add Vault secrets** (Supabase SQL editor), so the scheduled jobs can call the functions:
   ```sql
   select vault.create_secret('https://<project-ref>.supabase.co', 'project_url');
   select vault.create_secret('<your CRON_SECRET>', 'cron_secret');
   ```
3. **Deploy**
   ```bash
   supabase db push
   supabase functions deploy sync
   supabase functions deploy poll-gtasks
   supabase functions deploy watch-gcal
   supabase functions deploy relay
   ```
4. **Turn it on** (SQL editor):
   ```sql
   update public.sync_control set enabled = true, dirty = true where id = 1;
   ```

> Migrating from the old GitHub Actions version? Don't do step 4 on its own.
> Follow the cutover runbook in the legacy folder's README instead, so the
> two versions never run at the same time.

## Check it's working

```sql
select public.get_sync_control();  -- last_pass_at should be recent, last_error null
```

Function logs are in the Supabase dashboard → Edge Functions.

## Tests

```bash
cd supabase
deno task test
```

## More detail

- `CLAUDE.md`: architecture, sync rules, and gotchas.
- `cron (deprecated)/README.md`: the old GitHub Actions version and the cutover runbook.
