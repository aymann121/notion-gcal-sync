#!/usr/bin/env python3
"""
Run this ONCE on your own computer (not in GitHub Actions) to authorize
this app against your Google account and produce token.json.

Prereqs:
  1. Go to https://console.cloud.google.com/ -> create a project (or use an existing one).
  2. Enable the "Google Calendar API" AND "Google Tasks API" for that project.
  3. Go to "APIs & Services" -> "Credentials" -> "Create Credentials" -> "OAuth client ID".
     - Application type: "Desktop app"
     - Download the JSON, save it as client_secret.json next to this script.
  4. Run: python get_google_token.py
  5. A browser window opens -> log in -> approve access.
  6. token.json is created in this folder. Copy its full contents into the
     GOOGLE_TOKEN_JSON GitHub secret (see README.md).
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)

with open("token.json", "w") as f:
    f.write(creds.to_json())

print("Saved token.json — copy its contents into your GOOGLE_TOKEN_JSON secret.")
