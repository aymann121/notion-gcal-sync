// Registers a Google Calendar push channel that pings the relay whenever an
// event on GOOGLE_CALENDAR_ID changes. Channels expire, so pg_cron calls this
// every 2 days with a 7-day TTL. Old channels are left to expire on their
// own: while two overlap each change pings twice, which the sync lease absorbs.
//
// No-ops until cutover (sync_control.enabled), while "cron (deprecated)/"'s
// gcal-watch.yml still renews the channel. POST ?force=1 skips that check.

import { hasCronSecret } from "../_shared/auth.ts";
import { gcalClient, googleAccessToken } from "../_shared/clients.ts";
import { getSyncControl } from "../_shared/db.ts";

const CHANNEL_TTL_SECONDS = 7 * 24 * 3600;

Deno.serve(async (request) => {
  if (!hasCronSecret(request)) return new Response("unauthorized", { status: 401 });

  const force = new URL(request.url).searchParams.get("force") === "1";
  if (!force && !(await getSyncControl()).enabled) {
    return new Response("skipped: sync not enabled");
  }

  const calendarId = Deno.env.get("GOOGLE_CALENDAR_ID") || "primary";
  const accessToken = await googleAccessToken(Deno.env.get("GOOGLE_TOKEN_JSON")!);
  const channel = await gcalClient(accessToken, calendarId).watch({
    id: crypto.randomUUID(),
    type: "web_hook",
    address: `${Deno.env.get("SUPABASE_URL")}/functions/v1/relay/gcal`,
    token: Deno.env.get("GCAL_CHANNEL_TOKEN"),
    params: { ttl: String(CHANNEL_TTL_SECONDS) },
  });
  const message = `Watching ${calendarId}: channel ${channel.id}, expires ${channel.expiration} (ms since epoch)`;
  console.log(message);
  return new Response(message);
});
