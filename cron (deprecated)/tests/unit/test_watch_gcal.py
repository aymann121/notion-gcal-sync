"""Unit tests for watch_gcal.watch_calendar — the Calendar push-channel
registration the relay Worker depends on. A wrong address or token here means
Calendar edits silently stop triggering syncs until the fallback cron runs.
"""

import watch_gcal


class _Recorder:
    def __init__(self):
        self.calls = []

    def events(self):
        return self

    def watch(self, calendarId, body):
        self.calls.append((calendarId, body))
        return self

    def execute(self):
        return {"id": self.calls[-1][1]["id"], "expiration": "0"}


def test_watch_points_channel_at_relay_gcal_route(monkeypatch):
    """Given a relay URL, the channel posts to its /gcal route with our token."""
    recorder = _Recorder()
    monkeypatch.setattr(watch_gcal.sync, "gcal", recorder)

    watch_gcal.watch_calendar("https://relay.example.workers.dev/", "secret")

    calendar_id, body = recorder.calls[0]
    assert calendar_id == watch_gcal.sync.GOOGLE_CALENDAR_ID
    assert body["type"] == "web_hook"
    assert body["address"] == "https://relay.example.workers.dev/gcal"
    assert body["token"] == "secret"
    assert body["params"] == {"ttl": str(7 * 24 * 3600)}


def test_each_registration_uses_a_fresh_channel_id(monkeypatch):
    """Google rejects a reused channel id, so every renewal needs a new one."""
    recorder = _Recorder()
    monkeypatch.setattr(watch_gcal.sync, "gcal", recorder)

    watch_gcal.watch_calendar("https://r", "t")
    watch_gcal.watch_calendar("https://r", "t")

    assert recorder.calls[0][1]["id"] != recorder.calls[1][1]["id"]
