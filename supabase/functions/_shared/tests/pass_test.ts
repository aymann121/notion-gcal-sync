// runSyncPass end to end over the fakes, including the two-pass Archived
// regression, which a single-pass test can't catch.

import { assertEquals, assertRejects } from "jsr:@std/assert@1";
import { runSyncPass, type SyncState } from "../sync_core.ts";
import {
  DEFAULT_TASKLIST_ID,
  makeEventPage,
  makeFakes,
  makeGtask,
  makeTaskPage,
  NOTION_DATABASE_ID,
  taskState,
} from "./fakes.ts";

const config = { notionDatabaseId: NOTION_DATABASE_ID };
const TASKS_DS = `${NOTION_DATABASE_ID}-ds`;

Deno.test("a pass routes rows to the Tasks and Calendar paths", async () => {
  const f = makeFakes();
  f.notion.addPage(makeTaskPage("task-page"), NOTION_DATABASE_ID);
  f.notion.addPage(makeEventPage("event-page", { due: "2026-03-01" }), NOTION_DATABASE_ID);

  const { state } = await runSyncPass(f, config, {});

  assertEquals(state["task-page"].kind, "task");
  assertEquals(state["event-page"].kind, "event");
  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).size, 1);
  assertEquals(f.gcal.events.size, 1);
});

Deno.test("rows imported from Google go into the Tasks data source", async () => {
  const f = makeFakes();
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("gtask-1"));

  const { state } = await runSyncPass(f, config, {});

  const [pageId] = Object.keys(state);
  assertEquals(f.notion.membership.get(TASKS_DS), [pageId]);
});

Deno.test("a failed pass throws and leaves the caller's state untouched", async () => {
  const f = makeFakes();
  f.notion.addPage(makeTaskPage("task-page"), NOTION_DATABASE_ID);
  f.gcal.listEvents = () => Promise.reject(new Error("calendar down")); // after the Tasks path succeeds
  const initial: SyncState = {};

  await assertRejects(() => runSyncPass(f, config, initial));
  assertEquals(initial, {});
});

Deno.test("Archived stays Archived across two passes after Google bumps `updated`", async () => {
  const f = makeFakes();
  const page = f.notion.addPage(
    makeTaskPage("page-1", { status: "Archived", lastEditedTime: "2026-01-01T00:00:00Z" }),
    NOTION_DATABASE_ID,
  );

  // Pass 1: created, then completed in Google.
  const first = await runSyncPass(f, config, {});
  const [task] = f.gtasks.in(DEFAULT_TASKLIST_ID).values();
  assertEquals(task.status, "completed");

  // Completing it bumps Google's `updated` past last_sync...
  task.updated = new Date(Date.now() + 60_000).toISOString();

  // ...so pass 2 takes the "Google changed" branch, which must not write Done.
  await runSyncPass(f, config, first.state);
  assertEquals(page.properties.Status.status.name, "Archived");
});

Deno.test("a pass never mutates the state it was given", async () => {
  const f = makeFakes();
  const page = f.notion.addPage(makeTaskPage("page-1", { gtaskId: "missing" }), NOTION_DATABASE_ID);
  const initial: SyncState = { "page-1": taskState("missing", "2026-01-01T00:00:00Z") };

  const { state } = await runSyncPass(f, config, initial);

  assertEquals(page.in_trash, true);
  assertEquals(state["page-1"], undefined);
  assertEquals(initial["page-1"].task_id, "missing");
});
