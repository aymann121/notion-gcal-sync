// Webhook relay: Notion + Google Calendar change notifications -> GitHub
// repository_dispatch, which runs .github/workflows/sync.yml.
//
// Neither Notion nor Google can call GitHub's API directly (wrong body, no
// auth header), so this Worker verifies each notification and forwards it.
// It keeps no state: sync.yml's `concurrency: sync` group already collapses a
// burst of dispatches into one running + one pending run.

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("ok");
    const { pathname } = new URL(request.url);
    if (pathname === "/notion") return handleNotion(request, env, ctx);
    if (pathname === "/gcal") return handleGcal(request, env, ctx);
    return new Response("not found", { status: 404 });
  },
};

async function handleNotion(request, env, ctx) {
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

  if (!env.NOTION_VERIFICATION_TOKEN) {
    return new Response("verification token not configured", { status: 503 });
  }
  const expected = "sha256=" + (await hmacHex(env.NOTION_VERIFICATION_TOKEN, raw));
  const given = request.headers.get("X-Notion-Signature") || "";
  if (!safeEqual(given, expected)) {
    return new Response("bad signature", { status: 401 });
  }

  // sync.py's own writes (linking ids, mirroring Google edits) come back as
  // webhooks authored by this integration's bot. Dropping them keeps each
  // sync from triggering another, no-op sync.
  const authors = body.authors || [];
  if (
    env.NOTION_BOT_ID &&
    authors.length > 0 &&
    authors.every((a) => normalizeId(a.id) === normalizeId(env.NOTION_BOT_ID))
  ) {
    return new Response("ignored: own write");
  }

  ctx.waitUntil(dispatch(env, "notion-change", body.type));
  return new Response("ok");
}

async function handleGcal(request, env, ctx) {
  if (request.headers.get("X-Goog-Channel-Token") !== env.GCAL_CHANNEL_TOKEN) {
    return new Response("bad token", { status: 401 });
  }
  // "sync" is the handshake Google sends when a channel is created.
  const state = request.headers.get("X-Goog-Resource-State");
  if (state === "sync") return new Response("ok");

  ctx.waitUntil(dispatch(env, "gcal-change", state));
  return new Response("ok");
}

async function dispatch(env, eventType, detail) {
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "notion-gcal-sync-relay",
      },
      body: JSON.stringify({
        event_type: eventType,
        client_payload: { detail: detail || null },
      }),
    }
  );
  if (!res.ok) {
    console.error(`dispatch ${eventType} failed: ${res.status} ${await res.text()}`);
  } else {
    console.log(`dispatched ${eventType} (${detail})`);
  }
}

async function hmacHex(key, message) {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    enc.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function normalizeId(id) {
  return (id || "").replace(/-/g, "").toLowerCase();
}
