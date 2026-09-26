"""Unit tests for sync.py's read-only Notion property accessors
(notion_title, notion_due_date, notion_status, notion_sync_as,
notion_gcal_id, notion_gtask_id, notion_course_page_ids, rich_text_plain).

These are pure functions over a page dict with no I/O, so a mismatch here
means a Notion property was renamed without updating the matching PROP_*
constant (CLAUDE.md's "Gotchas" #1) and the read is silently returning
None/empty instead of erroring.
"""

from tests.fakes import make_task_page


def test_notion_title_joins_multiple_title_fragments():
    """Given a title split across multiple rich-text fragments, notion_title concatenates them."""
    import sync

    page = make_task_page("p1", title="Read chapter 1")
    page["properties"]["Task name"]["title"] = [
        {"plain_text": "Read "},
        {"plain_text": "chapter 1"},
    ]
    assert sync.notion_title(page) == "Read chapter 1"


def test_notion_title_defaults_to_untitled_when_empty():
    """Given no title fragments at all, notion_title falls back to "(untitled)"."""
    import sync

    page = make_task_page("p1")
    page["properties"]["Task name"]["title"] = []
    assert sync.notion_title(page) == "(untitled)"


def test_notion_due_date_truncates_to_date_only():
    """Given a date property with a full timestamp, notion_due_date keeps only YYYY-MM-DD."""
    import sync

    page = make_task_page("p1")
    page["properties"]["Due date"] = {"date": {"start": "2026-03-04T15:30:00.000Z"}}
    assert sync.notion_due_date(page) == "2026-03-04"


def test_notion_due_date_is_none_when_date_property_unset():
    """Given no due date set on the page, notion_due_date returns None (tasks may lack one)."""
    import sync

    page = make_task_page("p1", due=None)
    assert sync.notion_due_date(page) is None


def test_notion_status_reads_status_name():
    """Given a Status select of "Done", notion_status returns "Done"."""
    import sync

    page = make_task_page("p1", status="Done")
    assert sync.notion_status(page) == "Done"


def test_notion_status_is_none_when_unset():
    """Given no Status value, notion_status returns None rather than a default."""
    import sync

    page = make_task_page("p1", status=None)
    assert sync.notion_status(page) is None


def test_notion_sync_as_reads_select_name():
    """Given Sync As = "Event", notion_sync_as returns "Event"."""
    import sync

    page = make_task_page("p1", sync_as="Event")
    assert sync.notion_sync_as(page) == "Event"


def test_notion_sync_as_is_none_when_empty():
    """Given an empty Sync As select, notion_sync_as returns None (later defaulted to Task by is_task_row)."""
    import sync

    page = make_task_page("p1", sync_as=None)
    assert sync.notion_sync_as(page) is None


def test_notion_gcal_id_reads_rich_text_id():
    """Given a Google Event ID rich_text value, notion_gcal_id returns its plain text."""
    import sync

    page = make_task_page("p1")
    page["properties"]["Google Event ID"] = {"rich_text": [{"plain_text": "evt-123"}]}
    assert sync.notion_gcal_id(page) == "evt-123"


def test_notion_gtask_id_is_none_when_never_linked():
    """Given a task page never synced to Google, notion_gtask_id returns None."""
    import sync

    page = make_task_page("p1", gtask_id=None)
    assert sync.notion_gtask_id(page) is None


def test_notion_course_page_ids_returns_all_related_ids():
    """Given multiple Course relations, notion_course_page_ids returns every related page id in order."""
    import sync

    page = make_task_page("p1", course_ids=["course-a", "course-b"])
    assert sync.notion_course_page_ids(page) == ["course-a", "course-b"]


def test_notion_course_page_ids_empty_when_no_course_set():
    """Given no Course relation set, notion_course_page_ids returns an empty list, not None."""
    import sync

    page = make_task_page("p1", course_ids=[])
    assert sync.notion_course_page_ids(page) == []
