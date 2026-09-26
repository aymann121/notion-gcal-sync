// importUnlinkedGtasks plus the tasklist / Course find-or-create caches.

import { assertEquals, assertNotEquals } from "jsr:@std/assert@1";
import {
  ensureCourse,
  ensureTasklist,
  getDefaultTasklistId,
  importUnlinkedGtasks,
  listAllGtasks,
  newContext,
  notionCoursePageIds,
  notionGtaskId,
  notionStatus,
  notionTitle,
  refreshTasklistCache,
  type SyncState,
} from "../sync_core.ts";
import { DEFAULT_TASKLIST_ID, type Fakes, makeFakes, makeGtask, NOTION_DATABASE_ID } from "./fakes.ts";

function setup(): [Fakes, ReturnType<typeof newContext>] {
  const fakes = makeFakes();
  return [fakes, newContext(fakes, { notionDatabaseId: NOTION_DATABASE_ID })];
}

async function importAll(ctx: ReturnType<typeof newContext>, state: SyncState, linked = new Set<string>()) {
  await refreshTasklistCache(ctx);
  await importUnlinkedGtasks(ctx, state, await listAllGtasks(ctx), linked);
}

Deno.test("unlinked needsAction task is imported into Notion", async () => {
  const [f, ctx] = setup();
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("gtask-1", { title: "From Google" }));
  const state: SyncState = {};

  await importAll(ctx, state);

  const [page] = f.notion.pages.values();
  assertEquals(notionTitle(page), "From Google");
  assertEquals(notionGtaskId(page), "gtask-1");
  assertEquals(state[page.id].task_id, "gtask-1");
});

Deno.test("completed unlinked task is imported as Done", async () => {
  const [f, ctx] = setup();
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("gtask-1", { status: "completed" }));

  await importAll(ctx, {});

  assertEquals(notionStatus([...f.notion.pages.values()][0]), "Done");
});

Deno.test("an already linked task is not re-imported", async () => {
  const [f, ctx] = setup();
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("gtask-1"));

  await importAll(ctx, {}, new Set(["gtask-1"]));

  assertEquals(f.notion.pages.size, 0);
});

Deno.test("a task in a non-default list gets a matching Course", async () => {
  const [f, ctx] = setup();
  f.gtasks.addTasklist("chem-list", "Chemistry");
  f.gtasks.addTask("chem-list", makeGtask("gtask-1", { title: "Lab prep" }));

  await importAll(ctx, {});

  const page = [...f.notion.pages.values()].find((p) => notionGtaskId(p) === "gtask-1");
  assertNotEquals(notionCoursePageIds(page), []);
});

Deno.test("a task in the default list gets no Course", async () => {
  const [f, ctx] = setup();
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("gtask-1"));

  await importAll(ctx, {});

  assertEquals(notionCoursePageIds([...f.notion.pages.values()][0]), []);
});

Deno.test("getDefaultTasklistId resolves the @default alias", async () => {
  const [, ctx] = setup();
  assertEquals(await getDefaultTasklistId(ctx), DEFAULT_TASKLIST_ID);
});

Deno.test("ensureTasklist reuses an existing list by title", async () => {
  const [f, ctx] = setup();
  f.gtasks.addTasklist("history-list-id", "History");
  assertEquals(await ensureTasklist(ctx, "History"), "history-list-id");
  assertEquals(f.gtasks.tasklists.size, 2);
});

Deno.test("ensureTasklist creates a list once and caches it for the pass", async () => {
  const [f, ctx] = setup();
  const first = await ensureTasklist(ctx, "Physics");
  const second = await ensureTasklist(ctx, "Physics");
  assertEquals(first, second);
  assertEquals([...f.gtasks.tasklists.values()].filter((tl) => tl.title === "Physics").length, 1);
});

Deno.test("ensureCourse reuses an existing Course page by title", async () => {
  const [f, ctx] = setup();
  f.notion.addPage(
    { id: "existing-course", properties: { Name: { type: "title", title: [{ plain_text: "Chemistry" }] } } },
    "courses-db",
  );
  assertEquals(await ensureCourse(ctx, "Chemistry"), "existing-course");
  assertEquals(f.notion.pages.size, 1);
});

Deno.test("ensureCourse creates a Course page when none matches", async () => {
  const [f, ctx] = setup();
  const id = await ensureCourse(ctx, "Art History");
  assertEquals(f.notion.pages.get(id).properties.Name.title[0].plain_text, "Art History");
  assertEquals(f.notion.membership.get("courses-db-ds"), [id]);
});
