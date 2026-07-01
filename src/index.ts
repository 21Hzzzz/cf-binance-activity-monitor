type Env = {
  STATE: KVNamespace;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_CHAT_ID?: string;
  TELEGRAM_MESSAGE_THREAD_ID?: string;
  MONITOR_AUTH_TOKEN?: string;
  LANG?: string;
  STATE_KEY?: string;
  RESOURCE_BATCH_SIZE?: string;
  RESOURCE_BATCHES_PER_RUN?: string;
  RESOURCE_PROBE_BATCHES_PER_RUN?: string;
  RESOURCE_SCAN_START_ID?: string;
  RESOURCE_BACKTRACK?: string;
  RESOURCE_RECENT_AHEAD?: string;
  ALERT_ON_FIRST_RUN?: string;
};

type MonitorState = {
  version: 1;
  initializedAt?: string;
  runCount: number;
  maxObservedResourceId: number;
  nextProbeResourceId?: number;
  seenResourceIds: string[];
  seenResourceCodes: string[];
  seenBannerKeys: string[];
  lastRunAt?: string;
  lastAlertAt?: string;
  lastErrorAt?: string;
  lastErrorSignature?: string;
  lastScan?: LastScan;
};

type LastScan = {
  at: string;
  dryRun: boolean;
  firstRun: boolean;
  resourceBatches: number;
  resourceIdsScanned: number;
  resourceMatches: number;
  bannerMatches: number;
  alerts: number;
  recentWindow?: IdWindow;
  probeWindow?: IdWindow;
  errors: string[];
};

type IdWindow = {
  low: number;
  high: number;
};

type Alert = {
  source: "banner" | "resource";
  title: string;
  url: string;
  details: string[];
};

const BANNER_URL =
  "https://www.binance.com/bapi/apex/v2/friendly/apex/marketing/banners";
const RESOURCE_LIST_URL =
  "https://www.binance.com/bapi/composite/v1/public/growth-paas/resource/list";
const BINANCE_BASE_URL = "https://www.binance.com";
const DEFAULT_STATE_KEY = "monitor-state-v1";
const SEEN_LIMIT = 3000;
const MIN_RESOURCE_REQUEST_IDS = 5;

export default {
  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(
      runMonitor(env, { dryRun: false }).catch((error) => {
        console.error("scheduled run failed", error);
      }),
    );
  },

  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);

    if (url.pathname === "/" || url.pathname === "/status") {
      const state = await loadState(env);
      return jsonResponse({
        ok: true,
        service: "binance-activity-monitor",
        lastRunAt: state.lastRunAt,
        runCount: state.runCount,
        maxObservedResourceId: state.maxObservedResourceId,
        nextProbeResourceId: state.nextProbeResourceId,
        lastScan: state.lastScan,
      });
    }

    if (url.pathname === "/run") {
      const auth = authorizeManualRun(request, env);
      if (!auth.ok) {
        return jsonResponse({ ok: false, error: auth.error }, 403);
      }

      const dryRun = url.searchParams.get("dry") === "1";
      const result = await runMonitor(env, { dryRun });
      return jsonResponse(result);
    }

    return jsonResponse({ ok: false, error: "not_found" }, 404);
  },
};

async function runMonitor(env: Env, options: { dryRun: boolean }) {
  const now = new Date().toISOString();
  const state = await loadState(env);
  const firstRun = !state.initializedAt;
  const shouldAlert = !firstRun || parseBool(env.ALERT_ON_FIRST_RUN, false);
  const alerts: Alert[] = [];
  const errors: string[] = [];

  const bannerResult = await scanBanners(env, state, shouldAlert).catch((error) => {
    errors.push(errorMessage("banner", error));
    return { matches: 0, alerts: [] as Alert[] };
  });

  const resourcePlan = planResourceScan(env, state);
  const resourceResult = await scanResources(env, state, resourcePlan, shouldAlert).catch(
    (error) => {
      errors.push(errorMessage("resource", error));
      return { matches: 0, alerts: [] as Alert[], maxSeenId: state.maxObservedResourceId };
    },
  );

  alerts.push(...bannerResult.alerts ?? []);
  alerts.push(...resourceResult.alerts);

  if (!options.dryRun && alerts.length > 0) {
    await sendTelegram(env, formatAlerts(alerts, now));
    state.lastAlertAt = now;
  }

  if (!options.dryRun && errors.length > 0) {
    await maybeSendErrorAlert(env, state, errors, now);
  }

  const lastScan: LastScan = {
    at: now,
    dryRun: options.dryRun,
    firstRun,
    resourceBatches: resourcePlan.batches.length,
    resourceIdsScanned: resourcePlan.idsScanned,
    resourceMatches: resourceResult.matches,
    bannerMatches: bannerResult.matches,
    alerts: shouldAlert ? alerts.length : 0,
    recentWindow: resourcePlan.recentWindow,
    probeWindow: resourcePlan.probeWindow,
    errors,
  };

  if (!options.dryRun) {
    state.version = 1;
    state.initializedAt ||= now;
    state.runCount += 1;
    state.lastRunAt = now;
    state.lastScan = lastScan;
    state.maxObservedResourceId = Math.max(
      state.maxObservedResourceId,
      resourceResult.maxSeenId,
    );
    state.nextProbeResourceId = resourcePlan.nextProbeResourceId;
    pruneState(state);
    await saveState(env, state);
  }

  return {
    ok: errors.length === 0,
    dryRun: options.dryRun,
    firstRun,
    alerts: shouldAlert ? alerts : [],
    suppressedFirstRunAlerts: shouldAlert ? 0 : alerts.length,
    lastScan,
    maxObservedResourceId: state.maxObservedResourceId,
    nextProbeResourceId: state.nextProbeResourceId,
  };
}

async function scanBanners(env: Env, state: MonitorState, shouldAlert: boolean) {
  const payload = await fetchJson(BANNER_URL, {
    method: "GET",
    headers: binanceHeaders(env),
  });
  const banners = extractArray(payload);
  const alerts: Alert[] = [];
  let matches = 0;

  for (const banner of banners) {
    const link = getString(banner, ["link", "url", "webLink", "jumpUrl", "landingUrl"]);
    if (!link) continue;

    matches += 1;
    const title = getString(banner, ["title", "name", "bannerTitle"]) || "Binance banner";
    const id = getString(banner, ["id", "bannerId", "resourceId"]);
    const key = id ? `id:${id}` : `link:${link}`;
    const isNew = remember(state.seenBannerKeys, key);

    if (isNew && shouldAlert) {
      alerts.push({
        source: "banner",
        title,
        url: normalizeBinanceUrl(link, env.LANG || "zh-CN"),
        details: [
          id ? `id: ${id}` : "",
          formatTimeDetail("start", getNumber(banner, ["startTime", "startAt"])),
          formatTimeDetail("end", getNumber(banner, ["endTime", "endAt"])),
        ].filter(Boolean),
      });
    }
  }

  return { matches, alerts };
}

async function scanResources(
  env: Env,
  state: MonitorState,
  plan: ResourceScanPlan,
  shouldAlert: boolean,
) {
  const alerts: Alert[] = [];
  let matches = 0;
  let maxSeenId = state.maxObservedResourceId;

  for (const ids of plan.batches) {
    const requestIds = padResourceRequestIds(ids);
    const payload = await fetchJson(RESOURCE_LIST_URL, {
      method: "POST",
      headers: {
        ...binanceHeaders(env),
        "content-type": "application/json",
      },
      body: JSON.stringify({
        idList: requestIds,
        pageIndex: 1,
        pageSize: requestIds.length,
      }),
    });

    for (const resource of extractArray(payload)) {
      const id = getNumber(resource, ["id", "resourceId"]);
      if (id) maxSeenId = Math.max(maxSeenId, id);

      const type = getString(resource, ["type"]);
      const code = getString(resource, ["code"]);
      const uri = getResourceUri(resource);
      if (!uri || !uri.startsWith("/activity/chance/")) continue;

      matches += 1;
      const idKey = id ? String(id) : `code:${code || uri}`;
      const codeKey = code || uri;
      const isNewId = remember(state.seenResourceIds, idKey);
      remember(state.seenResourceCodes, codeKey);

      if (isNewId && shouldAlert) {
        alerts.push({
          source: "resource",
          title: getResourceTitle(resource) || code || uri,
          url: normalizeBinanceUrl(uri, env.LANG || "zh-CN"),
          details: [
            id ? `id: ${id}` : "",
            code ? `code: ${code}` : "",
            type ? `type: ${type}` : "",
            getString(resource, ["status"]) ? `status: ${getString(resource, ["status"])}` : "",
            formatTimeDetail("published", getNumber(resource, ["publishedTime"])),
          ].filter(Boolean),
        });
      }
    }
  }

  return { matches, alerts, maxSeenId };
}

function padResourceRequestIds(ids: number[]): number[] {
  if (ids.length === 0 || ids.length >= MIN_RESOURCE_REQUEST_IDS) return ids;

  // Binance rejects very short idList payloads, so pad tiny tail batches.
  const padded = [...ids];
  let previous = ids[0] - 1;
  while (padded.length < MIN_RESOURCE_REQUEST_IDS && previous > 0) {
    padded.unshift(previous);
    previous -= 1;
  }

  let next = ids[ids.length - 1] + 1;
  while (padded.length < MIN_RESOURCE_REQUEST_IDS) {
    padded.push(next);
    next += 1;
  }

  return padded;
}

type ResourceScanPlan = {
  batches: number[][];
  idsScanned: number;
  recentWindow?: IdWindow;
  probeWindow?: IdWindow;
  nextProbeResourceId?: number;
};

function planResourceScan(env: Env, state: MonitorState): ResourceScanPlan {
  const batchSize = clamp(readInt(env.RESOURCE_BATCH_SIZE, 100), 1, 100);
  const maxBatches = clamp(readInt(env.RESOURCE_BATCHES_PER_RUN, 35), 1, 40);
  const probeBatches = clamp(
    readInt(env.RESOURCE_PROBE_BATCHES_PER_RUN, 10),
    0,
    maxBatches,
  );
  const recentBatches = maxBatches - probeBatches;
  const startId = readInt(env.RESOURCE_SCAN_START_ID, 100003800);
  const backtrack = readInt(env.RESOURCE_BACKTRACK, 800);
  const recentAhead = readInt(env.RESOURCE_RECENT_AHEAD, 1700);
  const maxObserved = state.maxObservedResourceId || startId;

  const batches: number[][] = [];
  let recentWindow: IdWindow | undefined;
  let probeWindow: IdWindow | undefined;

  if (recentBatches > 0) {
    const recentCapacity = recentBatches * batchSize;
    const low = Math.max(startId, maxObserved - backtrack);
    const desiredHigh = Math.max(low, maxObserved + recentAhead);
    const high = Math.min(desiredHigh, low + recentCapacity - 1);
    recentWindow = { low, high };
    batches.push(...buildBatches(low, high, batchSize));
  }

  let nextProbeResourceId: number | undefined = state.nextProbeResourceId;
  if (probeBatches > 0) {
    const recentHigh = recentWindow?.high ?? startId - 1;
    const probeLow = Math.max(nextProbeResourceId || recentHigh + 1, recentHigh + 1);
    const probeHigh = probeLow + probeBatches * batchSize - 1;
    probeWindow = { low: probeLow, high: probeHigh };
    nextProbeResourceId = probeHigh + 1;
    batches.push(...buildBatches(probeLow, probeHigh, batchSize));
  }

  return {
    batches: batches.slice(0, maxBatches),
    idsScanned: batches.reduce((total, batch) => total + batch.length, 0),
    recentWindow,
    probeWindow,
    nextProbeResourceId,
  };
}

function buildBatches(low: number, high: number, batchSize: number): number[][] {
  const batches: number[][] = [];
  for (let start = low; start <= high; start += batchSize) {
    const ids: number[] = [];
    const end = Math.min(high, start + batchSize - 1);
    for (let id = start; id <= end; id += 1) ids.push(id);
    batches.push(ids);
  }
  return batches;
}

async function fetchJson(url: string, init: RequestInit): Promise<unknown> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);

  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    const text = await response.text();

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${text.slice(0, 180)}`);
    }

    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new Error(`non-json response: ${text.slice(0, 180)}`);
    }
  } finally {
    clearTimeout(timeout);
  }
}

function binanceHeaders(env: Env): Record<string, string> {
  return {
    accept: "application/json, text/plain, */*",
    clienttype: "android",
    lang: env.LANG || "zh-CN",
    "user-agent": "Binance/3.16.7 Android",
  };
}

async function sendTelegram(env: Env, text: string) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    throw new Error("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID");
  }

  const messages = splitTelegramMessage(text);
  for (const message of messages) {
    const body: Record<string, unknown> = {
      chat_id: env.TELEGRAM_CHAT_ID,
      text: message,
      disable_web_page_preview: true,
    };
    if (env.TELEGRAM_MESSAGE_THREAD_ID) {
      body.message_thread_id = Number(env.TELEGRAM_MESSAGE_THREAD_ID);
    }

    const response = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
    );

    if (!response.ok) {
      const responseText = await response.text();
      throw new Error(`telegram HTTP ${response.status}: ${responseText.slice(0, 180)}`);
    }
  }
}

async function maybeSendErrorAlert(
  env: Env,
  state: MonitorState,
  errors: string[],
  now: string,
) {
  const signature = errors.join(" | ");
  const lastErrorAt = state.lastErrorAt ? Date.parse(state.lastErrorAt) : 0;
  const cooldownMs = 60 * 60 * 1000;
  const shouldSend =
    signature !== state.lastErrorSignature || Date.parse(now) - lastErrorAt > cooldownMs;

  state.lastErrorAt = now;
  state.lastErrorSignature = signature;

  if (shouldSend) {
    await sendTelegram(env, `Binance activity monitor error\n${signature}`);
  }
}

function formatAlerts(alerts: Alert[], now: string): string {
  const lines = [`Binance activity monitor found ${alerts.length} new item(s)`, now, ""];

  for (const alert of alerts) {
    lines.push(`[${alert.source}] ${alert.title}`);
    lines.push(alert.url);
    for (const detail of alert.details) lines.push(detail);
    lines.push("");
  }

  return lines.join("\n").trim();
}

function splitTelegramMessage(text: string): string[] {
  const limit = 3900;
  if (text.length <= limit) return [text];

  const parts: string[] = [];
  let remaining = text;
  while (remaining.length > limit) {
    let splitAt = remaining.lastIndexOf("\n\n", limit);
    if (splitAt < 1) splitAt = limit;
    parts.push(remaining.slice(0, splitAt));
    remaining = remaining.slice(splitAt).trimStart();
  }
  if (remaining) parts.push(remaining);
  return parts;
}

async function loadState(env: Env): Promise<MonitorState> {
  const key = env.STATE_KEY || DEFAULT_STATE_KEY;
  const saved = await env.STATE.get(key, "json");
  if (isState(saved)) return saved;

  return {
    version: 1,
    runCount: 0,
    maxObservedResourceId: readInt(env.RESOURCE_SCAN_START_ID, 100003800),
    seenResourceIds: [],
    seenResourceCodes: [],
    seenBannerKeys: [],
  };
}

async function saveState(env: Env, state: MonitorState) {
  const key = env.STATE_KEY || DEFAULT_STATE_KEY;
  await env.STATE.put(key, JSON.stringify(state));
}

function isState(value: unknown): value is MonitorState {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<MonitorState>;
  return (
    candidate.version === 1 &&
    Array.isArray(candidate.seenResourceIds) &&
    Array.isArray(candidate.seenResourceCodes) &&
    Array.isArray(candidate.seenBannerKeys)
  );
}

function pruneState(state: MonitorState) {
  state.seenResourceIds = state.seenResourceIds.slice(-SEEN_LIMIT);
  state.seenResourceCodes = state.seenResourceCodes.slice(-SEEN_LIMIT);
  state.seenBannerKeys = state.seenBannerKeys.slice(-SEEN_LIMIT);
}

function remember(values: string[], value: string): boolean {
  if (values.includes(value)) return false;
  values.push(value);
  return true;
}

function extractArray(payload: unknown): unknown[] {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  const root = payload as Record<string, unknown>;
  const data = root.data;
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== "object") return [];
  const dataRecord = data as Record<string, unknown>;

  for (const key of ["list", "rows", "items", "resources", "banners", "data"]) {
    const value = dataRecord[key];
    if (Array.isArray(value)) return value;
  }

  return [];
}

function getResourceUri(resource: unknown): string {
  if (!resource || typeof resource !== "object") return "";
  const record = resource as Record<string, unknown>;
  const globalContent = record.globalContent;
  if (globalContent && typeof globalContent === "object") {
    const uri = (globalContent as Record<string, unknown>).uri;
    if (typeof uri === "string") return uri;
  }
  return getString(resource, ["uri", "url", "link"]);
}

function getResourceTitle(resource: unknown): string {
  const direct = getString(resource, ["title", "name"]);
  if (direct) return direct;
  if (!resource || typeof resource !== "object") return "";

  const record = resource as Record<string, unknown>;
  for (const containerKey of ["localizedContent", "globalContent", "content"]) {
    const container = record[containerKey];
    const title = findNestedTitle(container);
    if (title) return title;
  }
  return "";
}

function findNestedTitle(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  for (const key of ["title", "name", "activityTitle"]) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate) return candidate;
  }
  for (const child of Object.values(record)) {
    if (child && typeof child === "object") {
      const nested = findNestedTitle(child);
      if (nested) return nested;
    }
  }
  return "";
}

function getString(value: unknown, keys: string[]): string {
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  for (const key of keys) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate) return candidate;
    if (typeof candidate === "number") return String(candidate);
  }
  return "";
}

function getNumber(value: unknown, keys: string[]): number | undefined {
  if (!value || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  for (const key of keys) {
    const candidate = record[key];
    if (typeof candidate === "number" && Number.isFinite(candidate)) return candidate;
    if (typeof candidate === "string" && candidate && Number.isFinite(Number(candidate))) {
      return Number(candidate);
    }
  }
  return undefined;
}

function normalizeBinanceUrl(value: string, lang: string): string {
  if (value.startsWith("http://") || value.startsWith("https://")) return value;
  if (!value.startsWith("/")) return `${BINANCE_BASE_URL}/${value}`;
  if (value.startsWith(`/${lang}/`)) return `${BINANCE_BASE_URL}${value}`;
  if (value.startsWith("/activity/")) return `${BINANCE_BASE_URL}/${lang}${value}`;
  return `${BINANCE_BASE_URL}${value}`;
}

function formatTimeDetail(label: string, timestamp?: number): string {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "";
  const text = date.toLocaleString("zh-CN", {
    timeZone: "Asia/Hong_Kong",
    hour12: false,
  });
  return `${label}: ${text} HKT`;
}

function authorizeManualRun(request: Request, env: Env): { ok: true } | { ok: false; error: string } {
  if (!env.MONITOR_AUTH_TOKEN) {
    return { ok: false, error: "MONITOR_AUTH_TOKEN is not configured" };
  }

  const url = new URL(request.url);
  const token = url.searchParams.get("token");
  const authorization = request.headers.get("authorization") || "";
  const bearer = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";

  if (token === env.MONITOR_AUTH_TOKEN || bearer === env.MONITOR_AUTH_TOKEN) {
    return { ok: true };
  }

  return { ok: false, error: "unauthorized" };
}

function readInt(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

function parseBool(value: string | undefined, fallback: boolean): boolean {
  if (!value) return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
}

function errorMessage(label: string, error: unknown): string {
  if (error instanceof Error) return `${label}: ${error.message}`;
  return `${label}: ${String(error)}`;
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
