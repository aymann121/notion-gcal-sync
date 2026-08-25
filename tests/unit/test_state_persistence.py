"""Unit tests for load_state / save_state — the only persisted state in this
project (CLAUDE.md's Architecture #2). Uses the `tmp_state_file` fixture so
these tests never read or write the real sync_state.json checked into the
repo root.
"""

import json

import sync


def test_load_state_returns_empty_dict_when_file_missing(tmp_state_file):
    """Given no sync_state.json exists yet (first-ever run), load_state returns {} rather than raising."""
    assert not tmp_state_file.exists()
    assert sync.load_state() == {}


def test_save_state_then_load_state_round_trips(tmp_state_file):
    """Given a state dict is saved, loading it back returns an equal dict."""
    state = {
        "page-1": {"last_sync": "2026-01-01T00:00:00+00:00", "kind": "task", "task_id": "t1"},
        "_ignored_task_ids": ["t2", "t3"],
    }
    sync.save_state(state)
    assert sync.load_state() == state


def test_save_state_writes_readable_indented_json(tmp_state_file):
    """Given a state dict is saved, the file on disk is valid, human-diffable JSON (matches the committed-to-git use case)."""
    sync.save_state({"page-1": {"kind": "task"}})
    raw = tmp_state_file.read_text()
    assert json.loads(raw) == {"page-1": {"kind": "task"}}
    assert "\n" in raw  # indent=2 formatting, not a single minified line
