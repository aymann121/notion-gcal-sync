// The Google Tasks poller's change detection.

import { assertEquals } from "jsr:@std/assert@1";
import { detectGtasksChanges } from "../gtasks_poll.ts";
import { DEFAULT_TASKLIST_ID, FakeGTasks, makeGtask } from "./fakes.ts";

const CURSOR = "2026-01-02T00:00:00.000Z";

async function baseline(g: FakeGTasks) {
  return (await detectGtasksChanges(g, CURSOR, null)).listsSig;
}

Deno.test("no cursor yet (first run) counts as a change", async () => {
  const g = new FakeGTasks();
  assertEquals((await detectGtasksChanges(g, null, null)).changed, true);
});

Deno.test("nothing updated since the cursor → no change", async () => {
  const g = new FakeGTasks();
  g.addTask(DEFAULT_TASKLIST_ID, makeGtask("old", { updated: "2026-01-01T00:00:00.000Z" }));
  const sig = await baseline(g);
  assertEquals((await detectGtasksChanges(g, CURSOR, sig)).changed, false);
});

Deno.test("a task updated after the cursor is a change", async () => {
  const g = new FakeGTasks();
  g.addTasklist("bio", "Biology");
  const sig = await baseline(g);
  g.addTask("bio", makeGtask("new", { updated: "2026-01-03T00:00:00.000Z" }));
  assertEquals((await detectGtasksChanges(g, CURSOR, sig)).changed, true);
});

Deno.test("the tasks query asks for deleted, hidden and completed tasks, one result", async () => {
  const g = new FakeGTasks();
  const sig = await baseline(g);
  await detectGtasksChanges(g, CURSOR, sig);
  assertEquals(g.lastListTasksParams, {
    updatedMin: CURSOR,
    showDeleted: "true",
    showHidden: "true",
    showCompleted: "true",
    maxResults: "1",
  });
});

Deno.test("a list added, renamed or removed is a change", async () => {
  const g = new FakeGTasks();
  const sig = await baseline(g);
  g.addTasklist("bio", "Biology");
  assertEquals((await detectGtasksChanges(g, CURSOR, sig)).changed, true);

  const withBio = await baseline(g);
  g.tasklists.get("bio").title = "Biology II";
  assertEquals((await detectGtasksChanges(g, CURSOR, withBio)).changed, true);

  const renamed = await baseline(g);
  g.tasklists.delete("bio");
  assertEquals((await detectGtasksChanges(g, CURSOR, renamed)).changed, true);
});
