"""Hand-written in-memory fakes standing in for the Notion/Calendar/Tasks
clients that sync.py builds as module globals.

These are deliberately not full mocks of the real SDKs — they implement only
the call shapes sync.py actually uses (`.databases.query(...)`,
`.events().insert(...).execute()`, etc.) plus a small amount of state so
tests can set up a scenario, run a sync.py function against it, and assert
on what the fake recorded. Known limitation: pagination is not simulated
(everything is returned in one page, `has_more`/`nextPageToken` always
falsy) since no current test needs multi-page behavior.
"""

import itertools


class NotFound(Exception):
    """Stands in for whatever the real SDKs raise on a 404; sync.py's
    `get_gcal_event`/`get_gtask`/page-retrieve-in-a-try-block callers only
    care that *some* exception is raised, not its type."""


class _Call:
    """Wraps a zero-arg callable so it can be used as `...().execute()`."""

    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# ---- Notion -------------------------------------------------------------


class FakeNotionClient:
    """Stands in for `notion_client.Client`."""

    def __init__(self):
        self.pages = _FakeNotionPages(self)
        self.databases = _FakeNotionDatabases(self)
        self.client = _FakeNotionRawClient(self)

        self._pages = {}  # page_id -> page dict
        self._db_membership = {}  # db_id -> [page_id, ...]
        self._data_sources = {}  # db_id -> {"data_source_id": str, "properties": dict}
        self._id_counter = itertools.count(1)

    def new_id(self, prefix):
        return f"{prefix}-{next(self._id_counter)}"

    def register_data_source(self, database_id, properties):
        """Register the property schema returned for a database's data
        source, for `get_data_source_properties` to find."""
        ds_id = f"{database_id}-ds"
        self._data_sources[ds_id] = properties
        self._data_sources[database_id] = ds_id  # database_id -> its ds id

    def register_default_course_schema(self, main_database_id, courses_database_id="courses-db", title_prop_name="Name"):
        """Wire up the two-hop schema lookup get_courses_database_id/ensure_course
        depend on: the main database's Course relation property, and the Courses
        database's title property. Without this, any code path that creates a
        new Course page (e.g. import_unlinked_gtasks) raises NotFound."""
        self.register_data_source(
            main_database_id,
            {"Course": {"type": "relation", "relation": {"database_id": courses_database_id}}},
        )
        self.register_data_source(courses_database_id, {title_prop_name: {"type": "title"}})
        return courses_database_id

    def add_page(self, page, database_id=None):
        """Insert a pre-built page dict directly (bypassing `.create`) so
        tests can set exact last_edited_time/property values."""
        self._pages[page["id"]] = page
        if database_id:
            self._db_membership.setdefault(database_id, []).append(page["id"])
        return page


class _FakeNotionDatabases:
    def __init__(self, client):
        self._client = client

    def query(self, database_id, start_cursor=None):
        ids = self._client._db_membership.get(database_id, [])
        results = [self._client._pages[i] for i in ids if self._client._pages.get(i)]
        return {"results": results, "has_more": False, "next_cursor": None}


def _normalize_properties(properties):
    """Mirror what a real Notion write actually returns: for title/rich_text
    arrays written as `{"text": {"content": ...}}` (the shape sync.py's
    setters send), the real API echoes back a "plain_text" field alongside
    it. Our fake stores exactly what call sites send it, so without this
    normalization step, values written via set_notion_title/set_rich_text
    would be unreadable by notion_title/rich_text_plain immediately after.
    """
    for prop in properties.values():
        for key in ("title", "rich_text"):
            fragments = prop.get(key)
            if not fragments:
                continue
            for fragment in fragments:
                if "plain_text" not in fragment and "text" in fragment:
                    fragment["plain_text"] = fragment["text"]["content"]
    return properties


class _FakeNotionPages:
    def __init__(self, client):
        self._client = client

    def retrieve(self, page_id):
        page = self._client._pages.get(page_id)
        if page is None:
            raise NotFound(f"no such page {page_id}")
        return page

    def create(self, parent, properties):
        page_id = self._client.new_id("gen-page")
        page = {
            "id": page_id,
            "archived": False,
            "last_edited_time": "2020-01-01T00:00:00.000Z",
            "properties": _normalize_properties(properties),
        }
        self._client._pages[page_id] = page
        db_id = parent.get("database_id")
        if db_id:
            self._client._db_membership.setdefault(db_id, []).append(page_id)
        return page

    def update(self, page_id, properties=None, archived=None):
        page = self._client._pages[page_id]
        if properties:
            page["properties"].update(_normalize_properties(properties))
        if archived is not None:
            page["archived"] = archived
        return page


class _FakeNotionRawClient:
    """Stands in for the underlying httpx client `notion.client`, used only
    by `notion_get` to reach the data_sources endpoint (sync.py:186-198)."""

    def __init__(self, client):
        self._client = client

    def get(self, path, headers=None):
        if path.startswith("databases/"):
            database_id = path.split("/", 1)[1]
            ds_id = self._client._data_sources.get(database_id)
            if ds_id is None:
                raise NotFound(f"no data source registered for {database_id}")
            return _FakeHttpResponse({"data_sources": [{"id": ds_id}]})
        if path.startswith("data_sources/"):
            ds_id = path.split("/", 1)[1]
            properties = self._client._data_sources.get(ds_id)
            if properties is None:
                raise NotFound(f"no such data source {ds_id}")
            return _FakeHttpResponse({"properties": properties})
        raise NotFound(f"unhandled path {path}")


# ---- Google Calendar ------------------------------------------------------


class FakeGCalClient:
    """Stands in for the `googleapiclient` Calendar v3 resource."""

    def __init__(self):
        self._events = {}  # event_id -> event dict
        self._id_counter = itertools.count(1)

    def add_event(self, event):
        self._events[event["id"]] = event
        return event

    def events(self):
        return _FakeGCalEvents(self)


class _FakeGCalEvents:
    def __init__(self, client):
        self._client = client

    def get(self, calendarId, eventId):
        def fn():
            event = self._client._events.get(eventId)
            if event is None:
                raise NotFound(f"no such event {eventId}")
            return event

        return _Call(fn)

    def insert(self, calendarId, body):
        def fn():
            event_id = f"gen-event-{next(self._client._id_counter)}"
            event = {"id": event_id, "updated": "2020-01-01T00:00:00.000Z"}
            event.update(body)
            self._client._events[event_id] = event
            return event

        return _Call(fn)

    def patch(self, calendarId, eventId, body):
        def fn():
            event = self._client._events[eventId]
            event.update(body)
            return event

        return _Call(fn)

    def delete(self, calendarId, eventId):
        def fn():
            self._client._events.pop(eventId, None)
            return {}

        return _Call(fn)

    def list(self, calendarId, privateExtendedProperty=None, pageToken=None, showDeleted=None):
        def fn():
            items = list(self._client._events.values())
            if privateExtendedProperty == "notion_page_id=*":
                items = [
                    e
                    for e in items
                    if e.get("extendedProperties", {}).get("private", {}).get("notion_page_id")
                ]
            return {"items": items, "nextPageToken": None}

        return _Call(fn)


# ---- Google Tasks -----------------------------------------------------------

DEFAULT_TASKLIST_ID = "default-tasklist-id"


class FakeGTasksClient:
    """Stands in for the `googleapiclient` Tasks v1 resource. Pre-registers
    Google's default "My Tasks" list, matching how `get_default_tasklist_id`
    resolves the `@default` alias against a real account."""

    def __init__(self):
        self._tasklists = {DEFAULT_TASKLIST_ID: {"id": DEFAULT_TASKLIST_ID, "title": "My Tasks"}}
        self._tasks = {DEFAULT_TASKLIST_ID: {}}  # list_id -> {task_id: task}
        self._id_counter = itertools.count(1)

    def add_tasklist(self, list_id, title):
        self._tasklists[list_id] = {"id": list_id, "title": title}
        self._tasks.setdefault(list_id, {})
        return self._tasklists[list_id]

    def add_task(self, list_id, task):
        self._tasks.setdefault(list_id, {})[task["id"]] = task
        return task

    def tasklists(self):
        return _FakeGTaskLists(self)

    def tasks(self):
        return _FakeGTasks(self)


class _FakeGTaskLists:
    def __init__(self, client):
        self._client = client

    def get(self, tasklist):
        def fn():
            list_id = DEFAULT_TASKLIST_ID if tasklist == "@default" else tasklist
            tl = self._client._tasklists.get(list_id)
            if tl is None:
                raise NotFound(f"no such tasklist {tasklist}")
            return tl

        return _Call(fn)

    def list(self, pageToken=None, maxResults=100):
        def fn():
            return {"items": list(self._client._tasklists.values()), "nextPageToken": None}

        return _Call(fn)

    def insert(self, body):
        def fn():
            list_id = f"gen-tasklist-{next(self._client._id_counter)}"
            tl = {"id": list_id, "title": body["title"]}
            self._client._tasklists[list_id] = tl
            self._client._tasks[list_id] = {}
            return tl

        return _Call(fn)


class _FakeGTasks:
    def __init__(self, client):
        self._client = client

    def get(self, tasklist, task):
        def fn():
            t = self._client._tasks.get(tasklist, {}).get(task)
            if t is None:
                raise NotFound(f"no such task {task} in {tasklist}")
            return t

        return _Call(fn)

    def insert(self, tasklist, body):
        def fn():
            task_id = f"gen-task-{next(self._client._id_counter)}"
            task = {"id": task_id, "updated": "2020-01-01T00:00:00.000Z", "status": "needsAction"}
            task.update(body)
            self._client._tasks.setdefault(tasklist, {})[task_id] = task
            return task

        return _Call(fn)

    def patch(self, tasklist, task, body):
        def fn():
            t = self._client._tasks[tasklist][task]
            fields = dict(body)
            if fields.get("due", "unset") is None:
                t.pop("due", None)
                fields.pop("due", None)
            t.update(fields)
            return t

        return _Call(fn)

    def move(self, tasklist, task, destinationTasklist):
        def fn():
            t = self._client._tasks[tasklist].pop(task)
            self._client._tasks.setdefault(destinationTasklist, {})[task] = t
            return t

        return _Call(fn)

    def delete(self, tasklist, task):
        def fn():
            self._client._tasks.get(tasklist, {}).pop(task, None)
            return {}

        return _Call(fn)

    def list(self, tasklist, showCompleted=None, showHidden=None, pageToken=None, maxResults=100):
        def fn():
            return {"items": list(self._client._tasks.get(tasklist, {}).values()), "nextPageToken": None}

        return _Call(fn)


# ---- Fixture builders -------------------------------------------------------


def make_task_page(
    page_id,
    title="Test task",
    due=None,
    status="Not started",
    gtask_id=None,
    course_ids=None,
    sync_as=None,
    last_edited_time="2020-01-01T00:00:00.000Z",
    archived=False,
):
    """Build a well-formed Notion page dict shaped like a Tasks-Tracker row
    routed to Google Tasks (Sync As empty or "Task")."""
    properties = {
        "Task name": {"title": [{"plain_text": title}]},
        "Status": {"status": {"name": status} if status else None},
        "Course": {"relation": [{"id": cid} for cid in (course_ids or [])]},
        "Google Task ID": {"rich_text": [{"plain_text": gtask_id}] if gtask_id else []},
        "Sync As": {"select": {"name": sync_as} if sync_as else None},
    }
    if due:
        properties["Due date"] = {"date": {"start": due}}
    else:
        properties["Due date"] = {"date": None}
    return {
        "id": page_id,
        "archived": archived,
        "last_edited_time": last_edited_time,
        "properties": properties,
    }


def make_event_page(
    page_id,
    title="Test event",
    due="2026-01-01",
    gcal_id=None,
    last_edited_time="2020-01-01T00:00:00.000Z",
    archived=False,
):
    """Build a well-formed Notion page dict shaped like a Tasks-Tracker row
    routed to Google Calendar (Sync As = "Event")."""
    properties = {
        "Task name": {"title": [{"plain_text": title}]},
        "Due date": {"date": {"start": due} if due else None},
        "Google Event ID": {"rich_text": [{"plain_text": gcal_id}] if gcal_id else []},
        "Sync As": {"select": {"name": "Event"}},
    }
    return {
        "id": page_id,
        "archived": archived,
        "last_edited_time": last_edited_time,
        "properties": properties,
    }


def make_gcal_event(
    event_id,
    title="Test event",
    date="2026-01-01",
    updated="2020-01-01T00:00:00.000Z",
    notion_page_id=None,
):
    event = {
        "id": event_id,
        "summary": title,
        "start": {"date": date},
        "end": {"date": date},
        "updated": updated,
    }
    if notion_page_id:
        event["extendedProperties"] = {"private": {"notion_page_id": notion_page_id}}
    return event


def make_gtask(
    task_id,
    title="Test task",
    due=None,
    status="needsAction",
    updated="2020-01-01T00:00:00.000Z",
):
    task = {"id": task_id, "title": title, "status": status, "updated": updated}
    if due:
        task["due"] = f"{due}T00:00:00.000Z"
    return task
