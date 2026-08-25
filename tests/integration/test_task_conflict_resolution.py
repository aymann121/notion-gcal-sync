"""Integration tests for sync_task_pages against fake Notion/Tasks clients:
the same conflict-resolution matrix as events, plus task-specific behavior
(status <-> completion mapping, task-list assignment from Course, and the
DELETE_SYNC on/off branches for both a missing Google Task and an
orphaned state entry).
"""

import sync
from tests.fakes import DEFAULT_TASKLIST_ID, make_task_page, make_gtask


def test_new_notion_task_creates_a_google_task_in_default_list(fake_clients):
    """Given a Notion Task row with no Google Task ID yet and no Course set, sync_task_pages creates
    a Google Task in the default "My Tasks" list and writes its id back onto the page and into state."""
    page = make_task_page("page-1", title="Read chapter 1", due="2026-03-01", status="Not started", gtask_id=None)
    fake_clients["notion"].add_page(page)
    state = {}

    sync.sync_task_pages(state, [page])

    [created_task] = fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID].values()
    assert created_task["title"] == "Read chapter 1"
    assert created_task["status"] == "needsAction"
    assert sync.notion_gtask_id(page) == created_task["id"]
    assert state["page-1"]["tasklist_id"] == DEFAULT_TASKLIST_ID


def test_new_notion_task_with_course_creates_task_in_matching_list(fake_clients):
    """Given a Notion Task row whose Course relation points at a course named "Biology 101",
    sync_task_pages creates (or reuses) a Google Tasks list named "Biology 101" and creates the task there."""
    fake_clients["notion"].add_page(
        {"id": "course-1", "properties": {"Name": {"type": "title", "title": [{"plain_text": "Biology 101"}]}}},
    )
    page = make_task_page("page-1", title="Lab report", due="2026-03-01", course_ids=["course-1"], gtask_id=None)
    fake_clients["notion"].add_page(page)
    state = {}

    sync.sync_task_pages(state, [page])

    biology_list_id = {tl["title"]: tl["id"] for tl in fake_clients["gtasks"]._tasklists.values()}.get("Biology 101")
    assert biology_list_id is not None
    assert fake_clients["gtasks"]._tasks[biology_list_id]


def test_notion_changed_only_pushes_to_google_task(fake_clients):
    """Given only the Notion side changed since last_sync, sync_task_pages updates the Google Task
    to match Notion and leaves Notion untouched."""
    page = make_task_page(
        "page-1", title="Updated title", due="2026-04-01", status="Not started",
        gtask_id="task-1", last_edited_time="2026-01-02T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gtasks"].add_task(
        DEFAULT_TASKLIST_ID, make_gtask("task-1", title="Old title", due="2026-03-01", updated="2026-01-01T00:00:00Z")
    )
    state = {"page-1": {"last_sync": "2026-01-01T12:00:00Z", "kind": "task", "task_id": "task-1", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [page])

    updated = fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID]["task-1"]
    assert updated["title"] == "Updated title"
    assert updated["due"] == "2026-04-01T00:00:00.000Z"


def test_google_changed_only_pulls_into_notion(fake_clients):
    """Given only the Google side changed since last_sync, sync_task_pages updates the Notion page's
    title/due/status to match Google and leaves the Google Task untouched."""
    page = make_task_page(
        "page-1", title="Stale title", due="2026-03-01", status="Not started",
        gtask_id="task-1", last_edited_time="2026-01-01T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gtasks"].add_task(
        DEFAULT_TASKLIST_ID,
        make_gtask("task-1", title="Fresh title", due="2026-05-01", status="completed", updated="2026-01-02T00:00:00Z"),
    )
    state = {"page-1": {"last_sync": "2026-01-01T12:00:00Z", "kind": "task", "task_id": "task-1", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [page])

    assert sync.notion_title(page) == "Fresh title"
    assert sync.notion_due_date(page) == "2026-05-01"
    assert sync.notion_status(page) == "Done"


def test_both_changed_notion_wins(fake_clients):
    """Given both Notion and Google changed since last_sync, sync_task_pages pushes Notion's version
    to Google (the hardcoded "Notion wins" rule) rather than the reverse."""
    page = make_task_page(
        "page-1", title="Notion title", due="2026-06-01", status="Done",
        gtask_id="task-1", last_edited_time="2026-01-02T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gtasks"].add_task(
        DEFAULT_TASKLIST_ID,
        make_gtask("task-1", title="Google title", due="2026-07-01", status="needsAction", updated="2026-01-03T00:00:00Z"),
    )
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "task-1", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [page])

    updated = fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID]["task-1"]
    assert updated["title"] == "Notion title"
    assert updated["status"] == "completed"


def test_clearing_due_date_in_notion_clears_it_on_google_task(fake_clients):
    """Given a Notion task's due date is cleared (removed) while the Google Task still has one,
    sync_task_pages's clear_due path removes the due date on the Google side rather than leaving it stale."""
    page = make_task_page(
        "page-1", title="No longer due", due=None, status="Not started",
        gtask_id="task-1", last_edited_time="2026-01-02T00:00:00Z",
    )
    fake_clients["notion"].add_page(page)
    fake_clients["gtasks"].add_task(
        DEFAULT_TASKLIST_ID, make_gtask("task-1", title="No longer due", due="2026-03-01", updated="2026-01-01T00:00:00Z")
    )
    state = {"page-1": {"last_sync": "2026-01-01T12:00:00Z", "kind": "task", "task_id": "task-1", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [page])

    assert "due" not in fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID]["task-1"]


def test_missing_google_task_is_recreated_when_delete_sync_is_false(fake_clients):
    """Given the linked Google Task no longer exists anywhere and DELETE_SYNC is False (the default),
    sync_task_pages recreates the task and re-links it, instead of archiving the Notion page."""
    sync.DELETE_SYNC = False
    page = make_task_page("page-1", title="Still here", due="2026-09-01", gtask_id="missing-task")
    fake_clients["notion"].add_page(page)
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "missing-task", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [page])

    assert page["archived"] is False
    assert sync.notion_gtask_id(page) != "missing-task"
    assert state["page-1"]["task_id"] == sync.notion_gtask_id(page)


def test_missing_google_task_archives_notion_page_when_delete_sync_is_true(fake_clients):
    """Given the linked Google Task no longer exists anywhere and DELETE_SYNC is True, sync_task_pages
    archives the Notion page and drops its state entry instead of recreating the task."""
    sync.DELETE_SYNC = True
    page = make_task_page("page-1", title="Deleted on Google", due="2026-09-01", gtask_id="missing-task")
    fake_clients["notion"].add_page(page)
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "missing-task", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [page])

    assert page["archived"] is True
    assert "page-1" not in state


def test_orphaned_state_entry_is_ignored_forever_when_delete_sync_is_false(fake_clients):
    """Given a state entry's Notion page has been deleted/archived and DELETE_SYNC is False,
    sync_task_pages leaves the orphaned Google Task alone but records its id in _ignored_task_ids
    so import_unlinked_gtasks never re-imports it."""
    sync.DELETE_SYNC = False
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("orphan-task", status="needsAction"))
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "orphan-task", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [])  # page-1 no longer exists in Notion (not passed in, and not retrievable)

    assert "orphan-task" in state.get("_ignored_task_ids", [])
    assert "page-1" not in state
    assert "orphan-task" in fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID]  # left alone, not deleted
    # Confirm it really is never re-imported on a later pass:
    all_gtasks = sync.list_all_gtasks()
    sync.import_unlinked_gtasks(state, all_gtasks, set())
    assert len(fake_clients["notion"]._pages) == 0


def test_orphaned_state_entry_deletes_google_task_when_delete_sync_is_true(fake_clients):
    """Given a state entry's Notion page has been deleted/archived and DELETE_SYNC is True,
    sync_task_pages deletes the orphaned Google Task outright."""
    sync.DELETE_SYNC = True
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("orphan-task", status="needsAction"))
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "orphan-task", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [])

    assert "orphan-task" not in fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID]
    assert "page-1" not in state


def test_delete_sync_true_can_reimport_the_task_it_just_deleted(fake_clients):
    """KNOWN QUIRK: sync_task_pages snapshots all_gtasks once at the top of the function (line 698),
    before the orphaned-state cleanup loop runs. With DELETE_SYNC=True, cleanup deletes the orphaned
    Google Task from the real API, but import_unlinked_gtasks (called afterward, still using that
    stale snapshot) sees the task as if it still exists and needsAction, and recreates a Notion row
    for it. Net effect: a task deleted via DELETE_SYNC=True can reappear in Notion on the very same
    run. This test documents the behavior as-is; fixing it would mean re-fetching all_gtasks (or
    removing the deleted id from it) after the cleanup loop."""
    sync.DELETE_SYNC = True
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("orphan-task", title="Ghost task", status="needsAction"))
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "orphan-task", "tasklist_id": DEFAULT_TASKLIST_ID}}

    sync.sync_task_pages(state, [])

    assert "orphan-task" not in fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID]  # really deleted on Google
    reimported_pages = [p for p in fake_clients["notion"]._pages.values() if sync.notion_gtask_id(p) == "orphan-task"]
    assert len(reimported_pages) == 1  # ...but reimported into Notion anyway
