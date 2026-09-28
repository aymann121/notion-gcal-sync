// syncTaskPages against the fakes: the conflict matrix, status ↔ completion,
// Course → task list, DELETE_SYNC both ways, and the Archived guard.

import { assert, assertEquals, assertNotEquals } from "jsr:@std/assert@1";
import { newContext, notionGtaskId, notionStatus, type SyncState, syncTaskPages } from "../sync_core.ts";
import {
  DEFAULT_TASKLIST_ID,
  type Fakes,
  makeFakes,
  makeGtask,
  makeTaskPage,
  NOTION_DATABASE_ID,
  taskState,
} from "./fakes.ts";

function setup(deleteSync = true): [Fakes, ReturnType<typeof newContext>] {
  const fakes = makeFakes();
  return [fakes, newContext(fakes, { notionDatabaseId: NOTION_DATABASE_ID, deleteSync })];
}

Deno.test("new Notion task creates a Google Task in the default list", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", { title: "Read chapter 1", due: "2026-03-01" }));
  const state: SyncState = {};

  await syncTaskPages(ctx, state, [page]);

  const [created] = f.gtasks.in(DEFAULT_TASKLIST_ID).values();
  assertEquals(created.title, "Read chapter 1");
  assertEquals(created.status, "needsAction");
  assertEquals(notionGtaskId(page), created.id);
  assertEquals(state["page-1"].tasklist_id, DEFAULT_TASKLIST_ID);
});

Deno.test("new Notion task with a Course lands in the matching list", async () => {
  const [f, ctx] = setup();
  f.notion.addPage({ id: "course-1", properties: { Name: { type: "title", title: [{ plain_text: "Biology 101" }] } } });
  const page = f.notion.addPage(makeTaskPage("page-1", { title: "Lab report", courseIds: ["course-1"] }));

  await syncTaskPages(ctx, {}, [page]);

  const bio = [...f.gtasks.tasklists.values()].find((tl) => tl.title === "Biology 101");
  assert(bio);
  assertEquals(f.gtasks.in(bio.id).size, 1);
});

Deno.test("Notion-only change pushes to the Google Task", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    title: "Updated title",
    due: "2026-04-01",
    gtaskId: "task-1",
    lastEditedTime: "2026-01-02T00:00:00Z",
  }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { title: "Old title", due: "2026-03-01", updated: "2026-01-01T00:00:00Z" }));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T12:00:00Z") }, [page]);

  const task = f.gtasks.in(DEFAULT_TASKLIST_ID).get("task-1");
  assertEquals(task.title, "Updated title");
  assertEquals(task.due, "2026-04-01T00:00:00.000Z");
});

Deno.test("Google-only change pulls into Notion", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    title: "Stale title",
    due: "2026-03-01",
    gtaskId: "task-1",
    lastEditedTime: "2026-01-01T00:00:00Z",
  }));
  f.gtasks.addTask(
    DEFAULT_TASKLIST_ID,
    makeGtask("task-1", { title: "Fresh title", due: "2026-05-01", status: "completed", updated: "2026-01-02T00:00:00Z" }),
  );

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T12:00:00Z") }, [page]);

  assertEquals(page.properties["Task name"].title[0].plain_text, "Fresh title");
  assertEquals(page.properties["Due date"].date.start, "2026-05-01");
  assertEquals(notionStatus(page), "Done");
});

Deno.test("both changed → Notion wins", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    title: "Notion title",
    due: "2026-06-01",
    status: "Done",
    gtaskId: "task-1",
    lastEditedTime: "2026-01-02T00:00:00Z",
  }));
  f.gtasks.addTask(
    DEFAULT_TASKLIST_ID,
    makeGtask("task-1", { title: "Google title", due: "2026-07-01", updated: "2026-01-03T00:00:00Z" }),
  );

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T00:00:00Z") }, [page]);

  const task = f.gtasks.in(DEFAULT_TASKLIST_ID).get("task-1");
  assertEquals(task.title, "Notion title");
  assertEquals(task.status, "completed");
});

Deno.test("neither changed → no writes, last_sync refreshed", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", { title: "Same", gtaskId: "task-1", lastEditedTime: "2026-01-01T00:00:00Z" }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { title: "Same", updated: "2026-01-01T00:00:00Z" }));
  const state: SyncState = { "page-1": taskState("task-1", "2026-01-02T00:00:00Z") };
  const notionUpdate = f.notion.updatePage;
  let writes = 0;
  f.notion.updatePage = (...args) => (writes++, notionUpdate.apply(f.notion, args));
  f.gtasks.patchTask = () => (writes++, Promise.resolve({}));

  await syncTaskPages(ctx, state, [page]);

  assertEquals(writes, 0);
  assertNotEquals(state["page-1"].last_sync, "2026-01-02T00:00:00Z");
});

// Notion reports last_edited_time truncated to the minute: an edit at
// 15:36:21 reads as 15:36:00, before a 15:36:06 sync.
Deno.test("Notion edit later in the same minute as the last sync still pushes", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    status: "Done",
    gtaskId: "task-1",
    lastEditedTime: "2026-09-27T15:36:00.000Z",
  }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { updated: "2026-09-27T15:30:00Z" }));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-09-27T15:36:06Z") }, [page]);

  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).get("task-1").status, "completed");
});

Deno.test("same-minute Notion edit yields to a Google change", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    gtaskId: "task-1",
    lastEditedTime: "2026-09-27T15:36:00.000Z",
  }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { status: "completed", updated: "2026-09-27T15:36:40Z" }));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-09-27T15:36:06Z") }, [page]);

  assertEquals(notionStatus(page), "Done");
  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).get("task-1").status, "completed");
});

Deno.test("clearing the due date in Notion clears it on the Google Task", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", { gtaskId: "task-1", lastEditedTime: "2026-01-02T00:00:00Z" }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { due: "2026-03-01", updated: "2026-01-01T00:00:00Z" }));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T12:00:00Z") }, [page]);

  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).get("task-1").due, undefined);
});

Deno.test("changing the Course moves the task to the matching list", async () => {
  const [f, ctx] = setup();
  f.notion.addPage({ id: "course-1", properties: { Name: { type: "title", title: [{ plain_text: "History" }] } } });
  f.gtasks.addTasklist("history-list", "History");
  const page = f.notion.addPage(makeTaskPage("page-1", {
    gtaskId: "task-1",
    courseIds: ["course-1"],
    lastEditedTime: "2026-01-02T00:00:00Z",
  }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { updated: "2026-01-01T00:00:00Z" }));
  const state: SyncState = { "page-1": taskState("task-1", "2026-01-01T12:00:00Z") };

  await syncTaskPages(ctx, state, [page]);

  assert(f.gtasks.in("history-list").has("task-1"));
  assertEquals(state["page-1"].tasklist_id, "history-list");
});

Deno.test("missing Google Task is recreated when DELETE_SYNC is false", async () => {
  const [f, ctx] = setup(false);
  const page = f.notion.addPage(makeTaskPage("page-1", { due: "2026-09-01", gtaskId: "missing-task" }));
  const state: SyncState = { "page-1": taskState("missing-task", "2026-01-01T00:00:00Z") };

  await syncTaskPages(ctx, state, [page]);

  assertEquals(page.in_trash, false);
  assertNotEquals(notionGtaskId(page), "missing-task");
  assertEquals(state["page-1"].task_id, notionGtaskId(page));
});

Deno.test("missing Google Task trashes the Notion page when DELETE_SYNC is true", async () => {
  const [f, ctx] = setup(true);
  const page = f.notion.addPage(makeTaskPage("page-1", { due: "2026-09-01", gtaskId: "missing-task" }));
  const state: SyncState = { "page-1": taskState("missing-task", "2026-01-01T00:00:00Z") };

  await syncTaskPages(ctx, state, [page]);

  assertEquals(page.in_trash, true);
  assertEquals(state["page-1"], undefined);
});

Deno.test("orphaned state entry is left alone and re-imported when DELETE_SYNC is false", async () => {
  const [f, ctx] = setup(false);
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("orphan-task"));
  const state: SyncState = { "page-1": taskState("orphan-task", "2026-01-01T00:00:00Z") };

  await syncTaskPages(ctx, state, []);

  assertEquals(state["page-1"], undefined);
  assert(f.gtasks.in(DEFAULT_TASKLIST_ID).has("orphan-task"));
  assertEquals([...f.notion.pages.values()].filter((p) => notionGtaskId(p) === "orphan-task").length, 1);
});

Deno.test("orphaned state entry deletes the Google Task when DELETE_SYNC is true", async () => {
  const [f, ctx] = setup(true);
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("orphan-task"));
  const state: SyncState = { "page-1": taskState("orphan-task", "2026-01-01T00:00:00Z") };

  await syncTaskPages(ctx, state, []);

  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).has("orphan-task"), false);
  assertEquals(state["page-1"], undefined);
});

Deno.test("a trashed (not deleted) Notion page also deletes its Google Task", async () => {
  const [f, ctx] = setup(true);
  f.notion.addPage(makeTaskPage("page-1", { gtaskId: "task-1", inTrash: true }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1"));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T00:00:00Z") }, []);

  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).has("task-1"), false);
});

Deno.test("DELETE_SYNC doesn't re-import the task it just deleted (stale snapshot)", async () => {
  const [f, ctx] = setup(true);
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("orphan-task", { title: "Ghost task" }));

  await syncTaskPages(ctx, { "page-1": taskState("orphan-task", "2026-01-01T00:00:00Z") }, []);

  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).has("orphan-task"), false);
  assertEquals([...f.notion.pages.values()].filter((p) => notionGtaskId(p) === "orphan-task").length, 0);
});

// ---- Archived tasks (written by notion-task-radar) -------------------------

Deno.test("Archived Notion task is created then completed in Google", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", { title: "Exercise", status: "Archived" }));
  const patches: string[] = [];
  const patch = f.gtasks.patchTask.bind(f.gtasks);
  f.gtasks.patchTask = (l, t, body) => (patches.push(body.status), patch(l, t, body));

  await syncTaskPages(ctx, {}, [page]);

  const [created] = f.gtasks.in(DEFAULT_TASKLIST_ID).values();
  assertEquals(created.status, "completed");
  assertEquals(patches, ["completed"]); // the follow-up patch that stamps `completed`
  assertEquals(notionGtaskId(page), created.id);
});

Deno.test("archiving in Notion completes the existing Google Task", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    status: "Archived",
    gtaskId: "task-1",
    lastEditedTime: "2026-01-02T00:00:00Z",
  }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { updated: "2026-01-01T00:00:00Z" }));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T12:00:00Z") }, [page]);

  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).get("task-1").status, "completed");
});

Deno.test("completed Google task never rewrites Archived as Done", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    status: "Archived",
    gtaskId: "task-1",
    lastEditedTime: "2026-01-01T00:00:00Z",
  }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { status: "completed", updated: "2026-01-02T00:00:00Z" }));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T12:00:00Z") }, [page]);

  assertEquals(notionStatus(page), "Archived");
});

Deno.test("un-ticking an Archived task in Google revives the Notion row", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeTaskPage("page-1", {
    status: "Archived",
    gtaskId: "task-1",
    lastEditedTime: "2026-01-01T00:00:00Z",
  }));
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1", { status: "needsAction", updated: "2026-01-02T00:00:00Z" }));

  await syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T12:00:00Z") }, [page]);

  assertEquals(notionStatus(page), "Not started");
});
