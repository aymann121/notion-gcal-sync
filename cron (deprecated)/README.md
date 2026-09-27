# The GitHub Actions version (deprecated, still live)

This folder is the original Python implementation of the Notion ↔ Google
Tasks + Google Calendar sync. It runs on GitHub Actions and stays live until
the Supabase version at the repo root takes over. After that cutover
(runbook below), delete this folder and the two workflows.

For the Supabase version, see the root `CLAUDE.md`.

## What it does

A Notion "Tasks Tracker" database is synced two ways, routed by the `Sync As`
select property:

| `Sync As` | Destination | What syncs |
|---|---|---|
| `Task` or empty | Google Tasks | Title, due date, Status ↔ completion. Course → task list |
| `Event` | Google Calendar | Title + due date as an all-day event (both directions) |

Every run is one full pass. It compares Notion's `last_edited_time` and
Google's `updated` against `last_sync` in `sync_state.json`:

- If only one side changed, the other side is updated to match.
- If both changed, **Notion wins**.
- Unlinked rows and tasks are created on the other side, and the Google id is
  written back to the page's `Google Task ID` / `Google Event ID` property.

`Archived` (written by `notion-task-radar` for missed tasks) is terminal like
`Done`: it completes the Google Task. `resolve_notion_status` stops a
completed Google Task from rewriting an `Archived` row as `Done`. Un-ticking
the task in Google revives the row as `Not started`.

`DELETE_SYNC = True` mirrors deletions both ways. Only a genuine 404/410 or
`object_not_found` counts as "deleted" (`is_not_found`). A rate-limit or a
500 fails the run instead, so a bad API day can't delete live data.

The Supabase port in `supabase/functions/_shared/sync_core.ts` keeps all of
this logic, section for section.

## How it runs now

The workflows must live in `.github/workflows/` at the repo root, but both
now run inside this folder (`defaults.run.working-directory: "cron (deprecated)"`):

- **`sync.yml`** runs `python sync.py`. It is triggered three ways:
  - `repository_dispatch` from the relay, while the relay's
    `FORWARD_TO_GITHUB` isn't `"false"`.
  - An hourly cron (`17 13-23,0-3 * * *`).
  - A manual `workflow_dispatch`.

  After each run it commits `cron (deprecated)/sync_state.json` back to `main`, with a
  rebase-and-retry loop. `concurrency: sync` keeps one run going and one
  pending. It also calls the `public.keepalive()` RPC so the free-plan
  Supabase project doesn't pause.
- **`gcal-watch.yml`** runs `python watch_gcal.py` every 2 days to renew the
  Google Calendar push channel, which points at the relay.

GitHub repo secrets:

- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`
- `GOOGLE_CALENDAR_ID`
- `GOOGLE_TOKEN_JSON` (the contents of `token.json`)
- `RELAY_URL`
- `GCAL_CHANNEL_TOKEN`

## Files

- `sync.py`: the whole sync, in `# ==== <name> ====` sections ordered so each
  one only uses those above it.
- `watch_gcal.py`: opens a 7-day Calendar push channel to `<RELAY_URL>/gcal`.
- `get_google_token.py`: the one-time local OAuth flow. It reads
  `client_secret.json` from the current directory and writes `token.json`.
  The Supabase version reuses the same `token.json`.
- `sync_state.json`: the state, committed by CI.
- `tests/`: three layers.
  - `unit/`: pure helpers.
  - `integration/`: against the in-memory fakes in `tests/fakes.py`.
  - `e2e/`: opt-in, against real accounts. It needs `E2E_ENABLE=1`, a
    disposable Notion database and a non-primary calendar. Never point it at
    production.

## Commands

Run these from inside this folder:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                        # unit + integration; e2e excluded by pytest.ini
pytest -m e2e tests/e2e -v    # needs E2E_ENABLE=1 and disposable resources
python sync.py                # one full pass; needs the env vars above
```

## Cutover runbook: GitHub Actions → Supabase

The two versions keep separate state. They must never both be live, or each
would create its own Google Task for the same new Notion row. Two switches
guarantee that:

- `sync_control.enabled` in Postgres gates the Supabase side, and it starts
  `false`.
- The Actions tab's enable/disable toggle gates the GitHub side.

1. **Secrets.** Set these Supabase function secrets (`supabase secrets set …`):
   - `NOTION_TOKEN`
   - `NOTION_DATABASE_ID`
   - `GOOGLE_CALENDAR_ID`
   - `GOOGLE_TOKEN_JSON` (the contents of `token.json`)
   - `CRON_SECRET` (`openssl rand -hex 32`)

   `GCAL_CHANNEL_TOKEN`, `NOTION_BOT_ID`, `NOTION_VERIFICATION_TOKEN`,
   `GITHUB_TOKEN` and `GITHUB_REPO` already exist for the relay. Then create
   the two Vault secrets that pg_cron uses (SQL editor):
   ```sql
   select vault.create_secret('https://nwdulegrvumwcygvohhd.supabase.co', 'project_url');
   select vault.create_secret('<the CRON_SECRET value>', 'cron_secret');
   ```
2. **Deploy.** Run `supabase db push` for the two new migrations, then
   `supabase functions deploy` for `sync`, `poll-gtasks`, `watch-gcal` and
   `relay`. Nothing changes yet: every function no-ops while `enabled` is
   false, and the relay keeps forwarding to GitHub.
3. **Dry run.** Import the current state, then run one pass with writes
   logged and skipped:
   ```sql
   select public.save_sync_state('<contents of cron (deprecated)/sync_state.json>'::jsonb);
   ```
   ```bash
   curl -X POST "https://nwdulegrvumwcygvohhd.supabase.co/functions/v1/sync?dry_run=1" \
     -H "x-cron-secret: $CRON_SECRET"
   ```
   GitHub just synced, so `writes` should be (nearly) empty. Any unexpected
   write is a porting bug, so stop and investigate before going further.
4. **Stop GitHub.** In the Actions tab, disable both "Notion <-> Google Tasks
   + Calendar Sync" and "Renew Google Calendar push channel". Wait for any
   in-flight run to finish.
5. **Hand over.** Run `git pull` to get the final `cron (deprecated)/sync_state.json`,
   re-import it with the same `save_sync_state` call, and then:
   ```sql
   update public.sync_control set enabled = true, dirty = true where id = 1;
   ```
6. **Relay and channel.** Run `supabase secrets set FORWARD_TO_GITHUB=false`,
   then open a Calendar channel from the Supabase side:
   ```bash
   curl -X POST "https://nwdulegrvumwcygvohhd.supabase.co/functions/v1/watch-gcal" \
     -H "x-cron-secret: $CRON_SECRET"
   ```
7. **Watch it.** Edit a Notion row, a Calendar event and a Google Task. The
   first two should sync within seconds, and the task within about 5 minutes.
   `select public.get_sync_control();` should show a fresh `last_pass_at` and a
   null `last_error`.

**Rollback** (any time before step 8): set `enabled = false`, unset
`FORWARD_TO_GITHUB`, and re-enable the two workflows. First export
`select public.load_sync_state();` into `cron (deprecated)/sync_state.json` and commit
it, so GitHub resumes from the newest state.

8. **Cleanup** (after about a week of clean runs):
   - Delete this folder and both workflows.
   - Remove `dispatch()` and the `FORWARD_TO_GITHUB` branch from the relay,
     and delete the `GITHUB_TOKEN` / `GITHUB_REPO` secrets.
   - Drop `public.keepalive()` in a new migration. The poller's database
     calls every 5 minutes keep the project awake now.
