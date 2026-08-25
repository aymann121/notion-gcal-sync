#!/usr/bin/env python3
"""
Two-way sync: Notion Tasks Tracker ↔ Google Tasks + Google Calendar.

Routing via Notion "Sync As":
  Task (or empty) → Google Tasks  (Course → list; Status ↔ completion)
  Event           → Google Calendar (all-day; title + due)

Conflict rule: compare last_edited_time / updated vs last_sync;
whichever side changed more recently wins. Both changed → Notion wins.

Linked IDs live on the Notion page (Google Task ID / Google Event ID).
Deletions are not mirrored unless DELETE_SYNC is True. See README.md.
"""

import os
import json
import datetime
from notion_client import Client as NotionClient
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ---- Config -----------------------------------------------------------------

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

# If True, deleting on one side archives/deletes the other.
DELETE_SYNC = False

# Per-page last_sync (+ task/event ids) so we know which side changed.
STATE_FILE = "sync_state.json"

# ---- Clients ----------------------------------------------------------------

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

# ---- State ------------------------------------------------------------------


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---- Notion helpers ---------------------------------------------------------


def get_notion_pages():
    """All Tasks Tracker rows (tasks may have no due date; events need one later)."""
    pages = []
    cursor = None
    while True:
        kwargs = {"database_id": NOTION_DATABASE_ID}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        pages.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return pages


def rich_text_plain(page, prop_name):
    """First plain-text chunk of a rich_text property, or None."""
    rt = page["properties"].get(prop_name, {}).get("rich_text", [])
    return rt[0]["plain_text"] if rt else None


def notion_title(page):
    return "".join(
        t["plain_text"] for t in page["properties"][PROP_TITLE]["title"]
    ) or "(untitled)"


def notion_due_date(page):
    """YYYY-MM-DD only (time portion discarded)."""
    d = page["properties"].get(PROP_DUE_DATE, {}).get("date")
    if not d:
        return None
    start = d["start"]
    return start[:10] if start else None


def notion_status(page):
    status = page["properties"].get(PROP_STATUS, {}).get("status")
    return status["name"] if status else None


def notion_sync_as(page):
    sel = page["properties"].get(PROP_SYNC_AS, {}).get("select")
    return sel["name"] if sel else None


def notion_gcal_id(page):
    return rich_text_plain(page, PROP_GCAL_EVENT_ID)


def notion_gtask_id(page):
    return rich_text_plain(page, PROP_GTASK_ID)


def notion_course_page_ids(page):
    rel = page["properties"].get(PROP_COURSE, {}).get("relation", [])
    return [r["id"] for r in rel]


_course_title_cache = {}


def course_title(page_id):
    """Resolve a Course relation page to its title (cached per run)."""
    if page_id in _course_title_cache:
        return _course_title_cache[page_id]
    course = notion.pages.retrieve(page_id=page_id)
    title_prop = None
    for prop in course["properties"].values():
        if prop["type"] == "title":
            title_prop = prop
            break
    name = "".join(t["plain_text"] for t in title_prop["title"]) if title_prop else ""
    name = name or "(untitled course)"
    _course_title_cache[page_id] = name
    return name


def notion_target_tasklist_id(page):
    """Google Task list id = first Course's list, else Google's default list."""
    ids = notion_course_page_ids(page)
    if not ids:
        return get_default_tasklist_id()
    return ensure_tasklist(course_title(ids[0]))


_courses_database_id = None
_course_pages_cache = None  # title → page_id
_course_title_prop_name = None

# Notion's 2025 multi-source-database split moved property schemas off the
# database object onto a separate "data source" object; the pinned
# notion-client SDK (2.2.1) predates that split and has no data_sources
# endpoint. GET/query/create for pages still work fine on the old default
# version everywhere else in this script — only schema lookups need this.
NOTION_LATEST_VERSION = "2026-03-11"


def notion_get(path, notion_version):
    """Raw GET via the underlying httpx client, on a specific Notion-Version."""
    response = notion.client.get(path, headers={"Notion-Version": notion_version})
    response.raise_for_status()
    return response.json()


def get_data_source_properties(database_id):
    """Property schema of a database's (first) data source."""
    db = notion_get(f"databases/{database_id}", NOTION_LATEST_VERSION)
    data_source_id = db["data_sources"][0]["id"]
    data_source = notion_get(f"data_sources/{data_source_id}", NOTION_LATEST_VERSION)
    return data_source["properties"]


def get_courses_database_id():
    """Target database id of the Course relation property (cached)."""
    global _courses_database_id
    if _courses_database_id is None:
        props = get_data_source_properties(NOTION_DATABASE_ID)
        _courses_database_id = props[PROP_COURSE]["relation"]["database_id"]
    return _courses_database_id


def refresh_course_cache():
    """Load all Courses pages (title → page_id) and find the title property name."""
    global _course_pages_cache, _course_title_prop_name
    courses_db_id = get_courses_database_id()
    props = get_data_source_properties(courses_db_id)
    for name, prop in props.items():
        if prop["type"] == "title":
            _course_title_prop_name = name
            break

    _course_pages_cache = {}
    cursor = None
    while True:
        kwargs = {"database_id": courses_db_id}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        for page in resp["results"]:
            title_prop = page["properties"].get(_course_title_prop_name, {})
            name = "".join(t["plain_text"] for t in title_prop.get("title", []))
            if name:
                _course_pages_cache[name] = page["id"]
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return _course_pages_cache


def ensure_course(name):
    """Return page id for a Course with this title; create it if missing."""
    if _course_pages_cache is None:
        refresh_course_cache()
    if name in _course_pages_cache:
        return _course_pages_cache[name]
    created = notion.pages.create(
        parent={"database_id": get_courses_database_id()},
        properties={_course_title_prop_name: {"title": [{"text": {"content": name}}]}},
    )
    _course_pages_cache[name] = created["id"]
    return created["id"]


def is_task_row(page):
    """Empty Sync As defaults to Task."""
    sync_as = notion_sync_as(page)
    return sync_as is None or sync_as == SYNC_AS_TASK


def is_event_row(page):
    return notion_sync_as(page) == SYNC_AS_EVENT


def set_rich_text(page_id, prop_name, value):
    notion.pages.update(
        page_id=page_id,
        properties={
            prop_name: {"rich_text": [{"text": {"content": value}}]}
        },
    )


def set_notion_gcal_id(page_id, event_id):
    set_rich_text(page_id, PROP_GCAL_EVENT_ID, event_id)


def set_notion_gtask_id(page_id, task_id):
    set_rich_text(page_id, PROP_GTASK_ID, task_id)


def set_notion_due_date(page_id, date_str):
    if date_str:
        notion.pages.update(
            page_id=page_id,
            properties={PROP_DUE_DATE: {"date": {"start": date_str[:10]}}},
        )
    else:
        notion.pages.update(
            page_id=page_id,
            properties={PROP_DUE_DATE: {"date": None}},
        )


def set_notion_title(page_id, title):
    notion.pages.update(
        page_id=page_id,
        properties={PROP_TITLE: {"title": [{"text": {"content": title}}]}},
    )


def set_notion_status(page_id, status_name):
    notion.pages.update(
        page_id=page_id,
        properties={PROP_STATUS: {"status": {"name": status_name}}},
    )


def create_notion_event(title, date_str, event_id):
    """Create a Notion row from a Google Calendar event (Sync As = Event)."""
    return notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            PROP_TITLE: {"title": [{"text": {"content": title}}]},
            PROP_DUE_DATE: {"date": {"start": date_str[:10]}},
            PROP_GCAL_EVENT_ID: {"rich_text": [{"text": {"content": event_id}}]},
            PROP_SYNC_AS: {"select": {"name": SYNC_AS_EVENT}},
        },
    )


def create_notion_task(title, due, task_id, status_name, course_page_id=None):
    """Create a Notion row from a Google Task (Sync As = Task)."""
    properties = {
        PROP_TITLE: {"title": [{"text": {"content": title}}]},
        PROP_STATUS: {"status": {"name": status_name}},
        PROP_GTASK_ID: {"rich_text": [{"text": {"content": task_id}}]},
        PROP_SYNC_AS: {"select": {"name": SYNC_AS_TASK}},
    }
    if due:
        properties[PROP_DUE_DATE] = {"date": {"start": due[:10]}}
    if course_page_id:
        properties[PROP_COURSE] = {"relation": [{"id": course_page_id}]}
    return notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)


def status_to_gtasks(status_name):
    """Notion Done → completed; anything else → needsAction."""
    return "completed" if status_name == STATUS_DONE else "needsAction"


def gtasks_to_status(gtasks_status):
    """Google completed → Done; else Not started (not In progress)."""
    return STATUS_DONE if gtasks_status == "completed" else STATUS_NOT_STARTED


# ---- Google Calendar helpers ------------------------------------------------


def get_gcal_event(event_id):
    try:
        return gcal.events().get(
            calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()
    except Exception:
        return None


def create_gcal_event(title, date_str, notion_page_id):
    """All-day event tagged with notion_page_id for reverse lookup."""
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
    """Events previously created by this script (private notion_page_id tag)."""
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


# ---- Google Tasks helpers ---------------------------------------------------

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
    page_token = None
    while True:
        resp = gtasks.tasklists().list(pageToken=page_token, maxResults=100).execute()
        for tl in resp.get("items", []):
            _tasklist_cache[tl["title"]] = tl["id"]
            _tasklist_titles[tl["id"]] = tl["title"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return _tasklist_cache


def ensure_tasklist(name):
    """Return list id for name; create the list if missing."""
    if _tasklist_cache is None:
        refresh_tasklist_cache()
    if name in _tasklist_cache:
        return _tasklist_cache[name]
    created = gtasks.tasklists().insert(body={"title": name}).execute()
    _tasklist_cache[name] = created["id"]
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
    except Exception:
        return None


def find_gtask(task_id, preferred_list_id=None):
    """Find a task by id (try known list first, then scan all lists)."""
    if preferred_list_id:
        task = get_gtask(preferred_list_id, task_id)
        if task is not None:
            return preferred_list_id, task
    if _tasklist_cache is None:
        refresh_tasklist_cache()
    for list_id in _tasklist_cache.values():
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
    if from_list_id == to_list_id:
        return
    gtasks.tasks().move(
        tasklist=from_list_id,
        task=task_id,
        destinationTasklist=to_list_id,
    ).execute()


def list_all_gtasks():
    """task_id → (list_id, task), including completed/hidden."""
    if _tasklist_cache is None:
        refresh_tasklist_cache()
    result = {}
    for list_id in _tasklist_cache.values():
        page_token = None
        while True:
            resp = gtasks.tasks().list(
                tasklist=list_id,
                showCompleted=True,
                showHidden=True,
                pageToken=page_token,
                maxResults=100,
            ).execute()
            for task in resp.get("items", []):
                result[task["id"]] = (list_id, task)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return result


# ---- Core sync logic --------------------------------------------------------


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


def last_sync_dt(state, page_id):
    """When we last successfully synced this page (or epoch if never)."""
    last_sync = state.get(page_id, {}).get("last_sync")
    if last_sync:
        return parse_dt(last_sync)
    return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def sync_event_pages(state, event_pages):
    """Sync As = Event ↔ Google Calendar all-day events."""
    gcal_events = {e["id"]: e for e in list_gcal_events_from_notion()}
    seen_event_ids = set()

    for page in event_pages:
        page_id = page["id"]
        title = notion_title(page)
        due = notion_due_date(page)
        if not due:
            continue  # calendar events require a due date

        event_id = notion_gcal_id(page)
        notion_edited = parse_dt(page["last_edited_time"])

        # New Notion event → create Calendar event and store its id
        if not event_id:
            event = create_gcal_event(title, due, page_id)
            set_notion_gcal_id(page_id, event["id"])
            state[page_id] = {
                "last_sync": utc_now_iso(),
                "kind": "event",
                "event_id": event["id"],
            }
            continue

        seen_event_ids.add(event_id)
        event = gcal_events.get(event_id) or get_gcal_event(event_id)

        # Google event missing: archive Notion or recreate the event
        if event is None:
            if DELETE_SYNC:
                notion.pages.update(page_id=page_id, archived=True)
                state.pop(page_id, None)
            else:
                new_event = create_gcal_event(title, due, page_id)
                set_notion_gcal_id(page_id, new_event["id"])
                state[page_id] = {
                    "last_sync": utc_now_iso(),
                    "kind": "event",
                    "event_id": new_event["id"],
                }
            continue

        # Decide which side wins since last_sync
        google_updated = parse_dt(event["updated"])
        last = last_sync_dt(state, page_id)
        notion_changed = notion_edited > last
        google_changed = google_updated > last

        if notion_changed and not google_changed:
            update_gcal_event(event_id, title=title, date_str=due)
        elif google_changed and not notion_changed:
            g_title = event.get("summary", title)
            g_date = event["start"].get("date") or event["start"].get("dateTime", due)[:10]
            set_notion_title(page_id, g_title)
            set_notion_due_date(page_id, g_date)
        elif notion_changed and google_changed:
            update_gcal_event(event_id, title=title, date_str=due)  # Notion wins

        state[page_id] = {
            "last_sync": utc_now_iso(),
            "kind": "event",
            "event_id": event_id,
        }

    # Tagged Calendar events whose Notion page was deleted
    for event_id, event in gcal_events.items():
        if event_id in seen_event_ids:
            continue
        notion_page_id = (
            event.get("extendedProperties", {})
            .get("private", {})
            .get("notion_page_id")
        )
        try:
            page = notion.pages.retrieve(page_id=notion_page_id) if notion_page_id else None
        except Exception:
            page = None

        if page is not None and not page.get("archived"):
            continue

        if DELETE_SYNC:
            gcal.events().delete(
                calendarId=GOOGLE_CALENDAR_ID, eventId=event_id
            ).execute()
            continue

        # Recreate Notion Event row and retag the Calendar event
        g_title = event.get("summary", "(untitled)")
        g_date = event["start"].get("date") or (
            event["start"].get("dateTime", "")[:10] or None
        )
        if not g_date:
            continue
        new_page = create_notion_event(g_title, g_date, event_id)
        gcal.events().patch(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id,
            body={
                "extendedProperties": {
                    "private": {"notion_page_id": new_page["id"]}
                }
            },
        ).execute()
        state[new_page["id"]] = {
            "last_sync": utc_now_iso(),
            "kind": "event",
            "event_id": event_id,
        }


def sync_task_pages(state, task_pages):
    """Sync As = Task (or empty) ↔ Google Tasks."""
    refresh_tasklist_cache()
    all_gtasks = list_all_gtasks()
    seen_task_ids = set()

    for page in task_pages:
        page_id = page["id"]
        title = notion_title(page)
        due = notion_due_date(page)
        status_name = notion_status(page)
        g_status = status_to_gtasks(status_name)
        target_list_id = notion_target_tasklist_id(page)
        task_id = notion_gtask_id(page)
        notion_edited = parse_dt(page["last_edited_time"])
        prev = state.get(page_id, {})

        # New Notion task → create Google Task and store its id
        if not task_id:
            task = create_gtask(target_list_id, title, due=due, status=g_status)
            set_notion_gtask_id(page_id, task["id"])
            state[page_id] = {
                "last_sync": utc_now_iso(),
                "kind": "task",
                "task_id": task["id"],
                "tasklist_id": target_list_id,
            }
            continue

        seen_task_ids.add(task_id)
        preferred = prev.get("tasklist_id")
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
                new_task = create_gtask(
                    target_list_id, title, due=due, status=g_status
                )
                set_notion_gtask_id(page_id, new_task["id"])
                state[page_id] = {
                    "last_sync": utc_now_iso(),
                    "kind": "task",
                    "task_id": new_task["id"],
                    "tasklist_id": target_list_id,
                }
            continue

        # Course changed → move to the matching task list
        if list_id != target_list_id:
            move_gtask(task_id, list_id, target_list_id)
            list_id = target_list_id
            task = get_gtask(list_id, task_id) or task

        google_updated = parse_dt(task["updated"])
        last = last_sync_dt(state, page_id)
        notion_changed = notion_edited > last
        google_changed = google_updated > last

        if notion_changed and not google_changed:
            clear_due = due is None and bool(task.get("due"))
            update_gtask(
                list_id,
                task_id,
                title=title,
                due=due,
                status=g_status,
                clear_due=clear_due,
            )
        elif google_changed and not notion_changed:
            set_notion_title(page_id, task.get("title") or title)
            set_notion_due_date(page_id, due_from_gtasks(task))
            set_notion_status(page_id, gtasks_to_status(task.get("status")))
        elif notion_changed and google_changed:
            clear_due = due is None and bool(task.get("due"))
            update_gtask(
                list_id,
                task_id,
                title=title,
                due=due,
                status=g_status,
                clear_due=clear_due,
            )  # Notion wins

        state[page_id] = {
            "last_sync": utc_now_iso(),
            "kind": "task",
            "task_id": task_id,
            "tasklist_id": list_id,
        }

    # State entries whose Notion page is gone (no Google→Notion create for tasks)
    for page_id, entry in list(state.items()):
        if not isinstance(entry, dict) or entry.get("kind") != "task":
            continue
        task_id = entry.get("task_id")
        if not task_id or task_id in seen_task_ids:
            continue
        try:
            page = notion.pages.retrieve(page_id=page_id)
        except Exception:
            page = None
        if page is None or page.get("archived"):
            if DELETE_SYNC:
                list_id = entry.get("tasklist_id")
                if list_id:
                    try:
                        gtasks.tasks().delete(
                            tasklist=list_id, task=task_id
                        ).execute()
                    except Exception:
                        pass
            else:
                # Leave the orphaned Google task alone, but never re-import it.
                ignored = set(state.get("_ignored_task_ids", []))
                ignored.add(task_id)
                state["_ignored_task_ids"] = sorted(ignored)
            state.pop(page_id, None)

    import_unlinked_gtasks(state, all_gtasks, seen_task_ids)


def import_unlinked_gtasks(state, all_gtasks, linked_task_ids):
    """Create Notion rows for active Google Tasks not yet linked to any page."""
    ignored = set(state.get("_ignored_task_ids", []))
    default_list_id = get_default_tasklist_id()

    for task_id, (list_id, task) in all_gtasks.items():
        if task_id in linked_task_ids or task_id in ignored:
            continue
        if task.get("status") != "needsAction":
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
        state[new_page["id"]] = {
            "last_sync": utc_now_iso(),
            "kind": "task",
            "task_id": task_id,
            "tasklist_id": list_id,
        }


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
