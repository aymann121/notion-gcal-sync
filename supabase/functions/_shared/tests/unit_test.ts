// Pure helpers: no I/O, no fakes.

import { assert, assertEquals, assertFalse, assertThrows } from "jsr:@std/assert@1";
import { ApiError, isNotFound } from "../clients.ts";
import {
  dateValue,
  dueFromGtasks,
  dueToGtasks,
  findCourseRelationProperty,
  gtasksToStatus,
  isEventRow,
  isTaskRow,
  lastSyncDt,
  notionDueDate,
  notionGtaskId,
  notionTitle,
  parseDt,
  resolveNotionStatus,
  statusToGtasks,
} from "../sync_core.ts";
import { makeEventPage, makeTaskPage } from "./fakes.ts";

Deno.test("statusToGtasks: Done and Archived complete the task, anything else doesn't", () => {
  assertEquals(statusToGtasks("Done"), "completed");
  assertEquals(statusToGtasks("Archived"), "completed");
  assertEquals(statusToGtasks("In progress"), "needsAction");
  assertEquals(statusToGtasks(null), "needsAction");
});

Deno.test("gtasksToStatus: completed → Done, else Not started", () => {
  assertEquals(gtasksToStatus("completed"), "Done");
  assertEquals(gtasksToStatus("needsAction"), "Not started");
  assertEquals(gtasksToStatus(undefined), "Not started");
});

Deno.test("resolveNotionStatus keeps Archived when Google says completed", () => {
  assertEquals(resolveNotionStatus("Archived", "completed"), "Archived");
  assertEquals(resolveNotionStatus("Not started", "completed"), "Done");
});

Deno.test("resolveNotionStatus revives an Archived task that was un-ticked in Google", () => {
  assertEquals(resolveNotionStatus("Archived", "needsAction"), "Not started");
});

Deno.test("due dates keep only the date part in both directions", () => {
  assertEquals(dueToGtasks("2026-03-01"), "2026-03-01T00:00:00.000Z");
  assertEquals(dueToGtasks("2026-03-01T15:30:00.000-04:00"), "2026-03-01T00:00:00.000Z");
  assertEquals(dueToGtasks(null), null);
  assertEquals(dueFromGtasks({ due: "2026-03-01T00:00:00.000Z" }), "2026-03-01");
  assertEquals(dueFromGtasks({}), null);
  assertEquals(dateValue(null), { date: null });
  assertEquals(dateValue("2026-03-01T10:00"), { date: { start: "2026-03-01" } });
});

Deno.test("parseDt accepts Z, offsets, no zone, and Python microseconds", () => {
  assertEquals(parseDt("2026-01-01T00:00:00Z"), Date.UTC(2026, 0, 1));
  assertEquals(parseDt("2026-01-01T02:00:00+02:00"), Date.UTC(2026, 0, 1));
  assertEquals(parseDt("2026-01-01T00:00:00"), Date.UTC(2026, 0, 1));
  assertEquals(parseDt("2026-09-25T02:04:54.630224+00:00"), Date.UTC(2026, 8, 25, 2, 4, 54, 630));
  assertThrows(() => parseDt("not a date"));
});

Deno.test("lastSyncDt is -Infinity for a page never synced", () => {
  assertEquals(lastSyncDt({}, "page-1"), -Infinity);
});

Deno.test("row readers pull plain values out of a page", () => {
  const page = makeTaskPage("p", { title: "Read", due: "2026-03-01T09:00:00.000Z", gtaskId: "t-1" });
  assertEquals(notionTitle(page), "Read");
  assertEquals(notionDueDate(page), "2026-03-01");
  assertEquals(notionGtaskId(page), "t-1");
  assertEquals(notionTitle(makeTaskPage("q", { title: "" })), "(untitled)");
});

Deno.test("row classification: blank or Task → Tasks, Event → Calendar", () => {
  assert(isTaskRow(makeTaskPage("a")));
  assert(isTaskRow(makeTaskPage("b", { syncAs: "Task" })));
  assertFalse(isTaskRow(makeEventPage("c")));
  assert(isEventRow(makeEventPage("c")));
});

Deno.test("isNotFound: only Google 404/410 and Notion object_not_found", () => {
  assert(isNotFound(new ApiError("google", 404, null, "")));
  assert(isNotFound(new ApiError("google", 410, null, "")));
  assert(isNotFound(new ApiError("notion", 404, "object_not_found", "")));
  assertFalse(isNotFound(new ApiError("google", 500, null, "")));
  assertFalse(isNotFound(new ApiError("notion", 429, "rate_limited", "")));
  assertFalse(isNotFound(new Error("network down")));
});

Deno.test("findCourseRelationProperty: by name, then the only relation, else throw", () => {
  const course = { type: "relation", relation: { data_source_id: "c" } };
  assertEquals(findCourseRelationProperty({ Course: course }), course);
  assertEquals(findCourseRelationProperty({ Classes: course, Name: { type: "title" } }), course);
  assertThrows(() => findCourseRelationProperty({ A: course, B: course }));
  assertThrows(() => findCourseRelationProperty({ Name: { type: "title" } }));
});
