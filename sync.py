#!/usr/bin/env python3
"""
Two-way sync between a Notion database (Tasks Tracker) and a Google Calendar.

How it works
------------
- Each Notion task with a Due date gets a matching Google Calendar event.
  The event's ID is stored back on the Notion page (in a "Google Event ID"
  rich_text property) so we always know which event belongs to which task.
- Each sync run compares "last_edited_time" (Notion) vs "updated" (Google)
  for every linked pair, and whichever side changed more recently since the
  last successful sync wins and overwrites the other side.
- New tasks (no Google Event ID yet) get a new Google event created.
- New Google events tagged by this script but missing from Notion get a new
  Notion page created.
- Deletions are NOT auto-propagated by default (safer). See DELETE_SYNC below.

Setup required before running (see README.md for full steps):
  1. A Notion internal integration token, with the Tasks Tracker database shared with it.
  2. A Google Cloud OAuth client (Desktop app type) + a one-time browser login
     to produce token.json (see get_google_token.py, run once locally).
  3. Environment variables / GitHub Actions secrets:
       NOTION_TOKEN
       NOTION_DATABASE_ID
       GOOGLE_CALENDAR_ID      (e.g. "primary" or a specific calendar's ID)
       GOOGLE_TOKEN_JSON       (the full contents of token.json, as a string)
       GOOGLE_CLIENT_SECRET_JSON  (the full contents of your OAuth client json)
"""

import os
import json
import datetime
from notion_client import Client as NotionClient
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ---- Config ---------------------------------------------------------------

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# Notion property names -- change these if your database uses different labels
PROP_TITLE = "Task name"
PROP_DUE_DATE = "Due date"
PROP_GCAL_EVENT_ID = "Google Event ID"  # a rich_text property you add to Notion

# Set to True if you also want deleting a task/event to delete its counterpart.
# Off by default to avoid accidental data loss while you trust the sync.
DELETE_SYNC = False

STATE_FILE = "sync_state.json"  # tracks last successful sync time per pair

# ---- Clients ----------------------------------------------------------------

notion = NotionClient(auth=NOTION_TOKEN)


def get_google_service():
    creds = Credentials.from_authorized_user_info(
        json.loads(os.environ["GOOGLE_TOKEN_JSON"])
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


gcal = get_google_service()

# ---- State (tracks last sync time so we know which side changed) ----------


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---- Notion helpers ---------------------------------------------------------


def get_notion_tasks():
    tasks = []
    cursor = None
    while True:
        resp = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            start_cursor=cursor,
            filter={"property": PROP_DUE_DATE, "date": {"is_not_empty": True}},
        )
        tasks.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return tasks


def notion_title(page):
    return "".join(
        t["plain_text"] for t in page["properties"][PROP_TITLE]["title"]
    ) or "(untitled)"


def notion_due_date(page):
    d = page["properties"][PROP_DUE_DATE]["date"]
    return d["start"] if d else None


def notion_gcal_id(page):
    rt = page["properties"].get(PROP_GCAL_EVENT_ID, {}).get("rich_text", [])
    return rt[0]["plain_text"] if rt else None


def set_notion_gcal_id(page_id, event_id):
    notion.pages.update(
        page_id=page_id,
        properties={
            PROP_GCAL_EVENT_ID: {
                "rich_text": [{"text": {"content": event_id}}]
            }
        },
    )


def set_notion_due_date(page_id, date_str):
    notion.pages.update(
        page_id=page_id, properties={PROP_DUE_DATE: {"date": {"start": date_str}}}
    )


def set_notion_title(page_id, title):
    notion.pages.update(
        page_id=page_id,
        properties={PROP_TITLE: {"title": [{"text": {"content": title}}]}},
    )


def create_notion_task(title, date_str, event_id):
    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            PROP_TITLE: {"title": [{"text": {"content": title}}]},
            PROP_DUE_DATE: {"date": {"start": date_str}},
            PROP_GCAL_EVENT_ID: {"rich_text": [{"text": {"content": event_id}}]},
        },
    )
    return page


# ---- Google Calendar helpers ------------------------------------------------


def get_gcal_event(event_id):
    try:
        return gcal.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
    except Exception:
        return None


def create_gcal_event(title, date_str, notion_page_id):
    event = {
        "summary": title,
        "start": {"date": date_str[:10]},
        "end": {"date": date_str[:10]},
        "extendedProperties": {"private": {"notion_page_id": notion_page_id}},
    }
    return gcal.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()


def update_gcal_event(event_id, title=None, date_str=None):
    body = {}
    if title is not None:
        body["summary"] = title
    if date_str is not None:
        body["start"] = {"date": date_str[:10]}
        body["end"] = {"date": date_str[:10]}
    return gcal.events().patch(
        calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=body
    ).execute()


def list_gcal_events_from_notion():
    """List events this script created (tagged with notion_page_id)."""
    events = []
    page_token = None
    while True:
        resp = gcal.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            privateExtendedProperty="notion_page_id=*",
            pageToken=page_token,
            showDeleted=False,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


# ---- Core sync logic ---------------------------------------------------------


def parse_dt(s):
    """Parse an ISO timestamp into a timezone-aware UTC datetime."""
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sync():
    state = load_state()
    notion_tasks = get_notion_tasks()
    gcal_events = {e["id"]: e for e in list_gcal_events_from_notion()}

    seen_event_ids = set()

    # 1. Walk Notion tasks: create/update/skip against Google
    for page in notion_tasks:
        page_id = page["id"]
        title = notion_title(page)
        due = notion_due_date(page)
        event_id = notion_gcal_id(page)
        notion_edited = parse_dt(page["last_edited_time"])

        if not event_id:
            # brand-new task -> create a Google event
            event = create_gcal_event(title, due, page_id)
            set_notion_gcal_id(page_id, event["id"])
            state[page_id] = {"last_sync": utc_now_iso()}
            continue

        seen_event_ids.add(event_id)
        event = gcal_events.get(event_id) or get_gcal_event(event_id)
        if event is None:
            # event was deleted on Google's side
            if DELETE_SYNC:
                notion.pages.update(page_id=page_id, archived=True)
            else:
                # recreate it so nothing is lost
                new_event = create_gcal_event(title, due, page_id)
                set_notion_gcal_id(page_id, new_event["id"])
            continue

        google_updated = parse_dt(event["updated"])
        last_sync = state.get(page_id, {}).get("last_sync")
        last_sync_dt = parse_dt(last_sync) if last_sync else datetime.datetime.min.replace(
            tzinfo=datetime.timezone.utc
        )

        notion_changed = notion_edited > last_sync_dt
        google_changed = google_updated > last_sync_dt

        if notion_changed and not google_changed:
            update_gcal_event(event_id, title=title, date_str=due)
        elif google_changed and not notion_changed:
            g_title = event.get("summary", title)
            g_date = event["start"].get("date") or event["start"].get("dateTime", due)[:10]
            set_notion_title(page_id, g_title)
            set_notion_due_date(page_id, g_date)
        elif notion_changed and google_changed:
            # both changed since last sync -> Notion wins (adjust if you'd rather Google win)
            update_gcal_event(event_id, title=title, date_str=due)

        state[page_id] = {"last_sync": utc_now_iso()}

    # 2. Any tagged Google events with no matching Notion page? (Notion task deleted)
    for event_id, event in gcal_events.items():
        if event_id in seen_event_ids:
            continue
        notion_page_id = event.get("extendedProperties", {}).get("private", {}).get(
            "notion_page_id"
        )
        try:
            page = notion.pages.retrieve(page_id=notion_page_id) if notion_page_id else None
        except Exception:
            page = None

        if page is None or page.get("archived"):
            if DELETE_SYNC:
                gcal.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
            # else: leave the orphaned event alone

    save_state(state)


if __name__ == "__main__":
    sync()
