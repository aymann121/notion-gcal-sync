// Runs the Notion ↔ Google sync (_shared/sync_core.ts) against the state in
// Postgres. Called by the relay on every webhook, by the Google Tasks poller
// when it sees a change, and daily by pg_cron as a safety net.
//
// Every call marks the sync dirty, then answers 202 at once and works in the
// background. Only one pass runs at a time (the sync_control lease): a call
// that arrives mid-pass just leaves dirty set, and the running pass goes
// round again when it finishes. That replaces the GitHub `concurrency` group
// (one running + one pending).
//
// POST ?dry_run=1 instead runs a single pass synchronously with writes logged
// and skipped, and saves nothing. It works before cutover (enabled = false),
// which is what it's for: checking the port against production.

import { hasCronSecret, runInBackground } from "../_shared/auth.ts";
import { clientsFromEnv, dryRunClients } from "../_shared/clients.ts";
import {
  claimSyncLease,
  loadSyncState,
  markDirty,
  releaseSyncLease,
  saveSyncState,
} from "../_shared/db.ts";
import { runSyncPass, type SyncConfig } from "../_shared/sync_core.ts";

// A pass takes seconds; the lease outlives the 150 s wall-clock limit so it
// can only expire once the function holding it is certainly dead.
const LEASE_SECONDS = 160;
// Stay well inside the wall-clock limit; the poller restarts leftover work.
const MAX_PASSES = 3;

const config = (): SyncConfig => ({ notionDatabaseId: Deno.env.get("NOTION_DATABASE_ID")! });

Deno.serve(async (request) => {
  if (!hasCronSecret(request)) return new Response("unauthorized", { status: 401 });

  if (new URL(request.url).searchParams.get("dry_run") === "1") {
    const writes: string[] = [];
    const clients = dryRunClients(await clientsFromEnv(), writes);
    const state = await loadSyncState();
    const result = await runSyncPass(clients, config(), state);
    return Response.json({
      stateEntriesBefore: Object.keys(state).length,
      stateEntriesAfter: Object.keys(result.state).length,
      writes,
    });
  }

  await markDirty();
  runInBackground(drain());
  return new Response("accepted", { status: 202 });
});

/** Run passes while there is dirty work and we hold the lease. */
async function drain() {
  for (let i = 0; i < MAX_PASSES; i++) {
    // False when sync is disabled or another pass holds the lease (it will
    // see dirty and go again).
    if (!(await claimSyncLease(LEASE_SECONDS))) return;

    let error: string | null = null;
    try {
      const state = await loadSyncState();
      const result = await runSyncPass(await clientsFromEnv(), config(), state);
      await saveSyncState(result.state, result.startedAt);
      console.log(`pass ok: ${Object.keys(result.state).length} state entries`);
    } catch (e) {
      error = e instanceof Error ? e.stack ?? e.message : String(e);
      console.error(`pass failed: ${error}`);
    }

    const dirty = await releaseSyncLease(error);
    if (error || !dirty) return;
  }
}
