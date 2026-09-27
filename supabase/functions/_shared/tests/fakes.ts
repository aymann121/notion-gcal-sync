// Hand-written in-memory fakes for the Notion, Calendar and Tasks clients
// (a port of "cron (deprecated)/tests/fakes.py").
//
// They implement only the interface methods in clients.ts, plus enough state
// for a test to set up a scenario, run a sync_core function against it, and
// assert on what was recorded. Pages, events and tasks are stored by
// reference and updated in place, so a test can keep the object it added and
// read it after the pass. Missing objects throw a real ApiError, because
// isNotFound type-checks it. Known limitation: pagination isn't simulated.

import { ApiError, type Clients, type GCalApi, type GTasksApi, type Json, type NotionApi } from "../clients.ts";

export const NOTION_DATABASE_ID = "test-database-id";
export const DEFAULT_TASKLIST_ID = "default-tasklist-id";

export const notionNotFound = (message: string) =>
  new ApiError("notion", 404, "object_not_found", message);
export const googleNotFound = (message: string) => new ApiError("google", 404, null, message);

let idCounter = 0;
const newId = (prefix: string) => `${prefix}-${++idCounter}`;

// ---- Notion -------------------------------------------------------------

/** Echo plain_text next to {text: {content}}, as the real API does on write. */
function normalizeProperties(properties: Record<string, Json>): Record<string, Json> {
  for (const prop of Object.values(properties)) {
    for (const key of ["title", "rich_text"]) {
      for (const fragment of prop?.[key] ?? []) {
        if (!("plain_text" in fragment) && fragment.text) fragment.plain_text = fragment.text.content;
      }
    }
  }
  return properties;
}

export class FakeNotion implements NotionApi {
  pages = new Map<string, Json>();
  membership = new Map<string, string[]>(); // data source id → page ids
  dataSourceOf = new Map<string, string>(); // database id → data source id
  dataSourceProps = new Map<string, Record<string, Json>>();

  registerDatabase(databaseId: string, properties: Record<string, Json>): string {
    const dsId = `${databaseId}-ds`;
    this.dataSourceOf.set(databaseId, dsId);
    this.dataSourceProps.set(dsId, properties);
    return dsId;
  }

  /** The two-hop schema lookup that getCoursesDataSourceId/ensureCourse use:
   * the main database's Course relation and the Courses database's title. */
  registerDefaultCourseSchema(
    mainDatabaseId = NOTION_DATABASE_ID,
    coursesDatabaseId = "courses-db",
    titlePropName = "Name",
  ): string {
    const coursesDs = this.registerDatabase(coursesDatabaseId, { [titlePropName]: { type: "title" } });
    this.registerDatabase(mainDatabaseId, {
      Course: {
        type: "relation",
        relation: { database_id: coursesDatabaseId, data_source_id: coursesDs },
      },
    });
    return coursesDs;
  }

  /** Insert a pre-built page so tests control last_edited_time and values. */
  addPage(page: Json, databaseId: string | null = null): Json {
    this.pages.set(page.id, page);
    if (databaseId) this.join(this.dataSourceOf.get(databaseId) ?? databaseId, page.id);
    return page;
  }

  private join(dsId: string, pageId: string) {
    this.membership.set(dsId, [...(this.membership.get(dsId) ?? []), pageId]);
  }

  retrieveDatabase(databaseId: string): Promise<Json> {
    const dsId = this.dataSourceOf.get(databaseId);
    if (!dsId) return Promise.reject(notionNotFound(`no database ${databaseId}`));
    return Promise.resolve({ id: databaseId, data_sources: [{ id: dsId }] });
  }

  retrieveDataSource(dataSourceId: string): Promise<Json> {
    const properties = this.dataSourceProps.get(dataSourceId);
    if (!properties) return Promise.reject(notionNotFound(`no data source ${dataSourceId}`));
    return Promise.resolve({ id: dataSourceId, properties });
  }

  queryDataSource(dataSourceId: string) {
    const results = (this.membership.get(dataSourceId) ?? [])
      .map((id) => this.pages.get(id))
      .filter(Boolean);
    return Promise.resolve({ results, has_more: false, next_cursor: null });
  }

  retrievePage(pageId: string): Promise<Json> {
    const page = this.pages.get(pageId);
    return page ? Promise.resolve(page) : Promise.reject(notionNotFound(`no page ${pageId}`));
  }

  createPage(body: Json): Promise<Json> {
    const page = {
      id: newId("gen-page"),
      in_trash: false,
      last_edited_time: "2020-01-01T00:00:00.000Z",
      properties: normalizeProperties(structuredClone(body.properties)),
    };
    this.pages.set(page.id, page);
    if (body.parent?.data_source_id) this.join(body.parent.data_source_id, page.id);
    return Promise.resolve(page);
  }

  updatePage(pageId: string, body: Json): Promise<Json> {
    const page = this.pages.get(pageId);
    if (!page) return Promise.reject(notionNotFound(`no page ${pageId}`));
    if (body.properties) Object.assign(page.properties, normalizeProperties(structuredClone(body.properties)));
    if (body.in_trash !== undefined) page.in_trash = body.in_trash;
    return Promise.resolve(page);
  }
}

// ---- Google Calendar ------------------------------------------------------

export class FakeGCal implements GCalApi {
  events = new Map<string, Json>();

  addEvent(event: Json): Json {
    this.events.set(event.id, event);
    return event;
  }

  getEvent(eventId: string): Promise<Json> {
    const event = this.events.get(eventId);
    return event ? Promise.resolve(event) : Promise.reject(googleNotFound(`no event ${eventId}`));
  }

  insertEvent(body: Json): Promise<Json> {
    const event = { id: newId("gen-event"), updated: "2020-01-01T00:00:00.000Z", ...structuredClone(body) };
    this.events.set(event.id, event);
    return Promise.resolve(event);
  }

  patchEvent(eventId: string, body: Json): Promise<Json> {
    const event = this.events.get(eventId);
    if (!event) return Promise.reject(googleNotFound(`no event ${eventId}`));
    Object.assign(event, structuredClone(body));
    return Promise.resolve(event);
  }

  deleteEvent(eventId: string): Promise<void> {
    this.events.delete(eventId);
    return Promise.resolve();
  }

  listEvents(params: Record<string, string>) {
    let items = [...this.events.values()];
    if (params.privateExtendedProperty === "notion_page_id=*") {
      items = items.filter((e) => e.extendedProperties?.private?.notion_page_id);
    }
    return Promise.resolve({ items, nextPageToken: null });
  }

  watch(body: Json): Promise<Json> {
    return Promise.resolve({ id: body.id, expiration: "0" });
  }
}

// ---- Google Tasks -----------------------------------------------------------

export class FakeGTasks implements GTasksApi {
  tasklists = new Map<string, Json>([[DEFAULT_TASKLIST_ID, { id: DEFAULT_TASKLIST_ID, title: "My Tasks" }]]);
  tasks = new Map<string, Map<string, Json>>([[DEFAULT_TASKLIST_ID, new Map()]]);
  lastListTasksParams: Record<string, string> | null = null;

  addTasklist(listId: string, title: string): Json {
    this.tasklists.set(listId, { id: listId, title });
    if (!this.tasks.has(listId)) this.tasks.set(listId, new Map());
    return this.tasklists.get(listId);
  }

  addTask(listId: string, task: Json): Json {
    if (!this.tasks.has(listId)) this.tasks.set(listId, new Map());
    this.tasks.get(listId)!.set(task.id, task);
    return task;
  }

  /** Every task in one list, for assertions. */
  in(listId: string): Map<string, Json> {
    return this.tasks.get(listId) ?? new Map();
  }

  getTasklist(tasklistId: string): Promise<Json> {
    const tl = this.tasklists.get(tasklistId === "@default" ? DEFAULT_TASKLIST_ID : tasklistId);
    return tl ? Promise.resolve(tl) : Promise.reject(googleNotFound(`no tasklist ${tasklistId}`));
  }

  listTasklists() {
    return Promise.resolve({ items: [...this.tasklists.values()], nextPageToken: null });
  }

  insertTasklist(body: Json): Promise<Json> {
    return Promise.resolve(this.addTasklist(newId("gen-tasklist"), body.title));
  }

  getTask(tasklistId: string, taskId: string): Promise<Json> {
    const task = this.tasks.get(tasklistId)?.get(taskId);
    return task ? Promise.resolve(task) : Promise.reject(googleNotFound(`no task ${taskId} in ${tasklistId}`));
  }

  /** Honors updatedMin (by `updated`) and maxResults, for the poller tests. */
  listTasks(tasklistId: string, params: Record<string, string>) {
    this.lastListTasksParams = params;
    let items = [...(this.tasks.get(tasklistId)?.values() ?? [])];
    if (params.updatedMin) {
      const min = Date.parse(params.updatedMin);
      items = items.filter((t) => Date.parse(t.updated) >= min);
    }
    if (params.maxResults) items = items.slice(0, Number(params.maxResults));
    return Promise.resolve({ items, nextPageToken: null });
  }

  insertTask(tasklistId: string, body: Json): Promise<Json> {
    const task = {
      id: newId("gen-task"),
      updated: "2020-01-01T00:00:00.000Z",
      status: "needsAction",
      ...structuredClone(body),
    };
    return Promise.resolve(this.addTask(tasklistId, task));
  }

  patchTask(tasklistId: string, taskId: string, body: Json): Promise<Json> {
    const task = this.tasks.get(tasklistId)?.get(taskId);
    if (!task) return Promise.reject(googleNotFound(`no task ${taskId} in ${tasklistId}`));
    const fields = structuredClone(body);
    if (fields.due === null) {
      delete task.due;
      delete fields.due;
    }
    Object.assign(task, fields);
    return Promise.resolve(task);
  }

  moveTask(tasklistId: string, taskId: string, destinationTasklist: string): Promise<Json> {
    const task = this.tasks.get(tasklistId)?.get(taskId);
    if (!task) return Promise.reject(googleNotFound(`no task ${taskId} in ${tasklistId}`));
    this.tasks.get(tasklistId)!.delete(taskId);
    return Promise.resolve(this.addTask(destinationTasklist, task));
  }

  deleteTask(tasklistId: string, taskId: string): Promise<void> {
    this.tasks.get(tasklistId)?.delete(taskId);
    return Promise.resolve();
  }
}

export interface Fakes extends Clients {
  notion: FakeNotion;
  gcal: FakeGCal;
  gtasks: FakeGTasks;
}

/** Fresh fakes with the Tasks Tracker and Courses schemas registered. */
export function makeFakes(): Fakes {
  const notion = new FakeNotion();
  notion.registerDefaultCourseSchema();
  return { notion, gcal: new FakeGCal(), gtasks: new FakeGTasks() };
}

// ---- Fixture builders -------------------------------------------------------

export function makeTaskPage(
  pageId: string,
  opts: {
    title?: string;
    due?: string | null;
    status?: string | null;
    gtaskId?: string | null;
    courseIds?: string[];
    syncAs?: string | null;
    lastEditedTime?: string;
    inTrash?: boolean;
  } = {},
): Json {
  const {
    title = "Test task",
    due = null,
    status = "Not started",
    gtaskId = null,
    courseIds = [],
    syncAs = null,
    lastEditedTime = "2020-01-01T00:00:00.000Z",
    inTrash = false,
  } = opts;
  return {
    id: pageId,
    in_trash: inTrash,
    last_edited_time: lastEditedTime,
    properties: {
      "Task name": { title: [{ plain_text: title }] },
      "Status": { status: status ? { name: status } : null },
      "Course": { relation: courseIds.map((id) => ({ id })) },
      "Google Task ID": { rich_text: gtaskId ? [{ plain_text: gtaskId }] : [] },
      "Sync As": { select: syncAs ? { name: syncAs } : null },
      "Due date": { date: due ? { start: due } : null },
    },
  };
}

export function makeEventPage(
  pageId: string,
  opts: {
    title?: string;
    due?: string | null;
    gcalId?: string | null;
    lastEditedTime?: string;
    inTrash?: boolean;
  } = {},
): Json {
  const {
    title = "Test event",
    due = "2026-01-01",
    gcalId = null,
    lastEditedTime = "2020-01-01T00:00:00.000Z",
    inTrash = false,
  } = opts;
  return {
    id: pageId,
    in_trash: inTrash,
    last_edited_time: lastEditedTime,
    properties: {
      "Task name": { title: [{ plain_text: title }] },
      "Due date": { date: due ? { start: due } : null },
      "Google Event ID": { rich_text: gcalId ? [{ plain_text: gcalId }] : [] },
      "Sync As": { select: { name: "Event" } },
    },
  };
}

export function makeGcalEvent(
  eventId: string,
  opts: { title?: string; date?: string; updated?: string; notionPageId?: string | null } = {},
): Json {
  const { title = "Test event", date = "2026-01-01", updated = "2020-01-01T00:00:00.000Z", notionPageId = null } =
    opts;
  const event: Json = { id: eventId, summary: title, start: { date }, end: { date }, updated };
  if (notionPageId) event.extendedProperties = { private: { notion_page_id: notionPageId } };
  return event;
}

export function makeGtask(
  taskId: string,
  opts: { title?: string; due?: string | null; status?: string; updated?: string } = {},
): Json {
  const { title = "Test task", due = null, status = "needsAction", updated = "2020-01-01T00:00:00.000Z" } = opts;
  const task: Json = { id: taskId, title, status, updated };
  if (due) task.due = `${due}T00:00:00.000Z`;
  return task;
}

export function taskState(taskId: string, lastSync: string, listId = DEFAULT_TASKLIST_ID) {
  return { last_sync: lastSync, kind: "task" as const, task_id: taskId, tasklist_id: listId };
}

export function eventState(eventId: string, lastSync: string) {
  return { last_sync: lastSync, kind: "event" as const, event_id: eventId };
}
