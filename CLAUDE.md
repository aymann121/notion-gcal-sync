# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A two-way sync between a Notion "Tasks Tracker" database and Google Tasks + Google Calendar. It runs entirely on Supabase (project `nwdulegrvumwcygvohhd`, free plan): Edge Functions in Deno/TypeScript, with state in Postgres. Rows are routed by the Notion `Sync As` select property:

- `Task` (or empty) → Google Tasks: title, due date, Status ↔ completion, and Course relation → task list.
- `Event` → Google Calendar: title + due date as an all-day event, both directions.

**Migration in progress.** The original Python / GitHub Actions version lives in `cron (deprecated)/` and stays live until the cutover. `cron (deprecated)/README.md` has the runbook. Until then, `sync_control.enabled` is `false` and every function here no-ops, while the relay still forwards webhooks to GitHub (`FORWARD_TO_GITHUB`). Never have both sides live at once: they keep separate state, so both would create links for the same new row, which duplicates tasks.

## How it runs

```
Notion webhook ────────┐
GCal push ─────────────┴─► relay ───────────────┐
pg_cron */10 * * * * ──► poll-gtasks ─(change)──┼─► sync ─► one pass (sync_core.ts)
pg_cron 17 9 * * * (daily) ─────────────────────┘      state: sync_state, sync_control
pg_cron every 2 days ─► watch-gcal (renews the Calendar push channel → relay/gcal)
```

- **`functions/relay`** checks the Notion HMAC signature and the Calendar channel token, then POSTs to `sync`.
  - It drops Notion events authored only by the integration's own bot (`NOTION_BOT_ID`), so the sync's own writes don't trigger another sync.
  - Calendar notifications can't be filtered by author, so a pass that writes to Calendar costs one extra no-op pass.
- **`functions/sync`** marks the sync dirty, answers `202`, and drains the dirty flag in the background (`EdgeRuntime.waitUntil`). `POST ?dry_run=1` instead runs one pass synchronously with writes logged and skipped, saves nothing, and works while disabled.
- **`functions/poll-gtasks`**: Google Tasks has **no push API**, so this is the cheap poller. Every 10 minutes it compares a signature of all task lists (`id:title`), then asks each list for `updatedMin=<cursor>&maxResults=1` (with deleted, hidden and completed tasks included). It calls `sync` only if something changed. It also restarts leftover work (`dirty` set with the lease free), backing off 10 minutes after a failed pass.
- **`functions/watch-gcal`** opens a 7-day Calendar push channel to `relay/gcal`. pg_cron renews it every 2 days.
- **pg_cron** jobs (`migrations/*_cron_jobs.sql`) call the functions through `public.invoke_sync_function`, which reads `project_url` and `cron_secret` from Vault.

### Concurrency: lease + dirty flag

`sync_control` is a single row. The functions reach it only through RPCs (`migrations/*_sync_tables.sql`, callable by `service_role` alone). PostgREST pools its connections, so a session advisory lock isn't reliable; a lease row is used instead:

1. Every trigger calls `mark_dirty()`.
2. `claim_sync_lease(160)` succeeds only if `enabled` is true and nobody holds the lease, and it clears `dirty`.
3. After the pass, `release_sync_lease(error)` returns `dirty` in the same statement. If a webhook arrived mid-pass, the holder loops, up to `MAX_PASSES = 3`, with the poller picking up anything left.
4. A failed pass re-marks `dirty`.

The result is "one running + one pending", like the old GitHub `concurrency` group. The lease TTL outlives the 150 s wall-clock limit, so a dead pass frees it.

### State

`sync_state` has one row per linked Notion page. `load_sync_state()` and `save_sync_state(state, cursor)` use exactly the old `sync_state.json` shape, `{page_id: {last_sync, kind, task_id?, tasklist_id?, event_id?}}`. That makes the one-time import `select save_sync_state('<file>'::jsonb)`.

Two rules for how a pass handles state:

- **Only a successful pass saves.** The save replaces the whole table in one transaction, so a pass that fails part-way persists nothing.
- **The poller's cursor comes from the pass.** `runSyncPass` returns its start time, which becomes `gtasks_cursor`. The pass's own Google Tasks writes land after that time, so each writing pass costs one extra no-op pass.

## Commands

```bash
cd supabase
deno task test     # Deno test suite (functions/_shared/tests), in-memory fakes
deno task check    # type-check every function and test
supabase db push                       # apply migrations
supabase functions deploy <name>       # sync | poll-gtasks | watch-gcal | relay
supabase secrets set NAME=value
```

There's no linter or build step. The legacy Python suite is still run from `cron (deprecated)/` with `pytest`.

### Secrets (Edge Functions)

- `NOTION_TOKEN`, `NOTION_DATABASE_ID`.
- `GOOGLE_TOKEN_JSON`: the full `token.json` produced once, locally, by `cron (deprecated)/get_google_token.py`. The functions trade its refresh token for an access token on every pass.
- `GOOGLE_CALENDAR_ID`: optional, defaults to `primary`.
- `CRON_SECRET`: the `x-cron-secret` header for `sync`, `poll-gtasks` and `watch-gcal`. It's also stored in Vault as `cron_secret`, next to `project_url`.
- Relay:
  - `NOTION_VERIFICATION_TOKEN`: from the Notion subscription handshake. It's printed to the relay's logs, then pasted into Notion.
  - `NOTION_BOT_ID`: the integration's bot user id.
  - `GCAL_CHANNEL_TOKEN`.
  - `GITHUB_TOKEN`, `GITHUB_REPO` and `FORWARD_TO_GITHUB`: migration only.
- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are injected by Supabase automatically.

All four functions are deployed with `verify_jwt = false` (`config.toml`), because their callers can't send a Supabase JWT. The relay authenticates each request with the HMAC signature and the channel token; the other three use `CRON_SECRET`.

The Notion webhook subscription points at `<project>/functions/v1/relay/notion`, with the events `page.created`, `page.properties_updated`, `page.deleted` and `page.undeleted`.

## Code layout

- **`functions/_shared/sync_core.ts`**: the sync logic, a section-for-section port of `cron (deprecated)/sync.py`.
  - Each section opens with a `// ==== <name> ====` banner and is ordered so it only uses the sections above it: Config → Per-pass context → General utilities → State → Notion reading / writing / Courses → Google Calendar → Google Tasks → Status mapping → Core sync: Events → Core sync: Tasks → Entry point (`runSyncPass`).
  - Keep new code in the matching section.
  - All caches live on the per-pass `SyncContext`, never in module globals. Edge isolates are reused across requests, so a module-level cache would leak between passes.
- **`functions/_shared/clients.ts`**: plain `fetch` clients behind the `NotionApi`, `GCalApi` and `GTasksApi` interfaces.
  - Every non-2xx response throws `ApiError`.
  - Retries cover 429s, plus 5xx on non-POST calls. A POST that 500s may have created its object, so it isn't retried.
  - `dryRunClients` wraps real clients for `?dry_run=1`.
  - Notion is pinned to `Notion-Version: 2026-03-11`. That means rows are queried through `data_sources/{id}/query`, pages are created under a `data_source_id` parent, and trashing uses `in_trash`, since `archived` was removed in that version.
- **`functions/_shared/db.ts`**: the RPC wrappers.
- **`functions/_shared/gtasks_poll.ts`**: the poller's change detection.
- **`functions/_shared/auth.ts`**: the `x-cron-secret` check, function-to-function calls, and `runInBackground`.
- **`functions/_shared/tests/`**: `fakes.ts`, a port of the Python fakes whose not-found errors are real `ApiError`s, plus `*_test.ts`.

### Conflict resolution

For every linked pair, compare Notion's `last_edited_time` and Google's `updated` against `last_sync`:

- If only one side changed, the other side is updated to match.
- If both changed, **Notion wins**. This is a hardcoded branch, not a config flag.
- If a row isn't linked yet, a new object is created on the other side, and its id is written back onto the Notion page (`Google Event ID` / `Google Task ID`).

### Archived tasks

A companion repo, `notion-task-radar`, writes `Status = Archived` at 1am on Radar-course tasks that ended their day unfinished. Here, `Archived` is terminal like `Done`:

- `statusToGtasks` maps both to `completed`.
- A page that is already `Archived` when it first syncs is created, then completed with a follow-up patch. That patch is what reliably stamps Google's `completed` field.

The subtle part is the direction back:

- Completing the task bumps Google's `updated`, so the next pass takes the "Google changed" branch.
- There, a naive `completed` → `Done` mapping would silently rewrite every *missed* task as *finished*. `resolveNotionStatus` guards against this by leaving an `Archived` row alone when the Google task is completed.
- The regression test is `pass_test.ts` "Archived stays Archived across two passes". It only fails on the second pass.
- Un-ticking the Google task still revives the row (`Archived` + `needsAction` → `Not started`).

### Reverse-linking quirks

- **Tasks.** Google Tasks has no field for a Notion page id, so the link lives only in the page's `Google Task ID` property. `importUnlinkedGtasks` imports every unlinked Google Task into Notion, completed ones included (as `Done`). Nothing is skipped.
- **Events.** Events are tagged with a private extended property, `notion_page_id`. `handleOrphanGcalEvent` uses it to handle a Notion row that was deleted.
- **Task lists.**
  - A task's list is named after the title of its first `Course` relation (`notionTargetTasklistId` → `ensureTasklist`).
  - With no Course, it goes to "My Tasks", whose real id is resolved from the `@default` alias.
  - In the reverse direction, a list name is found or created as a Course page (`ensureCourse`), in the data source that the `Course` relation's schema points to.

### Deletions

`DELETE_SYNC = true`, and it applies both ways:

- Deleting or trashing a Notion row deletes its Google Task or Calendar event.
- A Google Task or event that no longer exists trashes its Notion page.

Because "missing" means "delete the counterpart", every lookup that can report something as missing goes through `isNotFound`. Only a Google 404/410 or a Notion `object_not_found` counts. Anything else throws and fails the pass. `transient_errors_test.ts` is the regression test.

There's also an ordering subtlety. `syncTaskPages` snapshots every Google Task before `deleteGtasksForRemovedPages` runs, so that function returns the ids it deleted. `importUnlinkedGtasks` then skips them; without that, they would be re-imported on the same pass.

## Gotchas when editing

- If a Notion property is renamed, update the matching `PROP_*` constant. There's no schema validation, so a mismatch fails silently: lookups return null or empty.
- Only the date part of `Due date` syncs, and all Calendar events are all-day.
- Adding a Google or Notion call means adding it to the interface in `clients.ts`, to `fakes.ts`, and, if it writes, to `dryRunClients`.
- The free Supabase project pauses after about 7 days without database activity. Before cutover, the GitHub `sync.yml` keepalive step prevents that. After cutover, the poller's RPCs every 10 minutes do.
- `client_secret.json` and `token.json` are gitignored and must never be committed.
