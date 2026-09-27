"""Integration tests for ensure_tasklist/get_default_tasklist_id and
ensure_course/get_courses_database_id: the create-vs-reuse caching logic
that decides whether a Google Tasks list or a Notion Course page gets
created fresh or reused across a run.
"""

import sync
from tests.fakes import DEFAULT_TASKLIST_ID


def test_get_default_tasklist_id_resolves_the_default_alias(fake_clients):
    """Given Google's "@default" alias, get_default_tasklist_id resolves and caches its real id."""
    assert sync.get_default_tasklist_id() == DEFAULT_TASKLIST_ID


def test_ensure_tasklist_reuses_existing_list_by_title(fake_clients):
    """Given a Google Tasks list already named "History", ensure_tasklist returns its existing id
    rather than creating a duplicate."""
    fake_clients["gtasks"].add_tasklist("history-list-id", "History")

    result = sync.ensure_tasklist("History")

    assert result == "history-list-id"
    assert len(fake_clients["gtasks"]._tasklists) == 2  # default + History, no new one created


def test_ensure_tasklist_creates_a_new_list_when_none_matches(fake_clients):
    """Given no Google Tasks list named "Physics" exists yet, ensure_tasklist creates one and
    returns its new id."""
    result = sync.ensure_tasklist("Physics")

    titles = {tl["title"] for tl in fake_clients["gtasks"]._tasklists.values()}
    assert "Physics" in titles
    assert fake_clients["gtasks"]._tasklists[result]["title"] == "Physics"


def test_ensure_tasklist_caches_across_calls_in_the_same_run(fake_clients):
    """Given ensure_tasklist("Physics") was already called once, a second call in the same run
    returns the same id without inserting a second list."""
    first = sync.ensure_tasklist("Physics")
    second = sync.ensure_tasklist("Physics")

    assert first == second
    physics_lists = [tl for tl in fake_clients["gtasks"]._tasklists.values() if tl["title"] == "Physics"]
    assert len(physics_lists) == 1


def test_ensure_course_reuses_existing_course_page_by_title(fake_clients):
    """Given a Notion Course page already titled "Chemistry", ensure_course returns its existing
    page id rather than creating a duplicate page."""
    courses_db_id = fake_clients["notion"].register_default_course_schema(sync.NOTION_DATABASE_ID)
    existing = fake_clients["notion"].add_page(
        {"id": "existing-course", "properties": {"Name": {"type": "title", "title": [{"plain_text": "Chemistry"}]}}},
        database_id=courses_db_id,
    )

    result = sync.ensure_course("Chemistry")

    assert result == existing["id"]
    assert len(fake_clients["notion"]._pages) == 1


def test_ensure_course_creates_a_new_course_page_when_none_matches(fake_clients):
    """Given no Course page titled "Art History" exists yet, ensure_course creates one in the
    Courses database and returns its new page id."""
    result = sync.ensure_course("Art History")

    created = fake_clients["notion"]._pages[result]
    title_fragments = created["properties"]["Name"]["title"]
    assert "".join(f["plain_text"] for f in title_fragments) == "Art History"
