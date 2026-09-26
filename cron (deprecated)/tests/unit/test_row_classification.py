"""Unit tests for is_task_row / is_event_row — the routing logic that
decides whether a Notion row syncs to Google Tasks or Google Calendar,
per the CLAUDE.md "Sync As" contract."""

import sync
from tests.fakes import make_task_page


def test_empty_sync_as_defaults_to_task_row():
    """Given an empty Sync As select, is_task_row is True (empty defaults to Task)."""
    page = make_task_page("p1", sync_as=None)
    assert sync.is_task_row(page) is True
    assert sync.is_event_row(page) is False


def test_sync_as_task_is_a_task_row():
    """Given Sync As = "Task", is_task_row is True."""
    page = make_task_page("p1", sync_as="Task")
    assert sync.is_task_row(page) is True
    assert sync.is_event_row(page) is False


def test_sync_as_event_is_an_event_row_not_a_task_row():
    """Given Sync As = "Event", is_event_row is True and is_task_row is False."""
    page = make_task_page("p1", sync_as="Event")
    assert sync.is_event_row(page) is True
    assert sync.is_task_row(page) is False
