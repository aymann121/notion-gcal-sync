"""End-to-end smoke tests that run sync.py against REAL Notion and Google
accounts. These hit real external APIs and mutate real data, so they:

  * are marked `@pytest.mark.e2e` and excluded by default (pytest.ini sets
    `addopts = -m "not e2e"`);
  * only run when E2E_* env vars are present, pointing at disposable test
    resources (never a real production Tasks Tracker);
  * are meant to be run manually (`pytest -m e2e tests/e2e -v`), not wired
    into CI, since they mutate real Notion/Google state and cost real quota.

Required env vars (in addition to the normal NOTION_TOKEN/NOTION_DATABASE_ID/
GOOGLE_TOKEN_JSON that sync.py itself needs):
  E2E_ENABLE=1               -- explicit opt-in guard, belt-and-suspenders
                                 alongside the pytest marker
  E2E_NOTION_DATABASE_ID     -- a disposable "Tasks Tracker" database id
  E2E_GOOGLE_CALENDAR_ID     -- a disposable/test calendar id (not "primary")

Safety: never point these at your real production database/calendar. A bug
in sync.py's conflict resolution or DELETE_SYNC handling could delete or
duplicate real data; that's exactly the risk this suite exists to catch
*before* it reaches production, using throwaway resources instead.
"""

import os
import time

import pytest

pytestmark = pytest.mark.e2e

E2E_ENABLED = os.environ.get("E2E_ENABLE") == "1"
SKIP_REASON = (
    "E2E tests require E2E_ENABLE=1 plus E2E_NOTION_DATABASE_ID / "
    "E2E_GOOGLE_CALENDAR_ID pointing at disposable test resources; "
    "see tests/e2e/test_full_roundtrip.py module docstring."
)


@pytest.fixture(autouse=True)
def _require_e2e_env():
    if not E2E_ENABLED:
        pytest.skip(SKIP_REASON)


@pytest.fixture
def e2e_sync(monkeypatch):
    """Import sync.py pointed at the disposable E2E database/calendar
    rather than whatever NOTION_DATABASE_ID/GOOGLE_CALENDAR_ID happen to be
    set to in the environment, and isolate its state file."""
    import sync as s

    monkeypatch.setattr(s, "NOTION_DATABASE_ID", os.environ["E2E_NOTION_DATABASE_ID"])
    monkeypatch.setattr(s, "GOOGLE_CALENDAR_ID", os.environ["E2E_GOOGLE_CALENDAR_ID"])
    monkeypatch.setattr(s, "STATE_FILE", "/tmp/e2e_sync_state.json")
    return s


def test_creating_a_task_in_notion_creates_a_google_task(e2e_sync):
    """Given a new Task row created directly via the Notion API, running sync() creates a
    matching Google Task and writes its id back onto the Notion page."""
    page = e2e_sync.notion.pages.create(
        parent={"database_id": e2e_sync.NOTION_DATABASE_ID},
        properties={
            e2e_sync.PROP_TITLE: {"title": [{"text": {"content": "E2E: created in Notion"}}]},
        },
    )
    try:
        e2e_sync.sync()
        page = e2e_sync.notion.pages.retrieve(page_id=page["id"])
        gtask_id = e2e_sync.notion_gtask_id(page)
        assert gtask_id is not None
        task = e2e_sync.gtasks.tasks().get(
            tasklist=e2e_sync.get_default_tasklist_id(), task=gtask_id
        ).execute()
        assert task["title"] == "E2E: created in Notion"
    finally:
        e2e_sync.notion.pages.update(page_id=page["id"], archived=True)


def test_creating_a_task_in_google_imports_it_into_notion(e2e_sync):
    """Given a new Google Task created directly via the Tasks API, running sync() imports it
    as a new Notion Task row linked back to it."""
    list_id = e2e_sync.get_default_tasklist_id()
    task = e2e_sync.gtasks.tasks().insert(
        tasklist=list_id, body={"title": "E2E: created in Google", "status": "needsAction"}
    ).execute()
    try:
        e2e_sync.sync()
        pages = e2e_sync.get_notion_pages()
        matches = [p for p in pages if e2e_sync.notion_gtask_id(p) == task["id"]]
        assert len(matches) == 1
        assert e2e_sync.notion_title(matches[0]) == "E2E: created in Google"
    finally:
        e2e_sync.gtasks.tasks().delete(tasklist=list_id, task=task["id"]).execute()


def test_editing_both_sides_before_a_sync_makes_notion_win(e2e_sync):
    """Given a linked task is edited on both Notion and Google before the next sync() runs,
    the Notion edit wins on Google (per CLAUDE.md's hardcoded conflict rule)."""
    page = e2e_sync.notion.pages.create(
        parent={"database_id": e2e_sync.NOTION_DATABASE_ID},
        properties={
            e2e_sync.PROP_TITLE: {"title": [{"text": {"content": "E2E: before edits"}}]},
        },
    )
    try:
        e2e_sync.sync()  # first pass links it
        page = e2e_sync.notion.pages.retrieve(page_id=page["id"])
        gtask_id = e2e_sync.notion_gtask_id(page)
        list_id = e2e_sync.get_default_tasklist_id()

        e2e_sync.notion.pages.update(
            page_id=page["id"],
            properties={e2e_sync.PROP_TITLE: {"title": [{"text": {"content": "Notion edit"}}]}},
        )
        e2e_sync.gtasks.tasks().patch(
            tasklist=list_id, task=gtask_id, body={"title": "Google edit"}
        ).execute()
        time.sleep(1)  # ensure both edits sort after the first sync's last_sync timestamp

        e2e_sync.sync()

        task = e2e_sync.gtasks.tasks().get(tasklist=list_id, task=gtask_id).execute()
        assert task["title"] == "Notion edit"
    finally:
        e2e_sync.notion.pages.update(page_id=page["id"], archived=True)
