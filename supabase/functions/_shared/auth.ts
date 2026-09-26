// Shared-secret check for the functions only pg_cron, the relay and the
// poller call (sync, poll-gtasks, watch-gcal). They're deployed with
// verify_jwt = false, like the relay, so this header is the authentication.

export function hasCronSecret(request: Request): boolean {
  const expected = Deno.env.get("CRON_SECRET") ?? "";
  const given = request.headers.get("x-cron-secret") ?? "";
  if (!expected || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

/** Fire-and-forget POST to another function in this project, with the secret. */
export function invokeFunction(name: string): Promise<void> {
  return fetch(`${Deno.env.get("SUPABASE_URL")}/functions/v1/${name}`, {
    method: "POST",
    headers: { "x-cron-secret": Deno.env.get("CRON_SECRET") ?? "", "Content-Type": "application/json" },
    body: "{}",
  }).then(async (res) => {
    await res.body?.cancel();
    if (!res.ok) console.error(`invoke ${name} failed: ${res.status}`);
  });
}

// Supabase runtime global: keeps work running after the response is sent.
declare const EdgeRuntime: { waitUntil(promise: Promise<unknown>): void } | undefined;

export function runInBackground(promise: Promise<unknown>): void {
  const guarded = promise.catch((e) => console.error(e));
  if (typeof EdgeRuntime !== "undefined") EdgeRuntime.waitUntil(guarded);
}
