#!/usr/bin/env python3
"""
Two-way sync: Notion Tasks Tracker ↔ Google Tasks + Google Calendar.

Routing via Notion "Sync As":
  Task (or empty) → Google Tasks  (Course → list; Status ↔ completion)
  Event           → Google Calendar (all-day; title + due)

Conflict rule: compare last_edited_time / updated vs last_sync;
whichever side changed more recently wins. Both changed → Notion wins.

Linked IDs live on the Notion page (Google Task ID / Google Event ID).
Deletions are mirrored both ways while DELETE_SYNC is True. See README.md.
"""

import os
import json
import datetime
from notion_client import Client as NotionClient
from notion_client import APIErrorCode, APIResponseError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ==== Config =================================================================
# Settings read from the environment, plus the exact Notion column and
# option names this script depends on. Edit here if Notion is renamed.

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# Must match property names in the Notion database exactly.
PROP_TITLE = "Task name"
PROP_DUE_DATE = "Due date"
PROP_STATUS = "Status"
PROP_COURSE = "Course"
PROP_SYNC_AS = "Sync As"
PROP_GCAL_EVENT_ID = "Google Event ID"
PROP_GTASK_ID = "Google Task ID"

SYNC_AS_TASK = "Task"
SYNC_AS_EVENT = "Event"
STATUS_DONE = "Done"
STATUS_NOT_STARTED = "Not started"
# Written by notion-task-radar when a Radar task ends its day unfinished.
# Terminal like Done, so it completes the Google Task -- but it must never be
# overwritten *by* a completed Google Task (see resolve_notion_status).
STATUS_ARCHIVED = "Archived"

# Deleting on one side archives/deletes the other: a deleted Notion row deletes
# its Google Task/event, and a deleted Google Task archives its Notion page.
# Because "missing" now means "delete the counterpart", every lookup that can
# report a thing as missing must be sure it really is (see is_not_found).
DELETE_SYNC = True

# Per-page last_sync (+ task/event ids) so we know which side changed.
STATE_FILE = "sync_state.json"


# ==== API clients ============================================================
# One client each for Notion, Google Calendar and Google Tasks, created
# once at import. Every function below talks to the outside world through these.

notion = NotionClient(auth=NOTION_TOKEN)


def get_google_credentials():
    """Load OAuth token from env; refresh if expired."""
    creds = Credentials.from_authorized_user_info(
        json.loads(os.environ["GOOGLE_TOKEN_JSON"])
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


_creds = get_google_credentials()
gcal = build("calendar", "v3", credentials=_creds)
gtasks = build("tasks", "v1", credentials=_creds)


# ==== General utilities ======================================================
# Small helpers with no knowledge of Notion or Google data: error
# classification and timestamp parsing/formatting.

def is_not_found(exc):
    """True only for a genuine 404/410 from Notion or Google.

    Every "has this been deleted?" lookup goes through here. With DELETE_SYNC on,
    a swallowed error is a deletion order, so a rate-limit or a 500 must raise and
    fail the run rather than look like a missing object.
    """
    if isinstance(exc, HttpError):
        return exc.resp.status in (404, 410)
    if isinstance(exc, APIResponseError):
        return exc.code == APIErrorCode.ObjectNotFound
    return False


def parse_dt(s):
    """ISO timestamp → timezone-aware UTC datetime."""
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def google_list_all(list_method, **params):
    """Every item from a Google API list call, following nextPageToken across pages.

    e.g. google_list_all(gtasks.tasklists().list, maxResults=100)
    """
    items = []
    page_token = None
    while True:
        resp = list_method(pageToken=page_token, **params).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


# ==== State file (sync_state.json) ===========================================
# The only memory between runs: {notion_page_id: {last_sync, kind, ids...}}.
# Comparing each side's edit time against last_sync tells us what changed.

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        # json.dump will write the state to the file in a pretty format
        # indent = 2 means 2 spaces per level of nesting


def last_sync_dt(state, page_id):
    """When we last successfully synced this page (or epoch if never)."""
    last_sync = state.get(page_id, {}).get("last_sync")
    if last_sync:
        return parse_dt(last_sync)
    return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def event_state_entry(event_id):
    """State-file record for a Notion row linked to a Calendar event."""
    return {"last_sync": utc_now_iso(), "kind": "event", "event_id": event_id}


def task_state_entry(task_id, tasklist_id):
    """State-file record for a Notion row linked to a Google Task."""
    return {
        "last_sync": utc_now_iso(),
        "kind": "task",
        "task_id": task_id,
        "tasklist_id": tasklist_id,
    }


# ==== Notion: reading rows ===================================================
# Notion basics: a "database" is a table and each row is a "page" (a dict).
# A row's column values live in page["properties"], keyed by column name
# (the PROP_* constants). Text is stored as "rich text": a list of chunks,
# each a run of same-formatted text whose raw characters are in "plain_text".
# A "relation" column links a row to rows in another database (like a foreign key).
#
# These functions fetch rows and pull plain Python values out of them.

def query_all(database_id):
    """Return every row of a database, fetched 100 at a time."""
    pages = []
    cursor = None
    # Query args; start_cursor is added after the first page (max 100 results per call).
    kwargs = {"database_id": database_id}
    while True:
        if cursor:
            kwargs["start_cursor"] = cursor
        # resp: {"results": [page, ...], "has_more": bool, "next_cursor": str | None}
        resp = notion.databases.query(**kwargs)
        pages.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return pages


def get_notion_pages():
    """Return every row of the Tasks Tracker database."""
    return query_all(NOTION_DATABASE_ID)


def get_notion_page(page_id):
    """One row by id, or None if it no longer exists (other errors raise)."""
    try:
        return notion.pages.retrieve(page_id=page_id)
    except Exception as exc:
        if not is_not_found(exc):
            raise
        return None


def plain_text(chunks):
    """Join a list of rich-text chunks into one plain string."""
    return "".join(t["plain_text"] for t in chunks)


def find_title_prop_name(props):
    """Name of the title column among a row's or database's properties, or None.

    Every database has exactly one; matching by type means we don't need to
    know what it's called (used for the Courses database).
    """
    for name, prop in props.items():
        if prop.get("type") == "title":
            return name
    return None


def rich_text_plain(page, prop_name):
    """Text of a text column (first chunk only; fine for the IDs stored there), or None."""
    rt = page["properties"].get(prop_name, {}).get("rich_text", [])
    return rt[0]["plain_text"] if rt else None


def notion_title(page):
    """Row's title as plain text (all chunks joined), or "(untitled)"."""
    return plain_text(page["properties"][PROP_TITLE]["title"]) or "(untitled)"


def notion_due_date(page):
    """Row's due date as "YYYY-MM-DD" (any time is dropped), or None."""
    d = page["properties"].get(PROP_DUE_DATE, {}).get("date")
    if not d:
        return None
    start = d["start"]
    return start[:10] if start else None


def notion_status(page):
    """Row's Status option name (e.g. "Done"), or None."""
    status = page["properties"].get(PROP_STATUS, {}).get("status")
    return status["name"] if status else None


def notion_sync_as(page):
    """Row's Sync As dropdown value ("Task"/"Event"), or None if blank."""
    sel = page["properties"].get(PROP_SYNC_AS, {}).get("select")
    return sel["name"] if sel else None


def notion_gcal_id(page):
    """Id of the linked Google Calendar event, or None if not linked yet."""
    return rich_text_plain(page, PROP_GCAL_EVENT_ID)


def notion_gtask_id(page):
    """Id of the linked Google Task, or None if not linked yet."""
    return rich_text_plain(page, PROP_GTASK_ID)


def notion_course_page_ids(page):
    """Page ids of the Course rows this row links to (may be empty)."""
    rel = page["properties"].get(PROP_COURSE, {}).get("relation", [])
    return [r["id"] for r in rel]


def is_task_row(page):
    """True if this row syncs to Google Tasks (Sync As is "Task" or blank)."""
    sync_as = notion_sync_as(page)
    return sync_as is None or sync_as == SYNC_AS_TASK


def is_event_row(page):
    """True if this row syncs to Google Calendar (Sync As is "Event")."""
    return notion_sync_as(page) == SYNC_AS_EVENT


# ==== Notion: writing rows ===================================================
# Build the value dicts Notion expects, then create or update rows with them.

# Builders for the value dicts Notion expects when writing a column.
def title_value(text):
    return {"title": [{"text": {"content": text}}]}


def text_value(text):
    return {"rich_text": [{"text": {"content": text}}]}


def date_value(date_str):
    """Date only; None (or empty) clears the date."""
    return {"date": {"start": date_str[:10]} if date_str else None}


def status_value(name):
    return {"status": {"name": name}}


def select_value(name):
    return {"select": {"name": name}}


def update_notion_page(page_id, properties):
    """Write several columns of one row in a single API call."""
    notion.pages.update(page_id=page_id, properties=properties)


def set_notion_gcal_id(page_id, event_id):
    """Link a row to its Google Calendar event by storing the event id."""
    update_notion_page(page_id, {PROP_GCAL_EVENT_ID: text_value(event_id)})


def set_notion_gtask_id(page_id, task_id):
    """Link a row to its Google Task by storing the task id."""
    update_notion_page(page_id, {PROP_GTASK_ID: text_value(task_id)})


def create_notion_event(title, date_str, event_id):
    """Add a Tasks Tracker row for a Google Calendar event, linked by its id."""
    return notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            PROP_TITLE: title_value(title),
            PROP_DUE_DATE: date_value(date_str),
            PROP_GCAL_EVENT_ID: text_value(event_id),
            PROP_SYNC_AS: select_value(SYNC_AS_EVENT),
        },
    )


def create_notion_task(title, due, task_id, status_name, course_page_id=None):
    """Add a Tasks Tracker row for a Google Task, linked by its id (optionally to a Course)."""
    properties = {
        PROP_TITLE: title_value(title),
        PROP_STATUS: status_value(status_name),
        PROP_GTASK_ID: text_value(task_id),
        PROP_SYNC_AS: select_value(SYNC_AS_TASK),
    }
    if due:
        properties[PROP_DUE_DATE] = date_value(due)
    if course_page_id:
        properties[PROP_COURSE] = {"relation": [{"id": course_page_id}]}
    return notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)


# ==== Notion: Courses database ===============================================
# Tasks link to rows in a separate Courses database via the Course column.
# Its id and title column are discovered at runtime, then cached per run,
# so course names can be looked up (id → name) or found/created (name → id).

# Notion's 2025 multi-source-database split moved property schemas off the
# database object onto a separate "data source" object; the pinned
# notion-client SDK (2.2.1) predates that split and has no data_sources
# endpoint. GET/query/create for pages still work fine on the old default
# version everywhere else in this script — only schema lookups need this.
NOTION_LATEST_VERSION = "2026-03-11"


def notion_get(path, notion_version):
    """Raw GET to the Notion API at `path`, using the given API version."""
    response = notion.client.get(path, headers={"Notion-Version": notion_version})
    response.raise_for_status()
    return response.json()


def get_data_source_properties(database_id):
    """
    A database's column definitions: {column name: {"type": ..., ...}}.
    This returns the property/column definitions for the database and their data types.
    """
    db = notion_get(f"databases/{database_id}", NOTION_LATEST_VERSION)
    data_source_id = db["data_sources"][0]["id"]
    data_source = notion_get(f"data_sources/{data_source_id}", NOTION_LATEST_VERSION)
    return data_source["properties"]


def find_course_relation_property(props):
    """Find the Course column's definition among a database's column definitions.

    Tries the column named PROP_COURSE first. If that fails (e.g. the column
    was renamed), falls back to the only relation-type column, since this
    database has just one. Raises if neither works.
    """
    # 1. By name.
    if PROP_COURSE in props and props[PROP_COURSE].get("type") == "relation":
        return props[PROP_COURSE]
    # 2. By type: if exactly one column is a relation, it must be Course.
    relations = {name: p for name, p in props.items() if p.get("type") == "relation"}
    if len(relations) == 1:
        return next(iter(relations.values()))
    # 3. Ambiguous or missing: fail, listing every column to help debugging.
    raise RuntimeError(
        f"Could not find the '{PROP_COURSE}' relation property. "
        f"Available properties: {[(n, p.get('type')) for n, p in props.items()]}"
    )


_courses_database_id = None


def get_courses_database_id():
    """Id of the Courses database that the Course column links to (cached)."""
    global _courses_database_id
    if _courses_database_id is None:
        props = get_data_source_properties(NOTION_DATABASE_ID)
        relation = find_course_relation_property(props)
        _courses_database_id = relation["relation"]["database_id"]
    return _courses_database_id


_course_title_cache = {}


def course_title(page_id):
    """Title of a Course row, given its page id (cached per run)."""
    if page_id in _course_title_cache: # check if course title is already in cache
        return _course_title_cache[page_id]
    props = notion.pages.retrieve(page_id=page_id)["properties"]
    title_name = find_title_prop_name(props)
    name = plain_text(props[title_name]["title"]) if title_name else ""
    name = name or "(untitled course)"
    _course_title_cache[page_id] = name
    return name


_course_pages_cache = None  # title → page_id
_course_title_prop_name = None


def refresh_course_cache():
    """Load every Course row into a {title: page id} cache; also record the title column's name."""
    global _course_pages_cache, _course_title_prop_name
    courses_db_id = get_courses_database_id()
    _course_title_prop_name = find_title_prop_name(
        get_data_source_properties(courses_db_id)
    )

    # Ends up as {course title: Course row's page id}, e.g.
    #   {"CS 101": "1a2b3c...", "Calculus II": "4d5e6f..."}
    _course_pages_cache = {}
    for page in query_all(courses_db_id):
        title_prop = page["properties"].get(_course_title_prop_name, {})
        name = plain_text(title_prop.get("title", []))
        if name:
            _course_pages_cache[name] = page["id"]
    return _course_pages_cache


def ensure_course(name):
    """Page id of the Course row with this title, creating the row if needed."""
    if _course_pages_cache is None:
        refresh_course_cache()
    if name in _course_pages_cache:
        return _course_pages_cache[name]
    created = notion.pages.create(
        parent={"database_id": get_courses_database_id()},
        properties={_course_title_prop_name: title_value(name)},
    )
    _course_pages_cache[name] = created["id"]
    return created["id"]


# ==== Google Calendar ========================================================
# Thin wrappers over the Calendar API. Every event is all-day and tagged
# with the id of the Notion row it came from.

def get_gcal_event(event_id):
    try:
        return gcal.events().get(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()
    except Exception as exc:
        if not is_not_found(exc):
            raise
        return None


def all_day(date_str):
    """start/end fields for an all-day event on this date."""
    return {"start": {"date": date_str[:10]}, "end": {"date": date_str[:10]}}


def create_gcal_event(title, date_str, notion_page_id):
    """All-day event tagged with notion_page_id for reverse lookup."""
    event = {
        "summary": title,
        **all_day(date_str),
        "extendedProperties": {"private": {"notion_page_id": notion_page_id}},
    }
    return gcal.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()


def update_gcal_event(event_id, title=None, date_str=None):
    body = {}
    if title is not None:
        body["summary"] = title
    if date_str is not None:
        body.update(all_day(date_str))
    return gcal.events().patch(
        calendarId=GOOGLE_CALENDAR_ID, eventId=event_id, body=body
    ).execute()


def list_gcal_events_from_notion():
    """Events previously created by this script (private notion_page_id tag)."""
    return google_list_all(
        gcal.events().list,
        calendarId=GOOGLE_CALENDAR_ID,
        privateExtendedProperty="notion_page_id=*",
        showDeleted=False,
    )


def gcal_event_date(event):
    """An event's start as "YYYY-MM-DD" (all-day or timed), or None."""
    start = event["start"]
    return start.get("date") or (start.get("dateTime") or "")[:10] or None


# ==== Google Tasks ===========================================================
# Thin wrappers over the Tasks API: task lists (one per course, found or
# created by name) and the tasks inside them.

_tasklist_cache = None  # title → list id
_tasklist_titles = None  # list id → title
_default_tasklist_id = None  # Google's "My Tasks" list id (resolved, not the alias)


def get_default_tasklist_id():
    """Real id of Google's default "My Tasks" list (the "@default" alias resolves to it)."""
    global _default_tasklist_id
    if _default_tasklist_id is None:
        _default_tasklist_id = gtasks.tasklists().get(tasklist="@default").execute()["id"]
    return _default_tasklist_id


def refresh_tasklist_cache():
    """Load all Google Task lists into _tasklist_cache / _tasklist_titles."""
    global _tasklist_cache, _tasklist_titles
    _tasklist_cache = {}
    _tasklist_titles = {}
    for tl in google_list_all(gtasks.tasklists().list, maxResults=100):
        _tasklist_cache[tl["title"]] = tl["id"]
        _tasklist_titles[tl["id"]] = tl["title"]
    return _tasklist_cache


def get_tasklist_cache():
    """{list title: list id}, loading it on first use."""
    if _tasklist_cache is None:
        refresh_tasklist_cache()
    return _tasklist_cache


def ensure_tasklist(name):
    """Return list id for name; create the list if missing."""
    cache = get_tasklist_cache()
    if name in cache:
        return cache[name]
    created = gtasks.tasklists().insert(body={"title": name}).execute()
    cache[name] = created["id"]
    return created["id"]


def due_to_gtasks(date_str):
    """Tasks API wants RFC3339; only the date part is kept (midnight UTC)."""
    if not date_str:
        return None
    return f"{date_str[:10]}T00:00:00.000Z"


def due_from_gtasks(task):
    due = task.get("due")
    return due[:10] if due else None


def get_gtask(tasklist_id, task_id):
    try:
        return gtasks.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    except Exception as exc:
        if not is_not_found(exc):
            raise
        return None


def find_gtask(task_id, preferred_list_id=None):
    """Find a task by id (try known list first, then scan all lists)."""
    if preferred_list_id:
        task = get_gtask(preferred_list_id, task_id)
        if task is not None:
            return preferred_list_id, task
    for list_id in get_tasklist_cache().values():
        if list_id == preferred_list_id:
            continue
        task = get_gtask(list_id, task_id)
        if task is not None:
            return list_id, task
    return None, None


def create_gtask(tasklist_id, title, due=None, status="needsAction"):
    body = {"title": title, "status": status}
    encoded = due_to_gtasks(due)
    if encoded:
        body["due"] = encoded
    return gtasks.tasks().insert(tasklist=tasklist_id, body=body).execute()


def update_gtask(tasklist_id, task_id, title=None, due=None, status=None, clear_due=False):
    body = {}
    if title is not None:
        body["title"] = title
    if status is not None:
        body["status"] = status
    if clear_due:
        body["due"] = None
    elif due is not None:
        body["due"] = due_to_gtasks(due)
    if not body:
        return None
    return gtasks.tasks().patch(
        tasklist=tasklist_id, task=task_id, body=body
    ).execute()


def move_gtask(task_id, from_list_id, to_list_id):
    """Move task when Notion Course (list) changes."""
    gtasks.tasks().move(
        tasklist=from_list_id,
        task=task_id,
        destinationTasklist=to_list_id,
    ).execute()


def list_all_gtasks():
    """task_id → (list_id, task), including completed/hidden."""
    result = {}
    for list_id in get_tasklist_cache().values():
        tasks = google_list_all(
            gtasks.tasks().list,
            tasklist=list_id,
            showCompleted=True,
            showHidden=True,
            maxResults=100,
        )
        for task in tasks:
            result[task["id"]] = (list_id, task)
    return result


# ==== Status mapping (Notion Status ↔ Google Tasks completion) ===============
# Notion has several statuses; a Google Task is only completed or not.

def status_to_gtasks(status_name):
    """Map a Notion Status to Google's task state: Done/Archived → "completed", else "needsAction"."""
    return (
        "completed"
        if status_name in (STATUS_DONE, STATUS_ARCHIVED)
        else "needsAction"
    )


def gtasks_to_status(gtasks_status):
    """Map Google's task state to a Notion Status: "completed" → Done, else Not started."""
    return STATUS_DONE if gtasks_status == "completed" else STATUS_NOT_STARTED


def resolve_notion_status(current_status, gtasks_status):
    """New Notion Status for a task after it was changed in Google Tasks.

    Normally: completed → Done, not completed → Not started.

    Exception: if the Notion task is Archived (missed) and the Google task is
    completed, keep it Archived. The sync itself marks archived tasks as
    completed in Google, so without this, the next run would see that change
    and wrongly turn every missed task into Done.
    """
    if gtasks_status == "completed" and current_status == STATUS_ARCHIVED:
        return STATUS_ARCHIVED
    return gtasks_to_status(gtasks_status)


# ==== Core sync: Events (Notion ↔ Google Calendar) ===========================
# Rows with Sync As = Event. Built from the Notion and Calendar helpers above;
# sync_event_pages at the bottom is the entry point for this half.

def create_gcal_event_for_page(state, page_id, title, due):
    """New Notion event → create its Calendar event and link the two."""
    event = create_gcal_event(title, due, page_id)
    set_notion_gcal_id(page_id, event["id"])
    state[page_id] = event_state_entry(event["id"])


def sync_linked_event_page(state, page, event_id, gcal_events):
    """Sync one Notion row that is already linked to a Calendar event."""
    page_id = page["id"]
    title = notion_title(page)
    due = notion_due_date(page)
    notion_edited = parse_dt(page["last_edited_time"])
    event = gcal_events.get(event_id) or get_gcal_event(event_id)

    # Google event missing: archive Notion or recreate the event
    if event is None:
        if DELETE_SYNC:
            notion.pages.update(page_id=page_id, archived=True)
            state.pop(page_id, None)
        else:
            create_gcal_event_for_page(state, page_id, title, due)
        return

    # Decide which side wins since last_sync
    google_updated = parse_dt(event["updated"])
    last = last_sync_dt(state, page_id)
    notion_changed = notion_edited > last
    google_changed = google_updated > last

    if notion_changed:
        # Notion-only change, or both changed (Notion wins)
        update_gcal_event(event_id, title=title, date_str=due)
    elif google_changed:
        update_notion_page(page_id, {
            PROP_TITLE: title_value(event.get("summary", title)),
            PROP_DUE_DATE: date_value(gcal_event_date(event) or due),
        })

    state[page_id] = event_state_entry(event_id)


def handle_orphan_gcal_event(state, event_id, event):
    """A tagged Calendar event whose Notion row is gone: delete it, or recreate the row."""
    notion_page_id = (
        event.get("extendedProperties", {})
        .get("private", {})
        .get("notion_page_id")
    )
    page = get_notion_page(notion_page_id) if notion_page_id else None
    if page is not None and not page.get("archived"):
        return

    if DELETE_SYNC:
        gcal.events().delete(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()
        return

    # Recreate Notion Event row and retag the Calendar event
    g_date = gcal_event_date(event)
    if not g_date:
        return
    new_page = create_notion_event(event.get("summary", "(untitled)"), g_date, event_id)
    gcal.events().patch(
        calendarId=GOOGLE_CALENDAR_ID,
        eventId=event_id,
        body={
            "extendedProperties": {
                "private": {"notion_page_id": new_page["id"]}
            }
        },
    ).execute()
    state[new_page["id"]] = event_state_entry(event_id)


def sync_event_pages(state, event_pages):
    """Sync As = Event ↔ Google Calendar all-day events."""
    gcal_events = {e["id"]: e for e in list_gcal_events_from_notion()}
    seen_event_ids = set()

    for page in event_pages:
        due = notion_due_date(page)
        if not due:
            continue  # calendar events require a due date
        event_id = notion_gcal_id(page)
        if not event_id:
            create_gcal_event_for_page(state, page["id"], notion_title(page), due)
            continue
        seen_event_ids.add(event_id)
        sync_linked_event_page(state, page, event_id, gcal_events)

    # Tagged Calendar events whose Notion page was deleted
    for event_id, event in gcal_events.items():
        if event_id not in seen_event_ids:
            handle_orphan_gcal_event(state, event_id, event)


# ==== Core sync: Tasks (Notion ↔ Google Tasks) ===============================
# Rows with Sync As = Task (or blank). Same shape as the event sync, plus
# course → task list routing and importing tasks created in Google.
# sync_task_pages at the bottom is the entry point for this half.

def notion_target_tasklist_id(page):
    """Google Tasks list for this row: one named after its first Course, else the default list."""
    ids = notion_course_page_ids(page)
    if not ids:
        return get_default_tasklist_id()
    return ensure_tasklist(course_title(ids[0]))


def create_gtask_for_page(state, page_id, target_list_id, title, due, g_status):
    """New Notion task → create its Google Task and link the two."""
    task = create_gtask(target_list_id, title, due=due, status=g_status)
    if g_status == "completed":
        update_gtask(target_list_id, task["id"], status="completed")
    set_notion_gtask_id(page_id, task["id"])
    state[page_id] = task_state_entry(task["id"], target_list_id)


def sync_linked_task_page(state, page, task_id, all_gtasks):
    """Sync one Notion row that is already linked to a Google Task."""
    page_id = page["id"]
    title = notion_title(page)
    due = notion_due_date(page)
    status_name = notion_status(page)
    g_status = status_to_gtasks(status_name)
    target_list_id = notion_target_tasklist_id(page)
    notion_edited = parse_dt(page["last_edited_time"])

    preferred = state.get(page_id, {}).get("tasklist_id")
    if task_id in all_gtasks:
        list_id, task = all_gtasks[task_id]
    else:
        list_id, task = find_gtask(task_id, preferred)

    # Google task missing: archive Notion or recreate the task
    if task is None:
        if DELETE_SYNC:
            notion.pages.update(page_id=page_id, archived=True)
            state.pop(page_id, None)
        else:
            create_gtask_for_page(state, page_id, target_list_id, title, due, g_status)
        return

    # Course changed → move to the matching task list
    if list_id != target_list_id:
        move_gtask(task_id, list_id, target_list_id)
        list_id = target_list_id
        task = get_gtask(list_id, task_id) or task

    google_updated = parse_dt(task["updated"])
    last = last_sync_dt(state, page_id)
    notion_changed = notion_edited > last
    google_changed = google_updated > last

    if notion_changed:
        # Notion-only change, or both changed (Notion wins).
        # Omitting `due` wouldn't remove it in Google, so a cleared date must be explicit.
        update_gtask(
            list_id, task_id, title=title, due=due, status=g_status,
            clear_due=due is None and bool(task.get("due")),
        )
    elif google_changed:
        update_notion_page(page_id, {
            PROP_TITLE: title_value(task.get("title") or title),
            PROP_DUE_DATE: date_value(due_from_gtasks(task)),
            PROP_STATUS: status_value(
                resolve_notion_status(status_name, task.get("status"))
            ),
        })

    state[page_id] = task_state_entry(task_id, list_id)


def delete_gtasks_for_removed_pages(state, all_gtasks, seen_task_ids):
    """State entries whose Notion page is gone → delete the Google Task too.

    Returns the ids of deleted Google Tasks.
    """
    deleted_task_ids = set()
    for page_id, entry in list(state.items()):
        if not isinstance(entry, dict) or entry.get("kind") != "task":
            continue
        task_id = entry.get("task_id")
        if not task_id or task_id in seen_task_ids:
            continue
        page = get_notion_page(page_id)
        if page is None or page.get("archived"):
            if DELETE_SYNC:
                # The recorded list can be stale (or absent on an old entry); fall
                # back to where the task actually is, or the task survives the
                # delete and gets re-imported into Notion on the next run.
                list_id = entry.get("tasklist_id")
                if task_id in all_gtasks:
                    list_id = all_gtasks[task_id][0]
                if list_id:
                    try:
                        gtasks.tasks().delete(
                            tasklist=list_id, task=task_id
                        ).execute()
                    except Exception as exc:
                        if not is_not_found(exc):
                            raise
                    deleted_task_ids.add(task_id)
            state.pop(page_id, None)
    return deleted_task_ids


def import_unlinked_gtasks(state, all_gtasks, linked_task_ids):
    """Create Notion rows for every Google Task not yet linked to a page.

    Nothing is skipped: completed tasks come in too (as Status = Done), so the
    two sides stay a complete mirror of each other.
    """
    default_list_id = get_default_tasklist_id()

    for task_id, (list_id, task) in all_gtasks.items():
        if task_id in linked_task_ids:
            continue

        course_page_id = None
        if list_id != default_list_id:
            list_title = _tasklist_titles.get(list_id)
            if list_title:
                course_page_id = ensure_course(list_title)

        new_page = create_notion_task(
            task.get("title") or "(untitled)",
            due_from_gtasks(task),
            task_id,
            gtasks_to_status(task.get("status")),
            course_page_id=course_page_id,
        )
        state[new_page["id"]] = task_state_entry(task_id, list_id)


def sync_task_pages(state, task_pages):
    """Sync As = Task (or empty) ↔ Google Tasks."""
    refresh_tasklist_cache()
    all_gtasks = list_all_gtasks()  # snapshot: {task_id: (list_id, task)}
    seen_task_ids = set()

    for page in task_pages:
        task_id = notion_gtask_id(page)
        if not task_id:
            create_gtask_for_page(
                state,
                page["id"],
                notion_target_tasklist_id(page),
                notion_title(page),
                notion_due_date(page),
                status_to_gtasks(notion_status(page)),
            )
            continue
        seen_task_ids.add(task_id)
        sync_linked_task_page(state, page, task_id, all_gtasks)

    deleted_task_ids = delete_gtasks_for_removed_pages(state, all_gtasks, seen_task_ids)
    # all_gtasks predates the deletes, so skip those ids or they'd be re-imported.
    import_unlinked_gtasks(state, all_gtasks, seen_task_ids | deleted_task_ids)


# ==== Entry point ============================================================
# One full pass: load state, sync events and tasks, save state.

def sync():
    """One full pass: Tasks path, then Events path, then persist state."""
    state = load_state()
    pages = get_notion_pages()

    task_pages = [p for p in pages if is_task_row(p)]
    event_pages = [p for p in pages if is_event_row(p)]

    sync_task_pages(state, task_pages)
    sync_event_pages(state, event_pages)

    save_state(state)


if __name__ == "__main__":
    sync()
