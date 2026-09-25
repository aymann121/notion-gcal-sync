# Webhook relay

A Supabase Edge Function that turns Notion webhooks and Google Calendar push
notifications into GitHub `repository_dispatch` events, which run
`.github/workflows/sync.yml`. Neither service can call GitHub's API directly,
so this sits in between, checks each request is genuine, and forwards it.

URL: `https://nwdulegrvumwcygvohhd.supabase.co/functions/v1/relay` (this is
`RELAY_URL`).

Routes:

- `POST /relay/notion`: handles the subscription handshake, then checks the
  `X-Notion-Signature` HMAC on each event. Events whose authors are all the sync
  integration's own bot (`NOTION_BOT_ID`) are dropped, so a sync's own writes
  don't trigger another sync.
- `POST /relay/gcal`: checks `X-Goog-Channel-Token` and ignores the `sync`
  handshake ping.

It must be deployed with JWT verification off (`supabase/config.toml` sets
`verify_jwt = false`). Notion and Google can't send a Supabase JWT, so with
verification on, every webhook gets a 401.

The project is on Supabase's free plan, which pauses after about 7 days
without database activity, and the relay never queries the database. So each
`sync.yml` run calls the `public.keepalive()` SQL function
(`supabase/migrations/`). If Supabase ever emails a pause warning, check that
step's output in the Actions logs.

Google Tasks has no push API. Its edits are picked up only by `sync.yml`'s
fallback cron.

## One-time setup

1. **Deploy**
   ```bash
   brew install supabase/tap/supabase
   supabase login
   supabase functions deploy relay     # from the repo root; reads supabase/config.toml
   ```
2. **Secrets.** Set these in the dashboard (Edge Functions → Secrets), or with
   `supabase secrets set NAME=value`:
   - `GITHUB_TOKEN`: fine-grained PAT, this repo only, Contents read/write.
   - `GITHUB_REPO`: `aymann121/notion-gcal-sync`.
   - `GCAL_CHANNEL_TOKEN`: any random string (`openssl rand -hex 32`). It must
     match the GitHub repo secret of the same name.
   - `NOTION_BOT_ID`: with `NOTION_TOKEN` exported, run
     ```bash
     python -c "from notion_client import Client; import os; print(Client(auth=os.environ['NOTION_TOKEN']).users.me()['id'])"
     ```

   Also add the GitHub repo secrets `RELAY_URL` (the URL above) and
   `GCAL_CHANNEL_TOKEN`.
3. **Notion subscription.** Go to notion.so/profile/integrations, open the sync
   integration, then the Webhooks tab, and create a subscription:
   - URL: `<RELAY_URL>/notion`
   - Events: `page.created`, `page.properties_updated`, `page.deleted`,
     `page.undeleted`

   Notion POSTs a `verification_token` to the function. Find it in the
   function's logs (dashboard → Edge Functions → relay → Logs), paste it into
   Notion to verify, then store it as the `NOTION_VERIFICATION_TOKEN` secret.
4. **Calendar channel.** In the Actions tab, run **Renew Google Calendar push
   channel** once. After that it renews itself every 2 days.

## Debugging

- The function's logs show each request plus `dispatched …` or
  `dispatch … failed`.
- Bursts don't need debouncing here. `sync.yml`'s `concurrency: sync` group
  keeps at most one run going and one waiting, so extra dispatches collapse into
  the waiting run.
