// Webhook relay: Notion + Google Calendar change notifications -> the `sync`
// Edge Function. Verifies each notification, then pokes `sync`, whose lease
// and dirty flag collapse a burst of webhooks into one running + one pending
// pass.
//
// During the migration it can also still forward to GitHub
// repository_dispatch (the "cron (deprecated)/" version's sync.yml). That stays on
// until FORWARD_TO_GITHUB is set to "false" by the cutover runbook in
// "cron (deprecated)/README.md". `sync` itself no-ops until cutover, so only one side
// ever writes.
//
// Deployed with verify_jwt = false (supabase/config.toml): Notion and Google
// can't send a Supabase JWT, so the HMAC / channel-token checks below are the
// authentication.

import { invokeFunction, runInBackground } from "../_shared/auth.ts";

const env = (name: string) => Deno.env.get(name) ?? "";

Deno.serve((request) => {
  if (request.method !== "POST") return new Response("ok");
  // Supabase passes the path with the function name, e.g. /relay/notion.
  const { pathname } = new URL(request.url);
  if (pathname.endsWith("/notion")) return handleNotion(request);
  if (pathname.endsWith("/gcal")) return handleGcal(request);
  return new Response("not found", { status: 404 });
});

async function handleNotion(request: Request): Promise<Response> {
  const raw = await request.text();
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return new Response("bad json", { status: 400 });
  }

  // One-time subscription handshake: Notion POSTs a token that must be pasted
  // back into the integration's Webhooks tab, then stored as the
  // NOTION_VERIFICATION_TOKEN secret (it's also the signing key from then on).
  if (body.verification_token) {
    console.log(`Notion verification_token: ${body.verification_token}`);
    return new Response("ok");
  }

  const verificationToken = env("NOTION_VERIFICATION_TOKEN");
  if (!verificationToken) {
    return new Response("verification token not configured", { status: 503 });
  }
  const expected = "sha256=" + (await hmacHex(verificationToken, raw));
  const given = request.headers.get("X-Notion-Signature") || "";
  if (!safeEqual(given, expected)) {
    return new Response("bad signature", { status: 401 });
  }

  // The sync's own writes (linking ids, mirroring Google edits) come back as
  // webhooks authored by this integration's bot. Dropping them keeps each
  // sync from triggering another, no-op sync.
  const botId = env("NOTION_BOT_ID");
  const authors: { id?: string }[] = body.authors || [];
  if (
    botId &&
    authors.length > 0 &&
    authors.every((a) => normalizeId(a.id) === normalizeId(botId))
  ) {
    return new Response("ignored: own write");
  }

  forward("notion-change", body.type);
  return new Response("ok");
}

function handleGcal(request: Request): Response {
  const channelToken = env("GCAL_CHANNEL_TOKEN");
  if (!channelToken || request.headers.get("X-Goog-Channel-Token") !== channelToken) {
    return new Response("bad token", { status: 401 });
  }
  // "sync" is the handshake Google sends when a channel is created.
  const state = request.headers.get("X-Goog-Resource-State");
  if (state === "sync") return new Response("ok");

  forward("gcal-change", state);
  return new Response("ok");
}

/** Start a sync pass, and during the migration also the GitHub workflow. */
function forward(eventType: string, detail: string | null) {
  runInBackground(invokeFunction("sync"));
  if (env("FORWARD_TO_GITHUB") !== "false") runInBackground(dispatch(eventType, detail));
}

// GitHub repository_dispatch for the "cron (deprecated)/" version. Remove once the
// cutover is done and FORWARD_TO_GITHUB is off for good.

async function dispatch(eventType: string, detail: string | null) {
  const res = await fetch(
    `https://api.github.com/repos/${env("GITHUB_REPO")}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env("GITHUB_TOKEN")}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "notion-gcal-sync-relay",
      },
      body: JSON.stringify({
        event_type: eventType,
        client_payload: { detail: detail || null },
      }),
    },
  );
  if (!res.ok) {
    console.error(`dispatch ${eventType} failed: ${res.status} ${await res.text()}`);
  } else {
    console.log(`dispatched ${eventType} (${detail})`);
  }
}

async function hmacHex(key: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    enc.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function normalizeId(id: string | undefined): string {
  return (id || "").replace(/-/g, "").toLowerCase();
}
