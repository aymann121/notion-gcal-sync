# Webhook relay

A Cloudflare Worker that turns Notion webhooks and Google Calendar push
notifications into GitHub `repository_dispatch` events, which run
`.github/workflows/sync.yml`. Neither service can call GitHub's API directly,
so this sits in between, checks each request is genuine, and forwards it.

Routes:

- `POST /notion`: handles the subscription handshake, then checks the
  `X-Notion-Signature` HMAC on each event. Events whose authors are all the sync
  integration's own bot (`NOTION_BOT_ID`) are dropped, so a sync's own writes
  don't trigger another sync.
- `POST /gcal`: checks `X-Goog-Channel-Token` and ignores the `sync` handshake
  ping.

Google Tasks has no push API. Its edits are picked up only by `sync.yml`'s
fallback cron.

## One-time setup

1. **Deploy**
   ```bash
   cd relay
   npx wrangler login
   npx wrangler deploy          # prints https://notion-gcal-sync-relay.<you>.workers.dev
   ```
2. **Bot id.** With `NOTION_TOKEN` exported, run:
   ```bash
   python -c "from notion_client import Client; import os; print(Client(auth=os.environ['NOTION_TOKEN']).users.me()['id'])"
   ```
   Put the printed id in `NOTION_BOT_ID` in `wrangler.toml`, then redeploy.
3. **Secrets**
   ```bash
   npx wrangler secret put GITHUB_TOKEN        # fine-grained PAT: this repo only, Contents read/write
   npx wrangler secret put GCAL_CHANNEL_TOKEN  # any random string, e.g. `openssl rand -hex 32`
   ```
   Add GitHub repo secrets `RELAY_URL` (the workers.dev URL) and
   `GCAL_CHANNEL_TOKEN` (the same random string).
4. **Notion subscription.** Go to notion.so/profile/integrations, open the sync
   integration, then the Webhooks tab, and create a subscription:
   - URL: `<RELAY_URL>/notion`
   - Events: `page.created`, `page.properties_updated`, `page.deleted`,
     `page.undeleted`

   Notion POSTs a `verification_token` to the Worker. Read it from
   `npx wrangler tail`, paste it into Notion to verify, then store it:
   `npx wrangler secret put NOTION_VERIFICATION_TOKEN`.
5. **Calendar channel.** In the Actions tab, run **Renew Google Calendar push
   channel** once. After that it renews itself every 5 days.

## Debugging

- `npx wrangler tail` shows each request plus `dispatched …` or
  `dispatch … failed`.
- Bursts don't need debouncing here. `sync.yml`'s `concurrency: sync` group
  keeps at most one run going and one waiting, so extra dispatches collapse into
  the waiting run.
