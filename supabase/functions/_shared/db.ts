// Postgres access for the sync functions: the RPCs defined in
// supabase/migrations/20260927145816_sync_tables.sql, called through
// PostgREST as service_role (both env vars are injected into every Edge
// Function by Supabase).

import type { Json } from "./clients.ts";
import type { SyncState } from "./sync_core.ts";

export interface SyncControl {
  enabled: boolean;
  dirty: boolean;
  locked_until: string;
  gtasks_cursor: string | null;
  gtasks_lists_sig: string | null;
  last_pass_at: string | null;
  last_error: string | null;
}

async function rpc(name: string, args: Record<string, Json> = {}): Promise<Json> {
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const headers: Record<string, string> = { apikey: key, "Content-Type": "application/json" };
  // Legacy service_role keys are JWTs and go in Authorization too; the newer
  // sb_secret_ keys are accepted from the apikey header alone.
  if (key.startsWith("eyJ")) headers.Authorization = `Bearer ${key}`;
  const res = await fetch(`${Deno.env.get("SUPABASE_URL")}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers,
    body: JSON.stringify(args),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`rpc ${name} failed: ${res.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

export const markDirty = (): Promise<void> => rpc("mark_dirty");

export const claimSyncLease = (ttlSeconds: number): Promise<boolean> =>
  rpc("claim_sync_lease", { ttl_seconds: ttlSeconds });

/** Returns dirty: true means another change arrived during the pass. */
export const releaseSyncLease = (error: string | null): Promise<boolean> =>
  rpc("release_sync_lease", { error });

export const getSyncControl = (): Promise<SyncControl> => rpc("get_sync_control");

export const setGtasksPoll = (cursor: string | null, listsSig: string): Promise<void> =>
  rpc("set_gtasks_poll", { cursor, lists_sig: listsSig });

export const loadSyncState = (): Promise<SyncState> => rpc("load_sync_state");

export const saveSyncState = (state: SyncState, cursor: string | null): Promise<void> =>
  rpc("save_sync_state", { state, cursor });
