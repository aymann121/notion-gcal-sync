# Test suite for notion-gcal-sync

Three layers, from fast/pure to slow/real:

```
tests/
  conftest.py       # env-var + fake-client fixtures shared by all layers
  fakes.py          # hand-written in-memory Notion/Calendar/Tasks fakes
  unit/             # pure-logic functions, no I/O, no mocking
  integration/      # sync_event_pages/sync_task_pages/sync() against fakes.py
  e2e/              # opt-in, hits real Notion + Google accounts
```

## Running the tests

```bash
pip install -r requirements.txt -r requirements-dev.txt

pytest                       # unit + integration (e2e excluded by pytest.ini's addopts)
pytest tests/unit            # just the pure-logic layer
pytest tests/integration     # just the fake-client layer
pytest -m e2e tests/e2e -v   # e2e layer only, see below
```

## Layer 1: `tests/unit/`

Tests functions with no I/O at all — property readers (`notion_title`,
`notion_due_date`, ...), status/due-date format conversions, timestamp
parsing, and the Course-relation schema lookup. These run in milliseconds
and need nothing beyond the dummy env vars `conftest.py` sets before
importing `sync.py`.

## Layer 2: `tests/integration/`

Tests `sync_event_pages`, `sync_task_pages`, `import_unlinked_gtasks`, and
`sync()` against `fakes.py`'s `FakeNotionClient`/`FakeGCalClient`/
`FakeGTasksClient` — in-memory stand-ins that implement only the call
shapes `sync.py` actually uses. The `fake_clients` fixture (in
`conftest.py`) installs fresh instances onto the `sync` module before each
test via `monkeypatch`.

This is where the project's riskiest logic lives and is tested most
thoroughly: the 4-way conflict matrix (Notion-only changed / Google-only
changed / both changed → "Notion wins" / neither changed) crossed with
tasks vs. events and with `DELETE_SYNC` True/False. See
`test_task_conflict_resolution.py` and `test_event_conflict_resolution.py`.

**Known limitation of `fakes.py`**: pagination isn't simulated (every list
call returns everything in one page), and the fakes don't reproduce every
real-API error shape — they raise a generic `NotFound` wherever `sync.py`
only checks "did this raise at all." If you extend `sync.py` to depend on
paginated responses or a specific exception type, extend `fakes.py`
alongside it rather than reaching for `unittest.mock.MagicMock` mid-test.

## Layer 3: `tests/e2e/`

Runs `sync.py` against **real** Notion and Google accounts to verify a full
round trip end-to-end. These are excluded by default (`pytest.ini`'s
`addopts = -m "not e2e"`) and gated by an explicit `E2E_ENABLE=1` env var on
top of the marker, so they can never run by accident.

**Before running these**, set up disposable test resources — a throwaway
Notion "Tasks Tracker" database and a non-primary Google Calendar/Tasks
account — and export:

```bash
export NOTION_TOKEN=...              # same as production
export GOOGLE_TOKEN_JSON=...         # same as production
export E2E_ENABLE=1
export E2E_NOTION_DATABASE_ID=...    # a DISPOSABLE database, never production
export E2E_GOOGLE_CALENDAR_ID=...    # a DISPOSABLE calendar, never "primary"
pytest -m e2e tests/e2e -v
```

**Never point these at real production data.** A bug in conflict
resolution or `DELETE_SYNC` handling could delete or duplicate real rows;
that's precisely the failure mode this layer exists to catch early, using
throwaway resources instead of your actual Tasks Tracker. This layer is
intentionally not wired into CI for the same reason — run it manually
before/after changes that touch the conflict-resolution or deletion logic.
