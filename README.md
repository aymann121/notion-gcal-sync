# Notion ↔ Google Calendar Two-Way Sync

Keeps your **Tasks Tracker** database's `Task name` / `Due date` in sync with
a Google Calendar, in both directions. Runs free on GitHub Actions every 15
minutes.

## 1. Notion setup

1. Go to https://www.notion.so/my-integrations → **New integration**.
   - Give it a name (e.g. "Calendar Sync"), select your workspace.
   - Copy the **Internal Integration Token** — this is `NOTION_TOKEN`.
2. Open your **Tasks Tracker** database in Notion → **···** menu (top right)
   → **Connections** → add the integration you just created.
3. Add a new property to the Tasks Tracker: type **Text**, named exactly
   `Google Event ID`. (The script writes to this — don't fill it in
   yourself.)
4. Get your database ID: open the database as a full page, copy the URL —
   the 32-character string right after your workspace name and before the
   `?v=` is your `NOTION_DATABASE_ID`.

## 2. Google Calendar setup

1. Go to https://console.cloud.google.com/ → create a project.
2. **APIs & Services → Library** → search "Google Calendar API" → **Enable**.
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - If prompted, configure the consent screen first (choose "External",
     add yourself as a test user — this is fine for personal use).
   - Application type: **Desktop app**.
   - Download the JSON → save it as `client_secret.json` in this folder.
4. On your own computer (not GitHub Actions), run:
   ```
   pip install -r requirements.txt
   python get_google_token.py
   ```
   This opens a browser, asks you to log in, and creates `token.json`.
5. Decide which calendar to sync to. For your primary calendar, use
   `GOOGLE_CALENDAR_ID=primary`. To use a separate calendar (recommended, so
   it's easy to tell synced events apart), create one in Google Calendar,
   open its settings, and copy its **Calendar ID**.

## 3. GitHub setup

1. Create a new **private** GitHub repo, push these files to it.
2. Repo → **Settings → Secrets and variables → Actions → New repository
   secret**. Add these four secrets:
   - `NOTION_TOKEN` — from step 1
   - `NOTION_DATABASE_ID` — from step 1
   - `GOOGLE_CALENDAR_ID` — from step 2
   - `GOOGLE_TOKEN_JSON` — paste the full contents of `token.json`
3. That's it. The workflow in `.github/workflows/sync.yml` runs every 15
   minutes automatically. You can also trigger it manually from the repo's
   **Actions** tab → "Notion <-> Google Calendar Sync" → **Run workflow**.

## How conflicts are resolved

Each sync run compares Notion's `last_edited_time` against Google's
`updated` timestamp for every linked task/event pair, since the last
successful sync:

- Only Notion changed → Google is updated to match.
- Only Google changed → Notion is updated to match.
- Both changed → **Notion wins** (edit `sync.py`'s `sync()` function if you'd
  rather Google win in that case).

## Deletions

By default, deleting a task or event does **not** delete its counterpart
(`DELETE_SYNC = False` in `sync.py`) — this avoids accidentally wiping data
out while you're still trusting the script. Flip it to `True` once you're
confident it's working the way you want.

## Limitations of this v1

- Only syncs `Task name` and `Due date` (all-day events). Doesn't sync
  Status, Course, or specific times — extend `sync.py` if you want those.
- Uses a single Notion `Due date` field, so recurring tasks aren't handled
  specially.
- If you rename the `Task name` / `Due date` properties in Notion, update
  the `PROP_*` constants at the top of `sync.py` to match.
