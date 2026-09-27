// Thin fetch-based clients for Notion, Google Calendar and Google Tasks.
//
// No SDKs: each client is an interface with one method per API call that
// sync_core.ts makes, so tests can swap in the in-memory fakes in
// tests/fakes.ts. Every non-2xx response throws ApiError, and isNotFound is
// the only way code decides something was deleted (see sync_core.ts).

// deno-lint-ignore no-explicit-any
export type Json = any;

export class ApiError extends Error {
  constructor(
    readonly service: "notion" | "google",
    readonly status: number,
    readonly code: string | null,
    message: string,
  ) {
    super(`${service} ${status}${code ? ` ${code}` : ""}: ${message}`);
  }
}

/** True only for a genuine 404/410 from Google or object_not_found from Notion.
 *
 * With DELETE_SYNC on, a swallowed error is a deletion order, so a rate-limit,
 * a 500 or a dropped connection must throw and fail the pass instead.
 */
export function isNotFound(exc: unknown): boolean {
  if (!(exc instanceof ApiError)) return false;
  if (exc.service === "google") return exc.status === 404 || exc.status === 410;
  return exc.code === "object_not_found";
}

// ==== Interfaces =============================================================

export interface Page<T> {
  items: T[];
  nextPageToken?: string | null;
}

export interface NotionApi {
  retrieveDatabase(databaseId: string): Promise<Json>;
  retrieveDataSource(dataSourceId: string): Promise<Json>;
  queryDataSource(
    dataSourceId: string,
    startCursor?: string,
  ): Promise<{ results: Json[]; has_more: boolean; next_cursor: string | null }>;
  retrievePage(pageId: string): Promise<Json>;
  createPage(body: Json): Promise<Json>;
  updatePage(pageId: string, body: Json): Promise<Json>;
}

export interface GCalApi {
  getEvent(eventId: string): Promise<Json>;
  insertEvent(body: Json): Promise<Json>;
  patchEvent(eventId: string, body: Json): Promise<Json>;
  deleteEvent(eventId: string): Promise<void>;
  listEvents(params: Record<string, string>): Promise<Page<Json>>;
  watch(body: Json): Promise<Json>;
}

export interface GTasksApi {
  getTasklist(tasklistId: string): Promise<Json>;
  listTasklists(params: Record<string, string>): Promise<Page<Json>>;
  insertTasklist(body: Json): Promise<Json>;
  getTask(tasklistId: string, taskId: string): Promise<Json>;
  listTasks(tasklistId: string, params: Record<string, string>): Promise<Page<Json>>;
  insertTask(tasklistId: string, body: Json): Promise<Json>;
  patchTask(tasklistId: string, taskId: string, body: Json): Promise<Json>;
  moveTask(tasklistId: string, taskId: string, destinationTasklist: string): Promise<Json>;
  deleteTask(tasklistId: string, taskId: string): Promise<void>;
}

export interface Clients {
  notion: NotionApi;
  gcal: GCalApi;
  gtasks: GTasksApi;
}

// ==== HTTP ===================================================================

const SERVER_ERRORS = new Set([500, 502, 503, 504]);
const MAX_ATTEMPTS = 4;

/** Worth retrying: a 429 (never processed), or a 5xx on a call that is safe
 * to repeat. A POST that 500s may still have created its object, and
 * retrying it would create a duplicate, so it fails the pass instead. */
function isRetryable(status: number, method: string): boolean {
  return status === 429 || (SERVER_ERRORS.has(status) && method !== "POST");
}

/** fetch with retries (see isRetryable); anything still failing throws ApiError. */
async function request(
  service: "notion" | "google",
  url: string,
  init: RequestInit,
): Promise<Json> {
  for (let attempt = 1; ; attempt++) {
    const res = await fetch(url, init);
    if (res.ok) {
      const text = await res.text();
      return text ? JSON.parse(text) : null;
    }
    const text = await res.text();
    if (isRetryable(res.status, init.method ?? "GET") && attempt < MAX_ATTEMPTS) {
      const retryAfter = Number(res.headers.get("Retry-After"));
      const delayMs = retryAfter > 0 ? retryAfter * 1000 : 500 * 2 ** (attempt - 1);
      await new Promise((r) => setTimeout(r, delayMs));
      continue;
    }
    let code: string | null = null;
    try {
      const body = JSON.parse(text);
      code = typeof body.code === "string" ? body.code : body.error?.status ?? null;
    } catch {
      // Non-JSON error body; the status alone decides.
    }
    throw new ApiError(service, res.status, code, text.slice(0, 500));
  }
}

function withQuery(url: string, params: Record<string, string>): string {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== ""),
  ).toString();
  return qs ? `${url}?${qs}` : url;
}

// ==== Notion =================================================================

// Pinned so the 2025 data-source split and the archived → in_trash rename are
// both in effect: rows are queried through data_sources/{id}/query, pages are
// created under a data_source_id parent, and trashing uses `in_trash`.
export const NOTION_VERSION = "2026-03-11";

export function notionClient(token: string): NotionApi {
  const call = (method: string, path: string, body?: Json) =>
    request("notion", `https://api.notion.com/v1/${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  return {
    retrieveDatabase: (id) => call("GET", `databases/${id}`),
    retrieveDataSource: (id) => call("GET", `data_sources/${id}`),
    queryDataSource: (id, startCursor) =>
      call("POST", `data_sources/${id}/query`, startCursor ? { start_cursor: startCursor } : {}),
    retrievePage: (id) => call("GET", `pages/${id}`),
    createPage: (body) => call("POST", "pages", body),
    updatePage: (id, body) => call("PATCH", `pages/${id}`, body),
  };
}

// ==== Google =================================================================

/** Access token from the same GOOGLE_TOKEN_JSON (token.json) the Python version uses. */
export async function googleAccessToken(tokenJson: string): Promise<string> {
  const token = JSON.parse(tokenJson);
  const res = await request("google", token.token_uri ?? "https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: token.client_id,
      client_secret: token.client_secret,
      refresh_token: token.refresh_token,
      grant_type: "refresh_token",
    }),
  });
  return res.access_token;
}

function googleCaller(accessToken: string, base: string) {
  return (method: string, path: string, params: Record<string, string> = {}, body?: Json) =>
    request("google", withQuery(`${base}/${path}`, params), {
      method,
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
}

export function gcalClient(accessToken: string, calendarId: string): GCalApi {
  const call = googleCaller(accessToken, "https://www.googleapis.com/calendar/v3");
  const events = `calendars/${encodeURIComponent(calendarId)}/events`;
  return {
    getEvent: (id) => call("GET", `${events}/${encodeURIComponent(id)}`),
    insertEvent: (body) => call("POST", events, {}, body),
    patchEvent: (id, body) => call("PATCH", `${events}/${encodeURIComponent(id)}`, {}, body),
    deleteEvent: async (id) => {
      await call("DELETE", `${events}/${encodeURIComponent(id)}`);
    },
    listEvents: (params) => call("GET", events, params),
    watch: (body) => call("POST", `${events}/watch`, {}, body),
  };
}

export function gtasksClient(accessToken: string): GTasksApi {
  const call = googleCaller(accessToken, "https://tasks.googleapis.com/tasks/v1");
  const list = (id: string) => `lists/${encodeURIComponent(id)}/tasks`;
  const task = (listId: string, taskId: string) =>
    `${list(listId)}/${encodeURIComponent(taskId)}`;
  return {
    getTasklist: (id) => call("GET", `users/@me/lists/${encodeURIComponent(id)}`),
    listTasklists: (params) => call("GET", "users/@me/lists", params),
    insertTasklist: (body) => call("POST", "users/@me/lists", {}, body),
    getTask: (listId, taskId) => call("GET", task(listId, taskId)),
    listTasks: (listId, params) => call("GET", list(listId), params),
    insertTask: (listId, body) => call("POST", list(listId), {}, body),
    patchTask: (listId, taskId, body) => call("PATCH", task(listId, taskId), {}, body),
    moveTask: (listId, taskId, destinationTasklist) =>
      call("POST", `${task(listId, taskId)}/move`, { destinationTasklist }),
    deleteTask: async (listId, taskId) => {
      await call("DELETE", task(listId, taskId));
    },
  };
}

/** Real clients for one pass, from the function's secrets. */
export async function clientsFromEnv(): Promise<Clients> {
  const env = (name: string) => {
    const value = Deno.env.get(name);
    if (!value) throw new Error(`missing secret ${name}`);
    return value;
  };
  const accessToken = await googleAccessToken(env("GOOGLE_TOKEN_JSON"));
  return {
    notion: notionClient(env("NOTION_TOKEN")),
    gcal: gcalClient(accessToken, Deno.env.get("GOOGLE_CALENDAR_ID") || "primary"),
    gtasks: gtasksClient(accessToken),
  };
}

// ==== Dry run ================================================================

/** Real reads, logged-and-skipped writes: validates a pass against production. */
export function dryRunClients(real: Clients, log: string[]): Clients {
  let n = 0;
  const fakeId = (kind: string) => `dry-run-${kind}-${++n}`;
  const note = (what: string, detail: Json) => {
    log.push(`${what} ${JSON.stringify(detail)}`);
  };
  return {
    notion: {
      ...real.notion,
      createPage: (body) => {
        note("notion.createPage", body);
        return Promise.resolve({ id: fakeId("page"), properties: {} });
      },
      updatePage: (id, body) => {
        note("notion.updatePage", { id, ...body });
        return Promise.resolve({ id });
      },
    },
    gcal: {
      ...real.gcal,
      insertEvent: (body) => {
        note("gcal.insertEvent", body);
        return Promise.resolve({ id: fakeId("event"), ...body });
      },
      patchEvent: (id, body) => {
        note("gcal.patchEvent", { id, ...body });
        return Promise.resolve({ id, ...body });
      },
      deleteEvent: (id) => {
        note("gcal.deleteEvent", { id });
        return Promise.resolve();
      },
      watch: (body) => {
        note("gcal.watch", body);
        return Promise.resolve({});
      },
    },
    gtasks: {
      ...real.gtasks,
      insertTasklist: (body) => {
        note("gtasks.insertTasklist", body);
        return Promise.resolve({ id: fakeId("tasklist"), ...body });
      },
      insertTask: (listId, body) => {
        note("gtasks.insertTask", { listId, ...body });
        return Promise.resolve({ id: fakeId("task"), ...body });
      },
      patchTask: (listId, taskId, body) => {
        note("gtasks.patchTask", { listId, taskId, ...body });
        return Promise.resolve({ id: taskId, ...body });
      },
      moveTask: (listId, taskId, destinationTasklist) => {
        note("gtasks.moveTask", { listId, taskId, destinationTasklist });
        return Promise.resolve({ id: taskId });
      },
      deleteTask: (listId, taskId) => {
        note("gtasks.deleteTask", { listId, taskId });
        return Promise.resolve();
      },
    },
  };
}
