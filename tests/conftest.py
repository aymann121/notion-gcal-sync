"""Shared fixtures for the sync.py test suite.

`sync.py` builds its Notion/Calendar/Tasks clients as module-level globals at
import time (see CLAUDE.md "Architecture" #4 and sync.py's `notion`/`gcal`/
`gtasks`), so importing the module at all requires real-looking env vars.
This conftest sets dummy env vars *before* the first `import sync` anywhere
in the test session, so no test file may import sync.py before this module
runs (pytest guarantees conftest.py loads first).

A dummy `GOOGLE_TOKEN_JSON` must include a far-future "expiry" — without one,
`Credentials.from_authorized_user_info` treats the token as already expired
and `get_google_credentials()` performs a *real* network refresh call before
any test even starts, which is both slow and impossible offline.
"""

import os
import sys
import json

os.environ.setdefault("NOTION_TOKEN", "test-notion-token")
os.environ.setdefault("NOTION_DATABASE_ID", "test-database-id")
os.environ.setdefault(
    "GOOGLE_TOKEN_JSON",
    json.dumps(
        {
            "token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
            "expiry": "2099-01-01T00:00:00Z",
        }
    ),
)

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync  # noqa: E402  (must import after env vars are set, see docstring)


@pytest.fixture(autouse=True)
def reset_module_caches():
    """Clear sync.py's module-level caches before every test.

    Functions like `ensure_course`, `ensure_tasklist`, and `course_title`
    memoize results in module globals (`_course_pages_cache`,
    `_tasklist_cache`, `_tasklist_titles`, `_courses_database_id`,
    `_course_title_cache`, `_default_tasklist_id`) so a real run only pays
    the lookup cost once. Left alone, a value cached by one test would leak
    into the next and hide bugs (or cause spurious failures) depending on
    test order.
    """
    sync._course_title_cache.clear()
    sync._courses_database_id = None
    sync._course_pages_cache = None
    sync._course_title_prop_name = None
    sync._tasklist_cache = None
    sync._tasklist_titles = None
    sync._default_tasklist_id = None
    yield


@pytest.fixture(autouse=True)
def restore_delete_sync():
    """Restore `sync.DELETE_SYNC` to its original value after every test.

    Several integration tests flip this module-level flag to exercise the
    destructive branches; without restoring it, a test that forgets to reset
    it (or fails before doing so) would silently change behavior for every
    test that runs afterward.
    """
    original = sync.DELETE_SYNC
    yield
    sync.DELETE_SYNC = original


@pytest.fixture
def fake_clients(monkeypatch):
    """Replace sync.notion/gcal/gtasks with in-memory fakes for a test.

    Returns the FakeNotionClient/FakeGCalClient/FakeGTasksClient instances
    (already installed on the sync module) so a test can both drive sync.py
    functions and assert on the fakes' recorded state.
    """
    from tests.fakes import FakeNotionClient, FakeGCalClient, FakeGTasksClient

    notion_fake = FakeNotionClient()
    gcal_fake = FakeGCalClient()
    gtasks_fake = FakeGTasksClient()
    notion_fake.register_default_course_schema(sync.NOTION_DATABASE_ID)

    monkeypatch.setattr(sync, "notion", notion_fake)
    monkeypatch.setattr(sync, "gcal", gcal_fake)
    monkeypatch.setattr(sync, "gtasks", gtasks_fake)

    return {"notion": notion_fake, "gcal": gcal_fake, "gtasks": gtasks_fake}


@pytest.fixture
def tmp_state_file(tmp_path, monkeypatch):
    """Point sync.STATE_FILE at a throwaway path so tests never touch the
    real sync_state.json checked into the repo."""
    path = tmp_path / "sync_state.json"
    monkeypatch.setattr(sync, "STATE_FILE", str(path))
    return path
