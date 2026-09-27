"""Unit tests for is_not_found — the guard that decides whether a failed
lookup means "this object was deleted" or "the API had a bad moment".

With DELETE_SYNC on, that distinction is destructive: a swallowed error is a
deletion order, so anything that isn't a genuine 404/410 has to propagate and
fail the run instead of looking like a missing object.
"""

import httpx
import pytest
from googleapiclient.errors import HttpError
from notion_client import APIErrorCode, APIResponseError

import sync
from tests.fakes import _FakeStatus


def google_error(status):
    return HttpError(_FakeStatus(status), b"boom")


def notion_error(code, status=400):
    return APIResponseError(httpx.Response(status), "boom", code)


@pytest.mark.parametrize("status", [404, 410])
def test_google_404_and_410_are_not_found(status):
    """Given Google reports the object gone, is_not_found says so."""
    assert sync.is_not_found(google_error(status)) is True


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_google_errors_are_not_treated_as_deletions(status):
    """Given a rate-limit or server error from Google, is_not_found refuses to call it a deletion."""
    assert sync.is_not_found(google_error(status)) is False


def test_notion_object_not_found_is_not_found():
    """Given Notion's object_not_found code, is_not_found says the page is really gone."""
    assert sync.is_not_found(notion_error(APIErrorCode.ObjectNotFound, 404)) is True


@pytest.mark.parametrize(
    "code", [APIErrorCode.RateLimited, APIErrorCode.InternalServerError, APIErrorCode.ServiceUnavailable]
)
def test_transient_notion_errors_are_not_treated_as_deletions(code):
    """Given Notion is rate-limiting or erroring, is_not_found refuses to call it a deletion."""
    assert sync.is_not_found(notion_error(code, 500)) is False


def test_unrelated_exceptions_are_not_treated_as_deletions():
    """Given something that isn't an SDK error at all (a dropped connection, a bug),
    is_not_found defaults to "not a deletion"."""
    assert sync.is_not_found(ConnectionError("network down")) is False
