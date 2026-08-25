# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python script (`sync.py`) that two-way syncs a Notion "Tasks Tracker" database with Google Tasks and Google Calendar, routed by a Notion `Sync As` select property:

- `Task` (or empty) → Google Tasks — title, due date, Status ↔ completion, Course relation → task list
- `Event` → Google Calendar — title + due date as an all-day event (both directions)

It runs on a schedule via GitHub Actions (`.github/workflows/sync.yml`, hourly cron + manual `workflow_dispatch`), committing its own state file back to the repo after each run.

## Commands

```bash
pip install -r requirements.txt   # deps: notion-client, google-api-python-client, google-auth(-oauthlib)
python get_google_token.py        # one-time, locally only: OAuth flow, produces token.json
python sync.py                    # run one full sync pass
```

There is no test suite, linter, or build step in this repo — it's a script run directly.

### Required environment variables (for `sync.py`)

- `NOTION_TOKEN`, `NOTION_DATABASE_ID`
- `GOOGLE_TOKEN_JSON` — full contents of `token.json`
- `GOOGLE_CALENDAR_ID` — optional, defaults to `primary`

In GitHub Actions these come from repo secrets; locally, export them in your shell before running `sync.py`.

## Architecture

Everything lives in `sync.py`, organized top-to-bottom as:

1. **Config constants** — `PROP_*` map to exact Notion property names (title/date/status/relation/select/rich_text). If a Notion property is renamed, update the constant here — nothing else needs to change.
2. **State** (`sync_state.json`) — a `{notion_page_id: {last_sync, kind, ...}}` map, the source of truth for "what changed since last run." It's the *only* persisted state; there's no database. In CI it round-trips via `actions/cache` and is committed back to `main` after each run (see workflow retry/rebase loop below).
3. **Notion helpers** — thin wrappers over `notion_client` for reading/writing specific properties (`notion_title`, `notion_due_date`, `set_notion_status`, etc.).
4. **Google Calendar helpers** and **Google Tasks helpers** — thin wrappers over the `googleapiclient` discovery API (`gcal`, `gtasks` clients built once at module load from `GOOGLE_TOKEN_JSON`).
5. **Core sync logic** — `sync_event_pages` and `sync_task_pages`, each called once per run from `sync()`.

### Conflict resolution

For every linked pair, compare `last_edited_time` (Notion) vs `updated` (Google) against `last_sync` in the state file:

- Only one side changed since `last_sync` → the other side is updated to match.
- Both changed → **Notion wins** (this is a hardcoded branch in each sync function, not a config flag).
- Neither side has a link yet → a new object is created on the other side and the id is written back onto the Notion page (`Google Event ID` / `Google Task ID` rich_text properties).

### Reverse-linking quirks

- **Tasks**: since Google Tasks has no field to store a Notion page id, matching a Google Task back to a Notion page relies on the `Google Task ID` rich_text property written onto the Notion page. Any Google Task not yet linked to a page is treated as "created directly in Google Tasks" and imported into Notion by `import_unlinked_gtasks` (called at the end of `sync_task_pages`) — but only if its status is `needsAction`; already-completed unlinked tasks are left alone so old history doesn't flood in. Once a Notion page for a task is deleted/archived, its Google Task id is recorded in the state file's `_ignored_task_ids` list so it's never re-imported.
- **Events**: Calendar events created by this script are tagged with a private extended property `notion_page_id`, which lets a deleted Notion row be recreated from the Calendar side (`sync_event_pages`'s second loop, over `list_gcal_events_from_notion()`).
- **Task lists**: a task's Google Tasks list is derived from its first `Course` relation's title (`notion_target_tasklist_id` → `ensure_tasklist`, cached per run in `_tasklist_cache`); no Course → Google's default "My Tasks" list (its real id is resolved once via the `@default` alias in `get_default_tasklist_id`, since `tasklists().list()` never returns that alias itself). Going the other direction, an unlinked task's list name is resolved to/created as a Notion `Course` page via `ensure_course` (cached in `_course_pages_cache`), using the target database id found through the `Course` relation property's schema (`get_courses_database_id`).

### Deletions

`DELETE_SYNC` (currently `False`) is a single module-level flag controlling whether deleting one side archives/deletes the other. When `False`, missing counterparts are recreated instead of propagating the deletion — this is the default specifically to avoid destructive surprises before the sync behavior is trusted.

### CI push-back mechanism

The workflow commits `sync_state.json` after every run, then rebases and retries the push (up to 5 times) since concurrent runs or human pushes may have moved `main` mid-sync. This is why the workflow checks out full history (`fetch-depth: 0`) and configures a `sync-bot` git identity inline.

## Gotchas when editing

- Property name changes in Notion require matching `PROP_*` constant updates — there's no schema validation, so a mismatch fails silently (property lookups return `None`/empty).
- Only the date portion of `Due date` is synced; all Calendar events are all-day (no timed events).
- `client_secret.json` and `token.json` are gitignored and must never be committed — they're the OAuth client secret and user token respectively.
