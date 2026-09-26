// Change detection for Google Tasks, which has no push API. Cheap by design:
// one tasklists call plus one maxResults=1 tasks call per list.

import type { GTasksApi } from "./clients.ts";
import { googleListAll } from "./sync_core.ts";

export interface PollResult {
  changed: boolean;
  listsSig: string;
}

/** Did anything in Google Tasks change since `cursor`?
 *
 * A list added, removed or renamed shows up in the lists signature. A task
 * created, edited, completed or deleted shows up through updatedMin, with
 * showDeleted/showHidden/showCompleted so none of those are filtered out.
 * No cursor yet (first run) counts as a change.
 */
export async function detectGtasksChanges(
  gtasks: GTasksApi,
  cursor: string | null,
  previousSig: string | null,
): Promise<PollResult> {
  const lists = await googleListAll((p) => gtasks.listTasklists(p), { maxResults: "100" });
  const listsSig = lists.map((l) => `${l.id}:${l.title}`).sort().join("|");
  if (cursor === null || listsSig !== previousSig) return { changed: true, listsSig };

  for (const list of lists) {
    const resp = await gtasks.listTasks(list.id, {
      updatedMin: cursor,
      showDeleted: "true",
      showHidden: "true",
      showCompleted: "true",
      maxResults: "1",
    });
    if ((resp.items ?? []).length > 0) return { changed: true, listsSig };
  }
  return { changed: false, listsSig };
}
