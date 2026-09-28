// Two-way sync: Notion Tasks Tracker ↔ Google Tasks + Google Calendar.
//
// Routing via Notion "Sync As":
//   Task (or empty) → Google Tasks  (Course → list; Status ↔ completion)
//   Event           → Google Calendar (all-day; title + due)
//
// Conflict rule: compare last_edited_time / updated vs last_sync;
// whichever side changed more recently wins. Both changed → Notion wins.
//
// Linked IDs live on the Notion page (Google Task ID / Google Event ID).
// Deletions are mirrored both ways while DELETE_SYNC is true.
//
// A port of "cron (deprecated)/sync.py", section for section and in the same order:
// each section only uses things defined above it. One difference: sync.py's
// module-level clients and per-run caches live on a SyncContext created fresh
// for every pass, because an Edge Function isolate is reused across requests
// and module globals would leak between passes.

import { type Clients, isNotFound, type Json } from "./clients.ts";

// ==== Config =================================================================
// The exact Notion column and option names this sync depends on. Edit here
// if Notion is renamed.

// Must match property names in the Notion database exactly.
export const PROP_TITLE = "Task name";
export const PROP_DUE_DATE = "Due date";
export const PROP_STATUS = "Status";
export const PROP_COURSE = "Course";
export const PROP_SYNC_AS = "Sync As";
export const PROP_GCAL_EVENT_ID = "Google Event ID";
export const PROP_GTASK_ID = "Google Task ID";

export const SYNC_AS_TASK = "Task";
export const SYNC_AS_EVENT = "Event";
export const STATUS_DONE = "Done";
export const STATUS_NOT_STARTED = "Not started";
// Written by notion-task-radar when a Radar task ends its day unfinished.
// Terminal like Done, so it completes the Google Task -- but it must never be
// overwritten *by* a completed Google Task (see resolveNotionStatus).
export const STATUS_ARCHIVED = "Archived";

// Deleting on one side archives/deletes the other: a deleted Notion row deletes
// its Google Task/event, and a deleted Google Task archives its Notion page.
// Because "missing" now means "delete the counterpart", every lookup that can
// report a thing as missing must be sure it really is (see isNotFound).
export const DELETE_SYNC = true;

export interface SyncConfig {
  notionDatabaseId: string;
  deleteSync?: boolean; // defaults to DELETE_SYNC; tests flip it
}

// ==== Per-pass context =======================================================
// The API clients plus every cache sync.py kept in module globals.

export interface SyncContext {
  notion: Clients["notion"];
  gcal: Clients["gcal"];
  gtasks: Clients["gtasks"];
  notionDatabaseId: string;
  deleteSync: boolean;
  tasksDataSourceId: string | null;
  coursesDataSourceId: string | null;
  courseTitleCache: Map<string, string>; // course page id → title
  coursePagesCache: Map<string, string> | null; // title → course page id
  courseTitlePropName: string | null;
  tasklistCache: Map<string, string> | null; // title → list id
  tasklistTitles: Map<string, string> | null; // list id → title
  defaultTasklistId: string | null; // Google's "My Tasks" list id (resolved, not the alias)
}

export function newContext(clients: Clients, config: SyncConfig): SyncContext {
  return {
    ...clients,
    notionDatabaseId: config.notionDatabaseId,
    deleteSync: config.deleteSync ?? DELETE_SYNC,
    tasksDataSourceId: null,
    coursesDataSourceId: null,
    courseTitleCache: new Map(),
    coursePagesCache: null,
    courseTitlePropName: null,
    tasklistCache: null,
    tasklistTitles: null,
    defaultTasklistId: null,
  };
}

// ==== General utilities ======================================================
// Small helpers with no knowledge of Notion or Google data: timestamp
// parsing/formatting and pagination. (isNotFound lives in clients.ts.)

/** ISO timestamp → epoch milliseconds (UTC). Extra fractional digits, as in
 * Python's microsecond timestamps, are truncated to milliseconds. */
export function parseDt(s: string): number {
  const normalized = s.replace(/(\.\d{3})\d+/, "$1");
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(normalized);
  const ms = Date.parse(hasZone ? normalized : normalized + "Z");
  if (Number.isNaN(ms)) throw new Error(`unparseable timestamp ${s}`);
  return ms;
}

export function utcNowIso(): string {
  return new Date().toISOString();
}

/** Every item from a Google list call, following nextPageToken across pages. */
export async function googleListAll(
  listMethod: (params: Record<string, string>) => Promise<{ items?: Json[]; nextPageToken?: string | null }>,
  params: Record<string, string> = {},
): Promise<Json[]> {
  const items: Json[] = [];
  let pageToken: string | undefined;
  while (true) {
    const resp = await listMethod(pageToken ? { ...params, pageToken } : params);
    items.push(...(resp.items ?? []));
    pageToken = resp.nextPageToken ?? undefined;
    if (!pageToken) return items;
  }
}

// ==== State (sync_state table) ===============================================
// The only memory between passes: {notion_page_id: {last_sync, kind, ids...}}.
// Same shape as sync_state.json; db.ts loads and saves it as a whole.
// Comparing each side's edit time against last_sync tells us what changed.

export interface StateEntry {
  last_sync: string;
  kind: "task" | "event";
  task_id?: string;
  tasklist_id?: string;
  event_id?: string;
}
export type SyncState = Record<string, StateEntry>;

/** When we last successfully synced this page (or epoch if never). */
export function lastSyncDt(state: SyncState, pageId: string): number {
  const lastSync = state[pageId]?.last_sync;
  return lastSync ? parseDt(lastSync) : -Infinity;
}

/** Did the Notion row change since last_sync?
 *
 * Notion truncates last_edited_time to the minute, so an edit made later in
 * the same minute as the last sync reads as older than it. An edit in that
 * minute counts as a change unless Google changed too: then the certain
 * Google change wins over the possible Notion one.
 */
export function notionChangedSince(notionEdited: number, last: number, googleChanged: boolean): boolean {
  if (notionEdited > last) return true;
  const lastMinute = Math.floor(last / 60_000) * 60_000;
  return notionEdited >= lastMinute && !googleChanged;
}

/** State record for a Notion row linked to a Calendar event. */
export function eventStateEntry(eventId: string): StateEntry {
  return { last_sync: utcNowIso(), kind: "event", event_id: eventId };
}

/** State record for a Notion row linked to a Google Task. */
export function taskStateEntry(taskId: string, tasklistId: string): StateEntry {
  return { last_sync: utcNowIso(), kind: "task", task_id: taskId, tasklist_id: tasklistId };
}

// ==== Notion: reading rows ===================================================
// Notion basics: a "database" holds one or more "data sources" (tables); each
// row is a "page". A row's column values live in page.properties, keyed by
// column name (the PROP_* constants). Text is "rich text": a list of chunks
// whose raw characters are in plain_text. A "relation" column links a row to
// rows in another database (like a foreign key).
//
// These functions fetch rows and pull plain values out of them.

/** First data source of a database (these databases each have exactly one). */
export async function dataSourceIdOf(ctx: SyncContext, databaseId: string): Promise<string> {
  const db = await ctx.notion.retrieveDatabase(databaseId);
  return db.data_sources[0].id;
}

export async function getTasksDataSourceId(ctx: SyncContext): Promise<string> {
  ctx.tasksDataSourceId ??= await dataSourceIdOf(ctx, ctx.notionDatabaseId);
  return ctx.tasksDataSourceId;
}

/** Every row of a data source, fetched 100 at a time. */
export async function queryAll(ctx: SyncContext, dataSourceId: string): Promise<Json[]> {
  const pages: Json[] = [];
  let cursor: string | undefined;
  while (true) {
    const resp = await ctx.notion.queryDataSource(dataSourceId, cursor);
    pages.push(...resp.results);
    if (!resp.has_more) return pages;
    cursor = resp.next_cursor ?? undefined;
  }
}

/** Every row of the Tasks Tracker database. */
export async function getNotionPages(ctx: SyncContext): Promise<Json[]> {
  return queryAll(ctx, await getTasksDataSourceId(ctx));
}

/** One row by id, or null if it no longer exists (other errors throw). */
export async function getNotionPage(ctx: SyncContext, pageId: string): Promise<Json | null> {
  try {
    return await ctx.notion.retrievePage(pageId);
  } catch (exc) {
    if (!isNotFound(exc)) throw exc;
    return null;
  }
}

/** True if the page is in Notion's trash. API 2026-03-11 renamed `archived`
 * to `in_trash`; both are accepted so fixtures in either shape work. */
export function isTrashed(page: Json): boolean {
  return Boolean(page.in_trash || page.archived);
}

/** Join a list of rich-text chunks into one plain string. */
export function plainText(chunks: Json[] | undefined): string {
  return (chunks ?? []).map((t) => t.plain_text).join("");
}

/** Name of the title column among a row's or database's properties, or null.
 *
 * Every database has exactly one; matching by type means we don't need to
 * know what it's called (used for the Courses database).
 */
export function findTitlePropName(props: Record<string, Json>): string | null {
  for (const [name, prop] of Object.entries(props)) {
    if (prop?.type === "title") return name;
  }
  return null;
}

/** Text of a text column (first chunk only; fine for the IDs stored there), or null. */
export function richTextPlain(page: Json, propName: string): string | null {
  const rt = page.properties[propName]?.rich_text ?? [];
  return rt.length ? rt[0].plain_text : null;
}

/** Row's title as plain text (all chunks joined), or "(untitled)". */
export function notionTitle(page: Json): string {
  return plainText(page.properties[PROP_TITLE]?.title) || "(untitled)";
}

/** Row's due date as "YYYY-MM-DD" (any time is dropped), or null. */
export function notionDueDate(page: Json): string | null {
  const d = page.properties[PROP_DUE_DATE]?.date;
  if (!d) return null;
  return d.start ? d.start.slice(0, 10) : null;
}

/** Row's Status option name (e.g. "Done"), or null. */
export function notionStatus(page: Json): string | null {
  return page.properties[PROP_STATUS]?.status?.name ?? null;
}

/** Row's Sync As dropdown value ("Task"/"Event"), or null if blank. */
export function notionSyncAs(page: Json): string | null {
  return page.properties[PROP_SYNC_AS]?.select?.name ?? null;
}

/** Id of the linked Google Calendar event, or null if not linked yet. */
export function notionGcalId(page: Json): string | null {
  return richTextPlain(page, PROP_GCAL_EVENT_ID);
}

/** Id of the linked Google Task, or null if not linked yet. */
export function notionGtaskId(page: Json): string | null {
  return richTextPlain(page, PROP_GTASK_ID);
}

/** Page ids of the Course rows this row links to (may be empty). */
export function notionCoursePageIds(page: Json): string[] {
  return (page.properties[PROP_COURSE]?.relation ?? []).map((r: Json) => r.id);
}

/** True if this row syncs to Google Tasks (Sync As is "Task" or blank). */
export function isTaskRow(page: Json): boolean {
  const syncAs = notionSyncAs(page);
  return syncAs === null || syncAs === SYNC_AS_TASK;
}

/** True if this row syncs to Google Calendar (Sync As is "Event"). */
export function isEventRow(page: Json): boolean {
  return notionSyncAs(page) === SYNC_AS_EVENT;
}

// ==== Notion: writing rows ===================================================
// Build the value objects Notion expects, then create or update rows with them.

export const titleValue = (text: string) => ({ title: [{ text: { content: text } }] });
export const textValue = (text: string) => ({ rich_text: [{ text: { content: text } }] });
/** Date only; null (or empty) clears the date. */
export const dateValue = (dateStr: string | null) => ({
  date: dateStr ? { start: dateStr.slice(0, 10) } : null,
});
export const statusValue = (name: string) => ({ status: { name } });
export const selectValue = (name: string) => ({ select: { name } });

/** Write several columns of one row in a single API call. */
export async function updateNotionPage(
  ctx: SyncContext,
  pageId: string,
  properties: Record<string, Json>,
): Promise<void> {
  await ctx.notion.updatePage(pageId, { properties });
}

/** Move a row to Notion's trash (the API's "archive"). */
export async function trashNotionPage(ctx: SyncContext, pageId: string): Promise<void> {
  await ctx.notion.updatePage(pageId, { in_trash: true });
}

/** Link a row to its Google Calendar event by storing the event id. */
export async function setNotionGcalId(ctx: SyncContext, pageId: string, eventId: string) {
  await updateNotionPage(ctx, pageId, { [PROP_GCAL_EVENT_ID]: textValue(eventId) });
}

/** Link a row to its Google Task by storing the task id. */
export async function setNotionGtaskId(ctx: SyncContext, pageId: string, taskId: string) {
  await updateNotionPage(ctx, pageId, { [PROP_GTASK_ID]: textValue(taskId) });
}

async function tasksParent(ctx: SyncContext) {
  return { type: "data_source_id", data_source_id: await getTasksDataSourceId(ctx) };
}

/** Add a Tasks Tracker row for a Google Calendar event, linked by its id. */
export async function createNotionEvent(
  ctx: SyncContext,
  title: string,
  dateStr: string,
  eventId: string,
): Promise<Json> {
  return ctx.notion.createPage({
    parent: await tasksParent(ctx),
    properties: {
      [PROP_TITLE]: titleValue(title),
      [PROP_DUE_DATE]: dateValue(dateStr),
      [PROP_GCAL_EVENT_ID]: textValue(eventId),
      [PROP_SYNC_AS]: selectValue(SYNC_AS_EVENT),
    },
  });
}

/** Add a Tasks Tracker row for a Google Task, linked by its id (optionally to a Course). */
export async function createNotionTask(
  ctx: SyncContext,
  title: string,
  due: string | null,
  taskId: string,
  statusName: string,
  coursePageId: string | null = null,
): Promise<Json> {
  const properties: Record<string, Json> = {
    [PROP_TITLE]: titleValue(title),
    [PROP_STATUS]: statusValue(statusName),
    [PROP_GTASK_ID]: textValue(taskId),
    [PROP_SYNC_AS]: selectValue(SYNC_AS_TASK),
  };
  if (due) properties[PROP_DUE_DATE] = dateValue(due);
  if (coursePageId) properties[PROP_COURSE] = { relation: [{ id: coursePageId }] };
  return ctx.notion.createPage({ parent: await tasksParent(ctx), properties });
}

// ==== Notion: Courses database ===============================================
// Tasks link to rows in a separate Courses database via the Course column.
// Its data source and title column are discovered at runtime, then cached per
// pass, so course names can be looked up (id → name) or found/created (name → id).

/** A data source's column definitions: {column name: {type, ...}}. */
export async function getDataSourceProperties(
  ctx: SyncContext,
  dataSourceId: string,
): Promise<Record<string, Json>> {
  return (await ctx.notion.retrieveDataSource(dataSourceId)).properties;
}

/** Find the Course column's definition among a database's column definitions.
 *
 * Tries the column named PROP_COURSE first. If that fails (e.g. the column was
 * renamed), falls back to the only relation-type column, since this database
 * has just one. Throws if neither works.
 */
export function findCourseRelationProperty(props: Record<string, Json>): Json {
  // 1. By name.
  if (props[PROP_COURSE]?.type === "relation") return props[PROP_COURSE];
  // 2. By type: if exactly one column is a relation, it must be Course.
  const relations = Object.values(props).filter((p) => p?.type === "relation");
  if (relations.length === 1) return relations[0];
  // 3. Ambiguous or missing: fail, listing every column to help debugging.
  const available = Object.entries(props).map(([n, p]) => `${n}:${p?.type}`);
  throw new Error(
    `Could not find the '${PROP_COURSE}' relation property. Available properties: ${available}`,
  );
}

/** Data source of the Courses database that the Course column links to (cached). */
export async function getCoursesDataSourceId(ctx: SyncContext): Promise<string> {
  if (ctx.coursesDataSourceId === null) {
    const props = await getDataSourceProperties(ctx, await getTasksDataSourceId(ctx));
    const relation = findCourseRelationProperty(props).relation;
    // Responses carry both ids since 2025-09-03; resolve from database_id if not.
    ctx.coursesDataSourceId = relation.data_source_id ??
      await dataSourceIdOf(ctx, relation.database_id);
  }
  return ctx.coursesDataSourceId!;
}

/** Title of a Course row, given its page id (cached per pass). */
export async function courseTitle(ctx: SyncContext, pageId: string): Promise<string> {
  const cached = ctx.courseTitleCache.get(pageId);
  if (cached !== undefined) return cached;
  const props = (await ctx.notion.retrievePage(pageId)).properties;
  const titleName = findTitlePropName(props);
  const name = (titleName ? plainText(props[titleName].title) : "") || "(untitled course)";
  ctx.courseTitleCache.set(pageId, name);
  return name;
}

/** Load every Course row into a {title: page id} cache; also record the title column's name. */
export async function refreshCourseCache(ctx: SyncContext): Promise<Map<string, string>> {
  const coursesDs = await getCoursesDataSourceId(ctx);
  ctx.courseTitlePropName = findTitlePropName(await getDataSourceProperties(ctx, coursesDs));

  // Ends up as {course title: Course row's page id}, e.g.
  //   {"CS 101": "1a2b3c...", "Calculus II": "4d5e6f..."}
  const cache = new Map<string, string>();
  for (const page of await queryAll(ctx, coursesDs)) {
    const name = plainText(page.properties[ctx.courseTitlePropName ?? ""]?.title);
    if (name) cache.set(name, page.id);
  }
  ctx.coursePagesCache = cache;
  return cache;
}

/** Page id of the Course row with this title, creating the row if needed. */
export async function ensureCourse(ctx: SyncContext, name: string): Promise<string> {
  const cache = ctx.coursePagesCache ?? await refreshCourseCache(ctx);
  const existing = cache.get(name);
  if (existing) return existing;
  const created = await ctx.notion.createPage({
    parent: { type: "data_source_id", data_source_id: await getCoursesDataSourceId(ctx) },
    properties: { [ctx.courseTitlePropName!]: titleValue(name) },
  });
  cache.set(name, created.id);
  return created.id;
}

// ==== Google Calendar ========================================================
// Thin wrappers over the Calendar API. Every event is all-day and tagged
// with the id of the Notion row it came from.

export async function getGcalEvent(ctx: SyncContext, eventId: string): Promise<Json | null> {
  try {
    return await ctx.gcal.getEvent(eventId);
  } catch (exc) {
    if (!isNotFound(exc)) throw exc;
    return null;
  }
}

/** start/end fields for an all-day event on this date. */
export function allDay(dateStr: string) {
  return { start: { date: dateStr.slice(0, 10) }, end: { date: dateStr.slice(0, 10) } };
}

/** All-day event tagged with notionPageId for reverse lookup. */
export function createGcalEvent(ctx: SyncContext, title: string, dateStr: string, notionPageId: string) {
  return ctx.gcal.insertEvent({
    summary: title,
    ...allDay(dateStr),
    extendedProperties: { private: { notion_page_id: notionPageId } },
  });
}

export function updateGcalEvent(
  ctx: SyncContext,
  eventId: string,
  title: string | null = null,
  dateStr: string | null = null,
) {
  const body: Json = {};
  if (title !== null) body.summary = title;
  if (dateStr !== null) Object.assign(body, allDay(dateStr));
  return ctx.gcal.patchEvent(eventId, body);
}

/** Events previously created by this sync (private notion_page_id tag). */
export function listGcalEventsFromNotion(ctx: SyncContext): Promise<Json[]> {
  return googleListAll((p) => ctx.gcal.listEvents(p), {
    privateExtendedProperty: "notion_page_id=*",
    showDeleted: "false",
  });
}

/** An event's start as "YYYY-MM-DD" (all-day or timed), or null. */
export function gcalEventDate(event: Json): string | null {
  const start = event.start ?? {};
  return start.date || (start.dateTime ?? "").slice(0, 10) || null;
}

// ==== Google Tasks ===========================================================
// Thin wrappers over the Tasks API: task lists (one per course, found or
// created by name) and the tasks inside them.

/** Real id of Google's default "My Tasks" list (the "@default" alias resolves to it). */
export async function getDefaultTasklistId(ctx: SyncContext): Promise<string> {
  ctx.defaultTasklistId ??= (await ctx.gtasks.getTasklist("@default")).id;
  return ctx.defaultTasklistId!;
}

/** Load all Google Task lists into ctx.tasklistCache / ctx.tasklistTitles. */
export async function refreshTasklistCache(ctx: SyncContext): Promise<Map<string, string>> {
  ctx.tasklistCache = new Map();
  ctx.tasklistTitles = new Map();
  for (const tl of await googleListAll((p) => ctx.gtasks.listTasklists(p), { maxResults: "100" })) {
    ctx.tasklistCache.set(tl.title, tl.id);
    ctx.tasklistTitles.set(tl.id, tl.title);
  }
  return ctx.tasklistCache;
}

/** {list title: list id}, loading it on first use. */
export async function getTasklistCache(ctx: SyncContext): Promise<Map<string, string>> {
  return ctx.tasklistCache ?? await refreshTasklistCache(ctx);
}

/** Return list id for name; create the list if missing. */
export async function ensureTasklist(ctx: SyncContext, name: string): Promise<string> {
  const cache = await getTasklistCache(ctx);
  const existing = cache.get(name);
  if (existing) return existing;
  const created = await ctx.gtasks.insertTasklist({ title: name });
  cache.set(name, created.id);
  return created.id;
}

/** Tasks API wants RFC3339; only the date part is kept (midnight UTC). */
export function dueToGtasks(dateStr: string | null): string | null {
  return dateStr ? `${dateStr.slice(0, 10)}T00:00:00.000Z` : null;
}

export function dueFromGtasks(task: Json): string | null {
  return task.due ? task.due.slice(0, 10) : null;
}

export async function getGtask(ctx: SyncContext, tasklistId: string, taskId: string): Promise<Json | null> {
  try {
    return await ctx.gtasks.getTask(tasklistId, taskId);
  } catch (exc) {
    if (!isNotFound(exc)) throw exc;
    return null;
  }
}

/** Find a task by id (try known list first, then scan all lists). */
export async function findGtask(
  ctx: SyncContext,
  taskId: string,
  preferredListId: string | null = null,
): Promise<[string | null, Json | null]> {
  if (preferredListId) {
    const task = await getGtask(ctx, preferredListId, taskId);
    if (task !== null) return [preferredListId, task];
  }
  for (const listId of (await getTasklistCache(ctx)).values()) {
    if (listId === preferredListId) continue;
    const task = await getGtask(ctx, listId, taskId);
    if (task !== null) return [listId, task];
  }
  return [null, null];
}

export function createGtask(
  ctx: SyncContext,
  tasklistId: string,
  title: string,
  due: string | null = null,
  status = "needsAction",
) {
  const body: Json = { title, status };
  const encoded = dueToGtasks(due);
  if (encoded) body.due = encoded;
  return ctx.gtasks.insertTask(tasklistId, body);
}

export function updateGtask(
  ctx: SyncContext,
  tasklistId: string,
  taskId: string,
  fields: { title?: string; due?: string | null; status?: string; clearDue?: boolean },
): Promise<Json | null> {
  const body: Json = {};
  if (fields.title !== undefined) body.title = fields.title;
  if (fields.status !== undefined) body.status = fields.status;
  if (fields.clearDue) body.due = null;
  else if (fields.due !== undefined && fields.due !== null) body.due = dueToGtasks(fields.due);
  if (Object.keys(body).length === 0) return Promise.resolve(null);
  return ctx.gtasks.patchTask(tasklistId, taskId, body);
}

/** Move task when Notion Course (list) changes. */
export async function moveGtask(ctx: SyncContext, taskId: string, fromListId: string, toListId: string) {
  await ctx.gtasks.moveTask(fromListId, taskId, toListId);
}

export type GtaskSnapshot = Map<string, [string, Json]>; // task_id → [list_id, task]

/** task_id → [list_id, task], including completed/hidden. */
export async function listAllGtasks(ctx: SyncContext): Promise<GtaskSnapshot> {
  const result: GtaskSnapshot = new Map();
  for (const listId of (await getTasklistCache(ctx)).values()) {
    const tasks = await googleListAll((p) => ctx.gtasks.listTasks(listId, p), {
      showCompleted: "true",
      showHidden: "true",
      maxResults: "100",
    });
    for (const task of tasks) result.set(task.id, [listId, task]);
  }
  return result;
}

// ==== Status mapping (Notion Status ↔ Google Tasks completion) ===============
// Notion has several statuses; a Google Task is only completed or not.

/** Map a Notion Status to Google's task state: Done/Archived → "completed", else "needsAction". */
export function statusToGtasks(statusName: string | null): string {
  return statusName === STATUS_DONE || statusName === STATUS_ARCHIVED ? "completed" : "needsAction";
}

/** Map Google's task state to a Notion Status: "completed" → Done, else Not started. */
export function gtasksToStatus(gtasksStatus: string | undefined): string {
  return gtasksStatus === "completed" ? STATUS_DONE : STATUS_NOT_STARTED;
}

/** New Notion Status for a task after it was changed in Google Tasks.
 *
 * Normally: completed → Done, not completed → Not started.
 *
 * Exception: if the Notion task is Archived (missed) and the Google task is
 * completed, keep it Archived. The sync itself marks archived tasks as
 * completed in Google, so without this, the next pass would see that change
 * and wrongly turn every missed task into Done.
 */
export function resolveNotionStatus(currentStatus: string | null, gtasksStatus: string | undefined): string {
  if (gtasksStatus === "completed" && currentStatus === STATUS_ARCHIVED) return STATUS_ARCHIVED;
  return gtasksToStatus(gtasksStatus);
}

// ==== Core sync: Events (Notion ↔ Google Calendar) ===========================
// Rows with Sync As = Event. Built from the Notion and Calendar helpers above;
// syncEventPages at the bottom is the entry point for this half.

/** New Notion event → create its Calendar event and link the two. */
export async function createGcalEventForPage(
  ctx: SyncContext,
  state: SyncState,
  pageId: string,
  title: string,
  due: string,
) {
  const event = await createGcalEvent(ctx, title, due, pageId);
  await setNotionGcalId(ctx, pageId, event.id);
  state[pageId] = eventStateEntry(event.id);
}

/** Sync one Notion row that is already linked to a Calendar event. */
export async function syncLinkedEventPage(
  ctx: SyncContext,
  state: SyncState,
  page: Json,
  eventId: string,
  gcalEvents: Map<string, Json>,
) {
  const pageId = page.id;
  const title = notionTitle(page);
  const due = notionDueDate(page)!;
  const notionEdited = parseDt(page.last_edited_time);
  const event = gcalEvents.get(eventId) ?? await getGcalEvent(ctx, eventId);

  // Google event missing: archive Notion or recreate the event
  if (event === null) {
    if (ctx.deleteSync) {
      await trashNotionPage(ctx, pageId);
      delete state[pageId];
    } else {
      await createGcalEventForPage(ctx, state, pageId, title, due);
    }
    return;
  }

  // Decide which side wins since last_sync
  const googleUpdated = parseDt(event.updated);
  const last = lastSyncDt(state, pageId);
  const googleChanged = googleUpdated > last;
  const notionChanged = notionChangedSince(notionEdited, last, googleChanged);

  if (notionChanged) {
    // Notion-only change, or both changed (Notion wins)
    await updateGcalEvent(ctx, eventId, title, due);
  } else if (googleChanged) {
    await updateNotionPage(ctx, pageId, {
      [PROP_TITLE]: titleValue(event.summary ?? title),
      [PROP_DUE_DATE]: dateValue(gcalEventDate(event) ?? due),
    });
  }

  state[pageId] = eventStateEntry(eventId);
}

/** A tagged Calendar event whose Notion row is gone: delete it, or recreate the row. */
export async function handleOrphanGcalEvent(
  ctx: SyncContext,
  state: SyncState,
  eventId: string,
  event: Json,
) {
  const notionPageId = event.extendedProperties?.private?.notion_page_id;
  const page = notionPageId ? await getNotionPage(ctx, notionPageId) : null;
  if (page !== null && !isTrashed(page)) return;

  if (ctx.deleteSync) {
    await ctx.gcal.deleteEvent(eventId);
    return;
  }

  // Recreate Notion Event row and retag the Calendar event
  const gDate = gcalEventDate(event);
  if (!gDate) return;
  const newPage = await createNotionEvent(ctx, event.summary ?? "(untitled)", gDate, eventId);
  await ctx.gcal.patchEvent(eventId, {
    extendedProperties: { private: { notion_page_id: newPage.id } },
  });
  state[newPage.id] = eventStateEntry(eventId);
}

/** Sync As = Event ↔ Google Calendar all-day events. */
export async function syncEventPages(ctx: SyncContext, state: SyncState, eventPages: Json[]) {
  const gcalEvents = new Map<string, Json>(
    (await listGcalEventsFromNotion(ctx)).map((e) => [e.id, e]),
  );
  const seenEventIds = new Set<string>();

  for (const page of eventPages) {
    const due = notionDueDate(page);
    if (!due) continue; // calendar events require a due date
    const eventId = notionGcalId(page);
    if (!eventId) {
      await createGcalEventForPage(ctx, state, page.id, notionTitle(page), due);
      continue;
    }
    seenEventIds.add(eventId);
    await syncLinkedEventPage(ctx, state, page, eventId, gcalEvents);
  }

  // Tagged Calendar events whose Notion page was deleted
  for (const [eventId, event] of gcalEvents) {
    if (!seenEventIds.has(eventId)) await handleOrphanGcalEvent(ctx, state, eventId, event);
  }
}

// ==== Core sync: Tasks (Notion ↔ Google Tasks) ===============================
// Rows with Sync As = Task (or blank). Same shape as the event sync, plus
// course → task list routing and importing tasks created in Google.
// syncTaskPages at the bottom is the entry point for this half.

/** Google Tasks list for this row: one named after its first Course, else the default list. */
export async function notionTargetTasklistId(ctx: SyncContext, page: Json): Promise<string> {
  const ids = notionCoursePageIds(page);
  if (ids.length === 0) return getDefaultTasklistId(ctx);
  return ensureTasklist(ctx, await courseTitle(ctx, ids[0]));
}

/** New Notion task → create its Google Task and link the two. */
export async function createGtaskForPage(
  ctx: SyncContext,
  state: SyncState,
  pageId: string,
  targetListId: string,
  title: string,
  due: string | null,
  gStatus: string,
) {
  const task = await createGtask(ctx, targetListId, title, due, gStatus);
  // Inserting with status=completed doesn't reliably stamp Google's `completed`
  // field; this patch does, so the task lands in the Completed list.
  if (gStatus === "completed") await updateGtask(ctx, targetListId, task.id, { status: "completed" });
  await setNotionGtaskId(ctx, pageId, task.id);
  state[pageId] = taskStateEntry(task.id, targetListId);
}

/** Sync one Notion row that is already linked to a Google Task. */
export async function syncLinkedTaskPage(
  ctx: SyncContext,
  state: SyncState,
  page: Json,
  taskId: string,
  allGtasks: GtaskSnapshot,
) {
  const pageId = page.id;
  const title = notionTitle(page);
  const due = notionDueDate(page);
  const statusName = notionStatus(page);
  const gStatus = statusToGtasks(statusName);
  const targetListId = await notionTargetTasklistId(ctx, page);
  const notionEdited = parseDt(page.last_edited_time);

  const preferred = state[pageId]?.tasklist_id ?? null;
  let [listId, task] = allGtasks.get(taskId) ?? await findGtask(ctx, taskId, preferred);

  // Google task missing: archive Notion or recreate the task
  if (task === null || listId === null) {
    if (ctx.deleteSync) {
      await trashNotionPage(ctx, pageId);
      delete state[pageId];
    } else {
      await createGtaskForPage(ctx, state, pageId, targetListId, title, due, gStatus);
    }
    return;
  }

  // Course changed → move to the matching task list
  if (listId !== targetListId) {
    await moveGtask(ctx, taskId, listId, targetListId);
    listId = targetListId;
    task = (await getGtask(ctx, listId, taskId)) ?? task;
  }

  const googleUpdated = parseDt(task.updated);
  const last = lastSyncDt(state, pageId);
  const googleChanged = googleUpdated > last;
  const notionChanged = notionChangedSince(notionEdited, last, googleChanged);

  if (notionChanged) {
    // Notion-only change, or both changed (Notion wins).
    // Omitting `due` wouldn't remove it in Google, so a cleared date must be explicit.
    await updateGtask(ctx, listId, taskId, {
      title,
      due,
      status: gStatus,
      clearDue: due === null && Boolean(task.due),
    });
  } else if (googleChanged) {
    await updateNotionPage(ctx, pageId, {
      [PROP_TITLE]: titleValue(task.title || title),
      [PROP_DUE_DATE]: dateValue(dueFromGtasks(task)),
      [PROP_STATUS]: statusValue(resolveNotionStatus(statusName, task.status)),
    });
  }

  state[pageId] = taskStateEntry(taskId, listId);
}

/** State entries whose Notion page is gone → delete the Google Task too.
 *
 * Returns the ids of deleted Google Tasks.
 */
export async function deleteGtasksForRemovedPages(
  ctx: SyncContext,
  state: SyncState,
  allGtasks: GtaskSnapshot,
  seenTaskIds: Set<string>,
): Promise<Set<string>> {
  const deletedTaskIds = new Set<string>();
  for (const [pageId, entry] of Object.entries(state)) {
    if (typeof entry !== "object" || entry === null || entry.kind !== "task") continue;
    const taskId = entry.task_id;
    if (!taskId || seenTaskIds.has(taskId)) continue;
    const page = await getNotionPage(ctx, pageId);
    if (page === null || isTrashed(page)) {
      if (ctx.deleteSync) {
        // The recorded list can be stale (or absent on an old entry); fall
        // back to where the task actually is, or the task survives the
        // delete and gets re-imported into Notion on the next pass.
        const listId = allGtasks.get(taskId)?.[0] ?? entry.tasklist_id;
        if (listId) {
          try {
            await ctx.gtasks.deleteTask(listId, taskId);
          } catch (exc) {
            if (!isNotFound(exc)) throw exc;
          }
          deletedTaskIds.add(taskId);
        }
      }
      delete state[pageId];
    }
  }
  return deletedTaskIds;
}

/** Create Notion rows for every Google Task not yet linked to a page.
 *
 * Nothing is skipped: completed tasks come in too (as Status = Done), so the
 * two sides stay a complete mirror of each other.
 */
export async function importUnlinkedGtasks(
  ctx: SyncContext,
  state: SyncState,
  allGtasks: GtaskSnapshot,
  linkedTaskIds: Set<string>,
) {
  const defaultListId = await getDefaultTasklistId(ctx);

  for (const [taskId, [listId, task]] of allGtasks) {
    if (linkedTaskIds.has(taskId)) continue;

    let coursePageId: string | null = null;
    if (listId !== defaultListId) {
      const listTitle = ctx.tasklistTitles?.get(listId);
      if (listTitle) coursePageId = await ensureCourse(ctx, listTitle);
    }

    const newPage = await createNotionTask(
      ctx,
      task.title || "(untitled)",
      dueFromGtasks(task),
      taskId,
      gtasksToStatus(task.status),
      coursePageId,
    );
    state[newPage.id] = taskStateEntry(taskId, listId);
  }
}

/** Sync As = Task (or empty) ↔ Google Tasks. */
export async function syncTaskPages(ctx: SyncContext, state: SyncState, taskPages: Json[]) {
  await refreshTasklistCache(ctx);
  const allGtasks = await listAllGtasks(ctx); // snapshot: {task_id: [list_id, task]}
  const seenTaskIds = new Set<string>();

  for (const page of taskPages) {
    const taskId = notionGtaskId(page);
    if (!taskId) {
      await createGtaskForPage(
        ctx,
        state,
        page.id,
        await notionTargetTasklistId(ctx, page),
        notionTitle(page),
        notionDueDate(page),
        statusToGtasks(notionStatus(page)),
      );
      continue;
    }
    seenTaskIds.add(taskId);
    await syncLinkedTaskPage(ctx, state, page, taskId, allGtasks);
  }

  const deletedTaskIds = await deleteGtasksForRemovedPages(ctx, state, allGtasks, seenTaskIds);
  // allGtasks predates the deletes, so skip those ids or they'd be re-imported.
  await importUnlinkedGtasks(ctx, state, allGtasks, new Set([...seenTaskIds, ...deletedTaskIds]));
}

// ==== Entry point ============================================================
// One full pass over an in-memory copy of the state. The caller persists the
// result only if this returns, so a failed pass saves nothing.

export interface PassResult {
  state: SyncState;
  startedAt: string; // becomes the Google Tasks poller's cursor
}

/** One full pass: Tasks path, then Events path. Doesn't mutate `initialState`. */
export async function runSyncPass(
  clients: Clients,
  config: SyncConfig,
  initialState: SyncState,
): Promise<PassResult> {
  const startedAt = utcNowIso();
  const ctx = newContext(clients, config);
  const state: SyncState = structuredClone(initialState);
  const pages = await getNotionPages(ctx);

  const taskPages = pages.filter(isTaskRow);
  const eventPages = pages.filter(isEventRow);

  await syncTaskPages(ctx, state, taskPages);
  await syncEventPages(ctx, state, eventPages);

  return { state, startedAt };
}
