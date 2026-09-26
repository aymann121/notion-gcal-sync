// syncEventPages against the fakes: the conflict matrix, DELETE_SYNC both
// ways, and orphaned (tagged) Calendar events.

import { assertEquals, assertNotEquals } from "jsr:@std/assert@1";
import { newContext, notionDueDate, notionGcalId, notionTitle, type SyncState, syncEventPages } from "../sync_core.ts";
import { eventState, type Fakes, makeEventPage, makeFakes, makeGcalEvent, NOTION_DATABASE_ID } from "./fakes.ts";

function setup(deleteSync = true): [Fakes, ReturnType<typeof newContext>] {
  const fakes = makeFakes();
  return [fakes, newContext(fakes, { notionDatabaseId: NOTION_DATABASE_ID, deleteSync })];
}

Deno.test("new Notion event creates a Calendar event", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeEventPage("page-1", { title: "Exam", due: "2026-03-01" }));
  const state: SyncState = {};

  await syncEventPages(ctx, state, [page]);

  const [created] = f.gcal.events.values();
  assertEquals(created.summary, "Exam");
  assertEquals(created.start.date, "2026-03-01");
  assertEquals(created.extendedProperties.private.notion_page_id, "page-1");
  assertEquals(notionGcalId(page), created.id);
  assertEquals(state["page-1"], { ...state["page-1"], kind: "event", event_id: created.id });
});

Deno.test("Notion-only change pushes to Calendar", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeEventPage("page-1", {
    title: "Updated title",
    due: "2026-04-01",
    gcalId: "event-1",
    lastEditedTime: "2026-01-02T00:00:00Z",
  }));
  f.gcal.addEvent(makeGcalEvent("event-1", { title: "Old title", date: "2026-03-01", updated: "2026-01-01T00:00:00Z" }));

  await syncEventPages(ctx, { "page-1": eventState("event-1", "2026-01-01T12:00:00Z") }, [page]);

  assertEquals(f.gcal.events.get("event-1").summary, "Updated title");
  assertEquals(f.gcal.events.get("event-1").start.date, "2026-04-01");
});

Deno.test("Calendar-only change pulls into Notion", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeEventPage("page-1", {
    title: "Stale title",
    due: "2026-03-01",
    gcalId: "event-1",
    lastEditedTime: "2026-01-01T00:00:00Z",
  }));
  f.gcal.addEvent(makeGcalEvent("event-1", { title: "Fresh title", date: "2026-05-01", updated: "2026-01-02T00:00:00Z" }));

  await syncEventPages(ctx, { "page-1": eventState("event-1", "2026-01-01T12:00:00Z") }, [page]);

  assertEquals(notionTitle(page), "Fresh title");
  assertEquals(notionDueDate(page), "2026-05-01");
});

Deno.test("both changed → Notion wins", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeEventPage("page-1", {
    title: "Notion title",
    due: "2026-06-01",
    gcalId: "event-1",
    lastEditedTime: "2026-01-02T00:00:00Z",
  }));
  f.gcal.addEvent(makeGcalEvent("event-1", { title: "Google title", date: "2026-07-01", updated: "2026-01-03T00:00:00Z" }));

  await syncEventPages(ctx, { "page-1": eventState("event-1", "2026-01-01T00:00:00Z") }, [page]);

  assertEquals(f.gcal.events.get("event-1").summary, "Notion title");
  assertEquals(f.gcal.events.get("event-1").start.date, "2026-06-01");
  assertEquals(notionTitle(page), "Notion title");
});

Deno.test("neither changed → no writes, last_sync refreshed", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeEventPage("page-1", {
    title: "Same",
    due: "2026-08-01",
    gcalId: "event-1",
    lastEditedTime: "2026-01-01T00:00:00Z",
  }));
  f.gcal.addEvent(makeGcalEvent("event-1", { title: "Same", date: "2026-08-01", updated: "2026-01-01T00:00:00Z" }));
  const state: SyncState = { "page-1": eventState("event-1", "2026-01-02T00:00:00Z") };
  let writes = 0;
  f.gcal.patchEvent = () => (writes++, Promise.resolve({}));
  f.notion.updatePage = () => (writes++, Promise.resolve({}));

  await syncEventPages(ctx, state, [page]);

  assertEquals(writes, 0);
  assertNotEquals(state["page-1"].last_sync, "2026-01-02T00:00:00Z");
});

Deno.test("missing Calendar event is recreated when DELETE_SYNC is false", async () => {
  const [f, ctx] = setup(false);
  const page = f.notion.addPage(makeEventPage("page-1", { due: "2026-09-01", gcalId: "missing-event" }));

  await syncEventPages(ctx, { "page-1": eventState("missing-event", "2026-01-01T00:00:00Z") }, [page]);

  assertEquals(page.in_trash, false);
  assertEquals(f.gcal.events.size, 1);
  assertEquals(notionGcalId(page), [...f.gcal.events.keys()][0]);
});

Deno.test("missing Calendar event trashes the Notion page when DELETE_SYNC is true", async () => {
  const [f, ctx] = setup(true);
  const page = f.notion.addPage(makeEventPage("page-1", { due: "2026-09-01", gcalId: "missing-event" }));
  const state: SyncState = { "page-1": eventState("missing-event", "2026-01-01T00:00:00Z") };

  await syncEventPages(ctx, state, [page]);

  assertEquals(page.in_trash, true);
  assertEquals(state["page-1"], undefined);
  assertEquals(f.gcal.events.size, 0);
});

Deno.test("an Event row without a due date is skipped", async () => {
  const [f, ctx] = setup();
  const page = f.notion.addPage(makeEventPage("page-1", { due: null }));
  const state: SyncState = {};

  await syncEventPages(ctx, state, [page]);

  assertEquals(f.gcal.events.size, 0);
  assertEquals(state, {});
});

Deno.test("deleted Notion page is recreated from its tagged event when DELETE_SYNC is false", async () => {
  const [f, ctx] = setup(false);
  f.gcal.addEvent(makeGcalEvent("event-1", { title: "Orphaned event", date: "2026-10-01", notionPageId: "deleted-page" }));

  await syncEventPages(ctx, {}, []);

  const created = [...f.notion.pages.values()];
  assertEquals(created.length, 1);
  assertEquals(notionTitle(created[0]), "Orphaned event");
  assertEquals(f.gcal.events.get("event-1").extendedProperties.private.notion_page_id, created[0].id);
});

Deno.test("deleted Notion page deletes its Calendar event when DELETE_SYNC is true", async () => {
  const [f, ctx] = setup(true);
  f.gcal.addEvent(makeGcalEvent("event-1", { notionPageId: "deleted-page" }));

  await syncEventPages(ctx, {}, []);

  assertEquals(f.gcal.events.size, 0);
  assertEquals(f.notion.pages.size, 0);
});

Deno.test("a trashed Notion page also deletes its Calendar event", async () => {
  const [f, ctx] = setup(true);
  f.notion.addPage(makeEventPage("page-1", { gcalId: "event-1", inTrash: true }));
  f.gcal.addEvent(makeGcalEvent("event-1", { notionPageId: "page-1" }));

  await syncEventPages(ctx, {}, []);

  assertEquals(f.gcal.events.size, 0);
});
