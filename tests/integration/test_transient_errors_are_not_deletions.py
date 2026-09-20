"""Integration tests for the destructive edge of DELETE_SYNC: a flaky API call
must never be mistaken for a deleted object.

Both "is it gone?" lookups used to swallow every exception, so a 500 or a
rate-limit was indistinguishable from a 404. That was survivable while
deletions weren't mirrored -- the worst case was a spurious skip. With
DELETE_SYNC on, the same hiccup would delete a live Google Task or archive a
live Notion page, so these paths must fail the run loudly instead.
"""

import httpx
import pytest
from googleapiclient.errors import HttpError
from notion_client import APIErrorCode, APIResponseError

import sync
from tests import fakes
from tests.fakes import DEFAULT_TASKLIST_ID, _Call, _FakeStatus, make_task_page, make_gtask


def test_transient_google_error_does_not_archive_the_notion_page(fake_clients, monkeypatch):
    """Given a linked task missing from the listing snapshot, and Google returning a 500 on the
    per-task lookup that follows, the run fails rather than reading the task as deleted and
    archiving the Notion page."""
    sync.DELETE_SYNC = True
    page = make_task_page("page-1", title="Still here", due="2026-09-01", gtask_id="task-1")
    fake_clients["notion"].add_page(page)
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "task-1", "tasklist_id": DEFAULT_TASKLIST_ID}}

    def exploding_get(self, tasklist, task):
        return _Call(lambda: (_ for _ in ()).throw(HttpError(_FakeStatus(500), b"backend error")))

    monkeypatch.setattr(fakes._FakeGTasks, "get", exploding_get)

    with pytest.raises(HttpError):
        sync.sync_task_pages(state, [page])

    assert page["archived"] is False


def test_transient_notion_error_does_not_delete_the_google_task(fake_clients, monkeypatch):
    """Given Notion is rate-limiting while the cleanup loop checks whether a page still exists,
    the run fails rather than reading the page as deleted and deleting its Google Task."""
    sync.DELETE_SYNC = True
    fake_clients["gtasks"].add_task(DEFAULT_TASKLIST_ID, make_gtask("task-1"))
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00Z", "kind": "task", "task_id": "task-1", "tasklist_id": DEFAULT_TASKLIST_ID}}

    def rate_limited(self, page_id):
        raise APIResponseError(httpx.Response(429), "rate limited", APIErrorCode.RateLimited)

    monkeypatch.setattr(fakes._FakeNotionPages, "retrieve", rate_limited)

    with pytest.raises(APIResponseError):
        sync.sync_task_pages(state, [])

    assert "task-1" in fake_clients["gtasks"]._tasks[DEFAULT_TASKLIST_ID]
    assert "page-1" in state  # entry survives, so the next run can retry the check
