"""Integration tests for sync_event_pages against fake Notion/Calendar
clients: the full conflict-resolution matrix from CLAUDE.md ("Only one side
changed... Both changed -> Notion wins... Neither side has a link yet ->
create") plus the DELETE_SYNC on/off branches for a missing counterpart.

Every test uses the `fake_clients` fixture (see conftest.py), which installs
fresh FakeNotionClient/FakeGCalClient/FakeGTasksClient instances onto the
sync module before the test body runs.
"""

import sync
from tests.fakes import make_event_page, make_gcal_event


def test_new_notion_event_creates_a_calendar_event(fake_clients):
    """Given a Notion Event row with no Google Event ID yet, sync_event_pages creates a Calendar event
    and writes its id back onto the page and into state."""
    page = make_event_page("page-1", title="Exam", due="2026-03-01", gcal_id=None)
    fake_clients["notion"].add_page(page)
    state = {}

    sync.sync_event_pages(state, [page])

    [created_event] = fake_clients["gcal"]._events.values()
    assert created_event["summary"] == "Exam"
    assert created_event["start"]["date"] == "2026-03-01"
    assert sync.notion_gcal_id(page) == created_event["id"]
    assert state["page-1"]["event_id"] == created_event["id"]
    assert state["page-1"]["kind"] == "event"


def test_notion_changed_only_pushes_to_calendar(fake_clients):
    """Given only the Notion side changed since last_sync, sync_event_pages updates the Calendar event
    to match Notion and leaves Notion untouched."""
    page = make_event_page(
        "page-1", title="Updated title", due="2026-04-01",
        gcal_id="event-1", last_edited_time="2026-01-02T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gcal"].add_event(
        make_gcal_event("event-1", title="Old title", date="2026-03-01", updated="2026-01-01T00:00:00Z")
    )
    state = {"page-1": {"last_sync": "2026-01-01T12:00:00Z", "kind": "event", "event_id": "event-1"}}

    sync.sync_event_pages(state, [page])

    assert fake_clients["gcal"]._events["event-1"]["summary"] == "Updated title"
    assert fake_clients["gcal"]._events["event-1"]["start"]["date"] == "2026-04-01"


def test_google_changed_only_pulls_into_notion(fake_clients):
    """Given only the Calendar side changed since last_sync, sync_event_pages updates the Notion page
    to match Calendar and leaves the Calendar event untouched."""
    page = make_event_page(
        "page-1", title="Stale title", due="2026-03-01",
        gcal_id="event-1", last_edited_time="2026-01-01T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gcal"].add_event(
        make_gcal_event("event-1", title="Fresh title", date="2026-05-01", updated="2026-01-02T00:00:00Z")
    )
    state = {"page-1": {"last_sync": "2026-01-01T12:00:00Z", "kind": "event", "event_id": "event-1"}}

    sync.sync_event_pages(state, [page])

    assert sync.notion_title(page) == "Fresh title"
    assert sync.notion_due_date(page) == "2026-05-01"


def test_both_changed_notion_wins(fake_clients):
    """Given both Notion and Calendar changed since last_sync, sync_event_pages pushes Notion's
    version to Calendar (the hardcoded "Notion wins" rule) rather than the reverse."""
    page = make_event_page(
        "page-1", title="Notion title", due="2026-06-01",
        gcal_id="event-1", last_edited_time="2026-01-02T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gcal"].add_event(
        make_gcal_event("event-1", title="Google title", date="2026-07-01", updated="2026-01-03T00:00:00Z")
    )
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "event", "event_id": "event-1"}}

    sync.sync_event_pages(state, [page])

    assert fake_clients["gcal"]._events["event-1"]["summary"] == "Notion title"
    assert fake_clients["gcal"]._events["event-1"]["start"]["date"] == "2026-06-01"
    assert sync.notion_title(page) == "Notion title"  # Notion page itself is not overwritten


def test_neither_changed_writes_no_updates_but_refreshes_last_sync(fake_clients):
    """Given neither side changed since last_sync, sync_event_pages makes no writes to either side,
    but still bumps last_sync so a future run's window starts from now."""
    page = make_event_page(
        "page-1", title="Same title", due="2026-08-01",
        gcal_id="event-1", last_edited_time="2026-01-01T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gcal"].add_event(
        make_gcal_event("event-1", title="Same title", date="2026-08-01", updated="2026-01-01T00:00:00Z")
    )
    state = {"page-1": {"last_sync": "2026-01-02T00:00:00Z", "kind": "event", "event_id": "event-1"}}

    sync.sync_event_pages(state, [page])

    assert fake_clients["gcal"]._events["event-1"]["summary"] == "Same title"
    assert state["page-1"]["last_sync"] != "2026-01-02T00:00:00Z"


def test_missing_google_event_is_recreated_when_delete_sync_is_false(fake_clients):
    """Given the linked Calendar event no longer exists and DELETE_SYNC is False (the default),
    sync_event_pages recreates the event and re-links it, instead of archiving the Notion page."""
    sync.DELETE_SYNC = False
    page = make_event_page("page-1", title="Still here", due="2026-09-01", gcal_id="missing-event")
    fake_clients["notion"].add_page(page)
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "event", "event_id": "missing-event"}}

    sync.sync_event_pages(state, [page])

    assert page["archived"] is False
    assert len(fake_clients["gcal"]._events) == 1
    new_event_id = next(iter(fake_clients["gcal"]._events))
    assert sync.notion_gcal_id(page) == new_event_id


def test_missing_google_event_archives_notion_page_when_delete_sync_is_true(fake_clients):
    """Given the linked Calendar event no longer exists and DELETE_SYNC is True, sync_event_pages
    archives the Notion page and drops its state entry instead of recreating the event."""
    sync.DELETE_SYNC = True
    page = make_event_page("page-1", title="Deleted on Google", due="2026-09-01", gcal_id="missing-event")
    fake_clients["notion"].add_page(page)
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "event", "event_id": "missing-event"}}

    sync.sync_event_pages(state, [page])

    assert page["archived"] is True
    assert "page-1" not in state
    assert len(fake_clients["gcal"]._events) == 0


def test_event_page_without_due_date_is_skipped(fake_clients):
    """Given an Event row with no due date set, sync_event_pages skips it entirely (Calendar events
    require a date), leaving no Calendar event created and no state entry written."""
    page = make_event_page("page-1", title="No date yet", due=None, gcal_id=None)
    fake_clients["notion"].add_page(page)
    state = {}

    sync.sync_event_pages(state, [page])

    assert fake_clients["gcal"]._events == {}
    assert state == {}


def test_deleted_notion_page_recreates_from_tagged_calendar_event_when_delete_sync_false(fake_clients):
    """Given a Calendar event tagged with a notion_page_id that no longer resolves to a live Notion page
    (deleted/archived) and DELETE_SYNC is False, sync_event_pages recreates a Notion Event row from the
    Calendar event and retags the event with the new page id."""
    sync.DELETE_SYNC = False
    fake_clients["gcal"].add_event(
        make_gcal_event("event-1", title="Orphaned event", date="2026-10-01", notion_page_id="deleted-page")
    )
    state = {}

    sync.sync_event_pages(state, [])

    assert len(fake_clients["notion"]._pages) == 1
    [new_page] = fake_clients["notion"]._pages.values()
    assert sync.notion_title(new_page) == "Orphaned event"
    assert fake_clients["gcal"]._events["event-1"]["extendedProperties"]["private"]["notion_page_id"] == new_page["id"]


def test_deleted_notion_page_deletes_calendar_event_when_delete_sync_true(fake_clients):
    """Given a Calendar event tagged with a notion_page_id that no longer resolves to a live Notion page
    and DELETE_SYNC is True, sync_event_pages deletes the Calendar event instead of recreating anything in Notion."""
    sync.DELETE_SYNC = True
    fake_clients["gcal"].add_event(
        make_gcal_event("event-1", title="Orphaned event", date="2026-10-01", notion_page_id="deleted-page")
    )
    state = {}

    sync.sync_event_pages(state, [])

    assert fake_clients["gcal"]._events == {}
    assert fake_clients["notion"]._pages == {}
