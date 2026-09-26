"""Unit tests for parse_dt / utc_now_iso / last_sync_dt — the timestamp
plumbing the conflict-resolution logic in sync_event_pages/sync_task_pages
depends on entirely. A bug here (e.g. comparing naive vs aware datetimes,
or getting the epoch fallback wrong) would silently corrupt every
conflict decision in the sync, so these are tested in isolation first.
"""

import datetime

from freezegun import freeze_time

import sync


def test_parse_dt_handles_z_suffix_as_utc():
    """Given an ISO timestamp ending in "Z", parse_dt treats it as UTC."""
    dt = sync.parse_dt("2026-01-01T12:00:00.000Z")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.timedelta(0)
    assert dt.hour == 12


def test_parse_dt_converts_non_utc_offset_to_utc():
    """Given a timestamp with an explicit non-UTC offset, parse_dt normalizes it to UTC."""
    dt = sync.parse_dt("2026-01-01T12:00:00+05:00")
    assert dt.tzinfo == datetime.timezone.utc
    assert dt.hour == 7  # 12:00+05:00 == 07:00 UTC


def test_parse_dt_treats_naive_timestamp_as_utc():
    """Given a timestamp with no timezone info, parse_dt assumes it's already UTC rather than raising."""
    dt = sync.parse_dt("2026-01-01T12:00:00")
    assert dt.tzinfo == datetime.timezone.utc
    assert dt.hour == 12


@freeze_time("2026-06-15T10:00:00Z")
def test_utc_now_iso_returns_current_utc_time_as_iso_string():
    """Given the system clock is frozen at a known instant, utc_now_iso returns that instant as an ISO 8601 string."""
    now = sync.parse_dt(sync.utc_now_iso())
    assert now == datetime.datetime(2026, 6, 15, 10, 0, 0, tzinfo=datetime.timezone.utc)


def test_last_sync_dt_returns_epoch_when_page_never_synced():
    """Given a page id absent from state, last_sync_dt returns the minimum possible datetime, so any real
    edit timestamp counts as "changed since last sync"."""
    dt = sync.last_sync_dt({}, "never-synced-page")
    assert dt == datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def test_last_sync_dt_reads_recorded_last_sync():
    """Given a page with a recorded last_sync in state, last_sync_dt parses and returns it."""
    state = {"page-1": {"last_sync": "2026-01-01T00:00:00+00:00"}}
    dt = sync.last_sync_dt(state, "page-1")
    assert dt == datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
