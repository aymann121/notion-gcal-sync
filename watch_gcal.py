#!/usr/bin/env python3
"""
Register a Google Calendar push channel that pings the relay Worker
(relay/worker.js) whenever an event on GOOGLE_CALENDAR_ID changes.

Channels expire, so .github/workflows/gcal-watch.yml re-runs this every few
days. Old channels are left to expire on their own: while two overlap, each
change pings twice, which sync.yml's concurrency group absorbs.

Env: everything sync.py needs, plus RELAY_URL and GCAL_CHANNEL_TOKEN.
"""

import os
import uuid

import sync

# Renewed every 5 days by gcal-watch.yml, so a 7-day channel never lapses.
CHANNEL_TTL_SECONDS = 7 * 24 * 3600


def watch_calendar(relay_url, channel_token):
    """Open a new push channel; returns Google's channel resource."""
    body = {
        "id": str(uuid.uuid4()),
        "type": "web_hook",
        "address": relay_url.rstrip("/") + "/gcal",
        "token": channel_token,
        "params": {"ttl": str(CHANNEL_TTL_SECONDS)},
    }
    return (
        sync.gcal.events()
        .watch(calendarId=sync.GOOGLE_CALENDAR_ID, body=body)
        .execute()
    )


if __name__ == "__main__":
    channel = watch_calendar(os.environ["RELAY_URL"], os.environ["GCAL_CHANNEL_TOKEN"])
    print(
        f"Watching {sync.GOOGLE_CALENDAR_ID}: channel {channel.get('id')}, "
        f"expires {channel.get('expiration')} (ms since epoch)"
    )
