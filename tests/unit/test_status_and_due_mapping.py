"""Unit tests for the Status <-> Google Tasks completion mapping
(status_to_gtasks / gtasks_to_status) and the due-date format conversion
between Notion's YYYY-MM-DD and the Tasks API's RFC3339
(due_to_gtasks / due_from_gtasks)."""

import sync


def test_status_done_maps_to_completed():
    """Given Notion Status "Done", status_to_gtasks returns "completed"."""
    assert sync.status_to_gtasks("Done") == "completed"


def test_any_other_status_maps_to_needs_action():
    """Given a Notion Status other than "Done" (e.g. "In progress"), status_to_gtasks returns "needsAction"."""
    assert sync.status_to_gtasks("In progress") == "needsAction"
    assert sync.status_to_gtasks(None) == "needsAction"


def test_gtasks_completed_maps_to_done():
    """Given a Google Task status of "completed", gtasks_to_status returns "Done"."""
    assert sync.gtasks_to_status("completed") == "Done"


def test_gtasks_needs_action_maps_to_not_started_not_in_progress():
    """Given a Google Task status of "needsAction", gtasks_to_status returns "Not started" (never "In progress")."""
    assert sync.gtasks_to_status("needsAction") == "Not started"


def test_due_to_gtasks_appends_midnight_utc_time():
    """Given a plain YYYY-MM-DD date, due_to_gtasks appends the RFC3339 midnight-UTC suffix the Tasks API requires."""
    assert sync.due_to_gtasks("2026-05-01") == "2026-05-01T00:00:00.000Z"


def test_due_to_gtasks_truncates_extra_precision():
    """Given a date string with extra precision, due_to_gtasks keeps only the first 10 characters."""
    assert sync.due_to_gtasks("2026-05-01T09:00:00Z") == "2026-05-01T00:00:00.000Z"


def test_due_to_gtasks_is_none_for_falsy_input():
    """Given no due date, due_to_gtasks returns None instead of a malformed timestamp."""
    assert sync.due_to_gtasks(None) is None
    assert sync.due_to_gtasks("") is None


def test_due_from_gtasks_extracts_date_only():
    """Given a Google Task with an RFC3339 due timestamp, due_from_gtasks returns just the date portion."""
    assert sync.due_from_gtasks({"due": "2026-05-01T00:00:00.000Z"}) == "2026-05-01"


def test_due_from_gtasks_is_none_when_task_has_no_due():
    """Given a Google Task with no due field, due_from_gtasks returns None."""
    assert sync.due_from_gtasks({}) is None
