# Notion ↔ Google Tasks + Calendar Sync

Keeps your **Tasks Tracker** database in sync with **Google Tasks** and
**Google Calendar**, routed by the `Sync As` property. Runs free on GitHub
Actions every 15 minutes.

| `Sync As` | Destination | What syncs |
|-----------|-------------|------------|
| `Task` or empty | Google Tasks | Title, due date (optional), Status ↔ completion. Course → task list |
| `Event` | Google Calendar | Title + due date as an all-day event (both directions) |

## 1. Notion setup

1. Go to https://www.notion.so/my-integrations → **New integration**.
   - Give it a name (e.g. "Google Sync"), select your workspace.
   - Copy the **Internal Integration Token** — this is `NOTION_TOKEN`.
2. Open your **Tasks Tracker** database in Notion → **···** menu (top right)
   → **Connections** → add the integration you just created.
3. Ensure these properties exist (names must match exactly):
   - **Text:** `Google Event ID`, `Google Task ID` (script-managed — leave blank)
   - **Select:** `Sync As` with options `Task` and `Event`
   - **Status:** `Status` with at least `Not started`, `In progress`, `Done`
   - **Relation:** `Course` (to your Courses database)
   - **Title / Date:** `Task name`, `Due date`
4. Get your database ID: open the database as a full page, copy the URL —
   the 32-character string right after your workspace name and before the
   `?v=` is your `NOTION_DATABASE_ID`.

## 2. Google setup (Calendar + Tasks)

1. Go to https://console.cloud.google.com/ → create a project.
2. **APIs & Services → Library** → enable both:
   - **Google Calendar API**
   - **Google Tasks API**
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
   This opens a browser, asks you to log in (Calendar + Tasks scopes), and
   creates `token.json`. If you already had a token from Calendar-only auth,
   re-run this so Tasks permission is included.
5. Decide which calendar to use for **Event** rows. For your primary calendar,
   use `GOOGLE_CALENDAR_ID=primary`. To use a separate calendar (recommended),
   create one in Google Calendar, open its settings, and copy its **Calendar ID**.

### How Tasks routing works

- Each Notion task’s first **Course** relation becomes a **Google Task list**
  (created if it doesn’t exist). No course → list named `Inbox`.
- Notion `Status = Done` ↔ Google Task `completed`; anything else ↔
  `needsAction`. Completing a task in Google Tasks (or the Calendar sidebar)
  sets Notion to `Done`, and vice versa.
- Unlinked Google Tasks are **not** imported into Notion (Tasks API has no
  place to store a Notion page id).

### How Event routing works

- Event rows with a due date become all-day Calendar events; the event id is
  stored in `Google Event ID`.
- Events this script created (tagged with a private `notion_page_id`) can
  recreate a Notion row if the Notion page is deleted (`Sync As = Event`).

## 3. GitHub setup

1. Create a new **private** GitHub repo, push these files to it.
2. Repo → **Settings → Secrets and variables → Actions → New repository
   secret**. Add these four secrets:
   - `NOTION_TOKEN` — from step 1
   - `NOTION_DATABASE_ID` — from step 1
   - `GOOGLE_CALENDAR_ID` — from step 2
   - `GOOGLE_TOKEN_JSON` — paste the full contents of `token.json`
3. That’s it. The workflow in `.github/workflows/sync.yml` runs every 15
   minutes automatically. You can also trigger it manually from the repo’s
   **Actions** tab → "Notion <-> Google Tasks + Calendar Sync" → **Run workflow**.

## How conflicts are resolved

Each sync run compares Notion’s `last_edited_time` against Google’s
`updated` timestamp for every linked pair, since the last successful sync:

- Only Notion changed → Google is updated to match.
- Only Google changed → Notion is updated to match.
- Both changed → **Notion wins** (edit `sync.py` if you’d rather Google win).

If you change `Sync As` between Task and Event, the script creates a link on
the new side and does **not** delete the old Google object unless
`DELETE_SYNC` is enabled.

## Deletions

By default, deleting a task or event does **not** delete its counterpart
(`DELETE_SYNC = False` in `sync.py`) — this avoids accidentally wiping data
out while you’re still trusting the script. Flip it to `True` once you’re
confident it’s working the way you want.

## Limitations

- Event path only syncs `Task name` and `Due date` (all-day); no timed events.
- Task path does not sync Priority, Effort level, or subtasks.
- Recurring items aren’t handled specially.
- Existing all-day Calendar events that were really tasks are not migrated
  automatically — set `Sync As = Task` on those rows (old `Google Event ID`
  can be cleared manually if desired).
- If you rename Notion properties, update the `PROP_*` constants in `sync.py`.
