// The destructive edge of DELETE_SYNC: a flaky API call must never be
// mistaken for a deleted object. With DELETE_SYNC on, a swallowed 500 or
// rate-limit would delete a live Google Task or trash a live Notion page, so
// these paths must fail the pass loudly instead.

import { assertEquals, assertRejects } from "jsr:@std/assert@1";
import { ApiError } from "../clients.ts";
import { newContext, type SyncState, syncEventPages, syncTaskPages } from "../sync_core.ts";
import {
  DEFAULT_TASKLIST_ID,
  eventState,
  makeEventPage,
  makeFakes,
  makeGcalEvent,
  makeGtask,
  makeTaskPage,
  NOTION_DATABASE_ID,
  taskState,
} from "./fakes.ts";

Deno.test("a Google 500 on task lookup doesn't trash the Notion page", async () => {
  const f = makeFakes();
  const ctx = newContext(f, { notionDatabaseId: NOTION_DATABASE_ID, deleteSync: true });
  // Linked task missing from the listing snapshot, so the pass looks it up.
  const page = f.notion.addPage(makeTaskPage("page-1", { gtaskId: "task-1" }));
  f.gtasks.getTask = () => Promise.reject(new ApiError("google", 500, null, "backend error"));

  await assertRejects(
    () => syncTaskPages(ctx, { "page-1": taskState("task-1", "2026-01-01T00:00:00Z") }, [page]),
    ApiError,
  );
  assertEquals(page.in_trash, false);
});

Deno.test("a Notion 429 during cleanup doesn't delete the Google Task", async () => {
  const f = makeFakes();
  const ctx = newContext(f, { notionDatabaseId: NOTION_DATABASE_ID, deleteSync: true });
  f.gtasks.addTask(DEFAULT_TASKLIST_ID, makeGtask("task-1"));
  const state: SyncState = { "page-1": taskState("task-1", "2026-01-01T00:00:00Z") };
  f.notion.retrievePage = () => Promise.reject(new ApiError("notion", 429, "rate_limited", "slow down"));

  await assertRejects(() => syncTaskPages(ctx, state, []), ApiError);
  assertEquals(f.gtasks.in(DEFAULT_TASKLIST_ID).has("task-1"), true);
  assertEquals(state["page-1"] !== undefined, true); // survives, so the next pass retries the check
});

Deno.test("a Google 500 on event lookup doesn't trash the Notion page", async () => {
  const f = makeFakes();
  const ctx = newContext(f, { notionDatabaseId: NOTION_DATABASE_ID, deleteSync: true });
  const page = f.notion.addPage(makeEventPage("page-1", { gcalId: "event-1" }));
  f.gcal.getEvent = () => Promise.reject(new ApiError("google", 503, null, "unavailable"));

  await assertRejects(
    () => syncEventPages(ctx, { "page-1": eventState("event-1", "2026-01-01T00:00:00Z") }, [page]),
    ApiError,
  );
  assertEquals(page.in_trash, false);
});

Deno.test("a Notion 500 on an orphan check doesn't delete the Calendar event", async () => {
  const f = makeFakes();
  const ctx = newContext(f, { notionDatabaseId: NOTION_DATABASE_ID, deleteSync: true });
  f.gcal.addEvent(makeGcalEvent("event-1", { notionPageId: "page-1" }));
  f.notion.retrievePage = () => Promise.reject(new ApiError("notion", 500, "internal_server_error", "oops"));

  await assertRejects(() => syncEventPages(ctx, {}, []), ApiError);
  assertEquals(f.gcal.events.has("event-1"), true);
});
