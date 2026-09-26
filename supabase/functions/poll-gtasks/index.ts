// The cheap Google Tasks poller, called every 10 minutes by pg_cron. Google Tasks
// has no push API, so this asks "anything updated since the cursor?" and only
// starts a sync pass when the answer is yes.
//
// The cursor is the start time of the last successful pass (set by sync) or
// of the last poll that found nothing. A pass's own writes to Google Tasks
// land after its start, so each pass that writes costs one extra no-op pass,
// the same as Calendar's push notifications.

import { hasCronSecret, invokeFunction, runInBackground } from "../_shared/auth.ts";
import { googleAccessToken, gtasksClient } from "../_shared/clients.ts";
import { getSyncControl, setGtasksPoll } from "../_shared/db.ts";
import { detectGtasksChanges } from "../_shared/gtasks_poll.ts";

// After a failed pass, wait this long before the poller retries. Webhooks
// still start passes straight away.
const FAILURE_BACKOFF_MS = 10 * 60 * 1000;

Deno.serve((request) => {
  if (!hasCronSecret(request)) return new Response("unauthorized", { status: 401 });
  runInBackground(poll());
  return new Response("accepted", { status: 202 });
});

async function poll() {
  const control = await getSyncControl();
  if (!control.enabled) return;

  const lastPass = control.last_pass_at ? Date.parse(control.last_pass_at) : 0;
  if (control.last_error && Date.now() - lastPass < FAILURE_BACKOFF_MS) return;

  // Work left over: a pass failed, or stopped at MAX_PASSES with dirty set.
  const leaseFree = Date.parse(control.locked_until) < Date.now();
  if (control.dirty && leaseFree) {
    await invokeFunction("sync");
    return;
  }

  const pollStart = new Date().toISOString();
  const gtasks = gtasksClient(await googleAccessToken(Deno.env.get("GOOGLE_TOKEN_JSON")!));
  const { changed, listsSig } = await detectGtasksChanges(
    gtasks,
    control.gtasks_cursor,
    control.gtasks_lists_sig,
  );

  if (changed) {
    // Record the new lists signature now, or a list change would read as a
    // change on every poll. The task cursor only moves once a pass succeeds.
    await setGtasksPoll(null, listsSig);
    await invokeFunction("sync");
  } else {
    await setGtasksPoll(pollStart, listsSig);
  }
}
