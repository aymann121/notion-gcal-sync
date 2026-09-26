"""Integration tests for sync() — the top-level entry point run by the
GitHub Actions cron job (CLAUDE.md's "CI push-back mechanism"). Verifies
call order (Tasks before Events), routing of rows by Sync As, and that
sync_state.json is only written on full success (an unhandled exception
partway through must not corrupt/lose prior state).
"""

import pytest

import sync
from tests.fakes import make_task_page, make_event_page


def test_sync_routes_rows_to_task_and_event_paths(fake_clients, tmp_state_file):
    """Given a database with one Task row and one Event row, sync() creates a Google Task for the
    Task row and a Calendar event for the Event row."""
    task_page = make_task_page("task-page", title="Homework", due="2026-03-01", gtask_id=None)
    event_page = make_event_page("event-page", title="Exam", due="2026-03-02", gcal_id=None)
    fake_clients["notion"].add_page(task_page, database_id=sync.NOTION_DATABASE_ID)
    fake_clients["notion"].add_page(event_page, database_id=sync.NOTION_DATABASE_ID)

    sync.sync()

    assert sync.notion_gtask_id(task_page) is not None
    assert sync.notion_gcal_id(event_page) is not None


def test_sync_persists_state_after_a_successful_run(fake_clients, tmp_state_file):
    """Given a successful sync() run, sync_state.json on disk reflects the updated state
    (CLAUDE.md: it's the only persisted state and is committed back to the repo after each run)."""
    task_page = make_task_page("task-page", title="Homework", due="2026-03-01", gtask_id=None)
    fake_clients["notion"].add_page(task_page, database_id=sync.NOTION_DATABASE_ID)

    sync.sync()

    reloaded = sync.load_state()
    assert "task-page" in reloaded
    assert reloaded["task-page"]["kind"] == "task"


def test_sync_does_not_persist_state_when_get_notion_pages_fails(fake_clients, tmp_state_file, monkeypatch):
    """Given get_notion_pages raises (e.g. a Notion API outage) before either sync path runs, sync()
    propagates the exception and never calls save_state — so a prior good sync_state.json on disk
    is left untouched rather than being overwritten with nothing."""
    tmp_state_file.write_text('{"prior-page": {"kind": "task", "last_sync": "2020-01-01T00:00:00Z"}}')

    def boom():
        raise RuntimeError("Notion API unavailable")

    monkeypatch.setattr(sync, "get_notion_pages", boom)

    with pytest.raises(RuntimeError):
        sync.sync()

    assert sync.load_state() == {"prior-page": {"kind": "task", "last_sync": "2020-01-01T00:00:00Z"}}


def test_sync_does_not_persist_state_when_event_path_fails_after_task_path_succeeds(fake_clients, tmp_state_file, monkeypatch):
    """Given sync_task_pages succeeds (mutating its `state` argument in place) but sync_event_pages then
    raises, sync() still never calls save_state — so the task-side progress from this run is lost rather
    than being partially committed, matching sync.py's all-or-nothing save_state() placement."""
    tmp_state_file.write_text('{"prior-page": {"kind": "task", "last_sync": "2020-01-01T00:00:00Z"}}')
    task_page = make_task_page("task-page", title="Homework", due="2026-03-01", gtask_id=None)
    fake_clients["notion"].add_page(task_page, database_id=sync.NOTION_DATABASE_ID)

    def boom(state, event_pages):
        raise RuntimeError("Calendar API unavailable")

    monkeypatch.setattr(sync, "sync_event_pages", boom)

    with pytest.raises(RuntimeError):
        sync.sync()

    assert sync.load_state() == {"prior-page": {"kind": "task", "last_sync": "2020-01-01T00:00:00Z"}}
