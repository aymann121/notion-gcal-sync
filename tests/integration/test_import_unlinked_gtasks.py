"""Integration tests for import_unlinked_gtasks — creating Notion rows for
Google Tasks that were created directly in Google Tasks rather than synced
down from Notion (CLAUDE.md's "Reverse-linking quirks" for Tasks).
"""

import sync
from tests.fakes import DEFAULT_TASKLIST_ID, make_gtask


def test_unlinked_needs_action_task_is_imported_into_notion(fake_clients):
    """Given a Google Task with no matching Notion page and status needsAction, import_unlinked_gtasks
    creates a new Notion Task row linked to it."""
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("gtask-1", title="From Google", status="needsAction"))
    state = {}
    all_gtasks = sync.list_all_gtasks()

    sync.import_unlinked_gtasks(state, all_gtasks, linked_task_ids=set())

    [page] = fake_clients["notion"]._pages.values()
    assert sync.notion_title(page) == "From Google"
    assert sync.notion_gtask_id(page) == "gtask-1"
    assert state[page["id"]]["task_id"] == "gtask-1"


def test_completed_unlinked_task_is_not_imported(fake_clients):
    """Given a Google Task with no matching Notion page but status "completed", import_unlinked_gtasks
    leaves it alone (old completed history should not flood into Notion, per CLAUDE.md)."""
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("gtask-1", title="Old and done", status="completed"))
    state = {}
    all_gtasks = sync.list_all_gtasks()

    sync.import_unlinked_gtasks(state, all_gtasks, linked_task_ids=set())

    assert fake_clients["notion"]._pages == {}
    assert state == {}


def test_already_linked_task_is_not_reimported(fake_clients):
    """Given a Google Task id that's already linked to a Notion page this run, import_unlinked_gtasks
    skips it (it's not "unlinked", just processed earlier in sync_task_pages)."""
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("gtask-1", status="needsAction"))
    state = {}
    all_gtasks = sync.list_all_gtasks()

    sync.import_unlinked_gtasks(state, all_gtasks, linked_task_ids={"gtask-1"})

    assert fake_clients["notion"]._pages == {}


def test_ignored_task_id_is_never_reimported(fake_clients):
    """Given a Google Task id previously recorded in state["_ignored_task_ids"] (its Notion page was
    deleted without DELETE_SYNC), import_unlinked_gtasks never recreates a Notion row for it."""
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("gtask-1", status="needsAction"))
    state = {"_ignored_task_ids": ["gtask-1"]}
    all_gtasks = sync.list_all_gtasks()

    sync.import_unlinked_gtasks(state, all_gtasks, linked_task_ids=set())

    assert fake_clients["notion"]._pages == {}


def test_unlinked_task_in_non_default_list_gets_a_matching_course(fake_clients):
    """Given a Google Task in a non-default task list (e.g. "Chemistry"), import_unlinked_gtasks
    resolves/creates a matching Notion Course page and links the new Task row to it."""
    fake_clients["gtasks"].add_tasklist("chem-list", "Chemistry")
    fake_clients["gtasks"].add_task("chem-list", make_gtask("gtask-1", title="Lab prep", status="needsAction"))
    state = {}
    all_gtasks = sync.list_all_gtasks()

    sync.import_unlinked_gtasks(state, all_gtasks, linked_task_ids=set())

    [page] = [p for p in fake_clients["notion"]._pages.values() if sync.notion_gtask_id(p) == "gtask-1"]
    assert sync.notion_course_page_ids(page) != []


def test_unlinked_task_in_default_list_gets_no_course(fake_clients):
    """Given a Google Task in the default "My Tasks" list, import_unlinked_gtasks creates the Notion
    row with no Course relation set."""
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("gtask-1", title="Misc", status="needsAction"))
    state = {}
    all_gtasks = sync.list_all_gtasks()

    sync.import_unlinked_gtasks(state, all_gtasks, linked_task_ids=set())

    [page] = fake_clients["notion"]._pages.values()
    assert sync.notion_course_page_ids(page) == []
