import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export interface DashboardCommandContext {
  prefixes: Record<string, string>;
  gamesMode: "commands" | "triggers";
}

export interface DashboardCommandCategory {
  key: string;
  label: string;
  emoji: string;
  description: string;
}

export interface DashboardCommandEntry {
  key: string;
  category: string;
  group: string;
  description: string;
  usage: string;
  aliases: string[];
  keywords: string[];
}

export interface DashboardCommandsPayload {
  catalogVersion: number;
  musicAvailable: boolean;
  categories: DashboardCommandCategory[];
  commands: DashboardCommandEntry[];
}

type RawCategory = {
  key?: unknown;
  label?: unknown;
  emoji?: unknown;
  description?: unknown;
};

type RawEntry = {
  key?: unknown;
  category?: unknown;
  group?: unknown;
  description?: unknown;
  usage?: unknown;
  search_terms?: unknown;
  aliases?: unknown;
  permission?: unknown;
  show_in_category?: unknown;
};

type RawCatalog = {
  version: number;
  categories: RawCategory[];
  entries: RawEntry[];
};

const MUSIC_CACHE_MS = 5_000;
const DEFAULT_WORKER_OFFLINE_SECONDS = 90;
const UTILITIES_CATEGORY: DashboardCommandCategory = {
  key: "utilities",
  label: "Utilidades",
  emoji: "⌘",
  description: "Consulte a ajuda e confira o estado do bot.",
};

let catalogCache: RawCatalog | null = null;
let musicCache: { checkedAt: number; available: boolean } | null = null;

function catalogCandidates(): string[] {
  return [
    fileURLToPath(new URL("../data/help_catalog.json", import.meta.url)),
    fileURLToPath(new URL("../../../../shared/help_catalog.json", import.meta.url)),
  ];
}

function loadCatalog(): RawCatalog {
  if (catalogCache) return catalogCache;
  const path = catalogCandidates().find((candidate) => existsSync(candidate));
  if (!path) throw new Error("help_catalog_missing");
  const parsed = JSON.parse(readFileSync(path, "utf8")) as Partial<RawCatalog>;
  if (parsed.version !== 1 || !Array.isArray(parsed.categories) || !Array.isArray(parsed.entries)) {
    throw new Error("help_catalog_invalid");
  }
  catalogCache = { version: parsed.version, categories: parsed.categories, entries: parsed.entries };
  return catalogCache;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function workerRegistryPath(): string {
  const configured = String(process.env.CORE_WORKERS_REGISTRY_PATH || "").trim();
  if (configured) return resolve(configured);
  return fileURLToPath(new URL("../../../../data/core_workers_registry.json", import.meta.url));
}

function normalizedCapabilities(record: Record<string, unknown>): Set<string> {
  const values = [
    ...stringList(record.roles),
    ...stringList(record.manual_roles),
    ...stringList(record.capabilities),
    ...stringList(record.manual_capabilities),
  ];
  return new Set(values.map((value) => value.toLowerCase().replace(/_/g, "-")));
}

function timestampSeconds(value: unknown): number {
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) return numeric;
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

function musicWorkerAvailable(): boolean {
  const now = Date.now();
  if (musicCache && now - musicCache.checkedAt <= MUSIC_CACHE_MS) return musicCache.available;

  let available = false;
  try {
    const raw = JSON.parse(readFileSync(workerRegistryPath(), "utf8")) as { workers?: unknown };
    const records = raw.workers && typeof raw.workers === "object" && !Array.isArray(raw.workers)
      ? Object.values(raw.workers as Record<string, unknown>)
      : [];
    const offlineAfter = Math.max(15, Number.parseInt(String(process.env.CORE_WORKER_OFFLINE_AFTER_SECONDS || DEFAULT_WORKER_OFFLINE_SECONDS), 10) || DEFAULT_WORKER_OFFLINE_SECONDS);
    const nowSeconds = now / 1000;
    available = records.some((candidate) => {
      if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return false;
      const worker = candidate as Record<string, unknown>;
      if (worker.enabled === false) return false;
      const runtimeKind = String(worker.runtime_kind || "").trim().toLowerCase();
      const source = String(worker.source || "").trim().toLowerCase();
      if (runtimeKind === "apk" || source.startsWith("core-worker-apk")) return false;
      const lastSeen = timestampSeconds(worker.last_heartbeat_at || worker.updated_at);
      if (!lastSeen || nowSeconds - lastSeen > offlineAfter) return false;
      const capabilities = normalizedCapabilities(worker);
      return capabilities.has("phone-worker") && capabilities.has("music");
    });
  } catch {
    available = false;
  }

  musicCache = { checkedAt: now, available };
  return available;
}

function resolveUsage(rawUsage: unknown, context: DashboardCommandContext, category: string): string {
  const usage = String(rawUsage || "").replace(/\{([a-z0-9_]+)\}/gi, (match, key: string) => context.prefixes[key] ?? match);
  if (category !== "games" || context.gamesMode !== "commands") return usage;
  const prefix = context.prefixes.bot_prefix || "_";
  return usage.startsWith(prefix) ? usage : `${prefix}${usage}`;
}

function resolveAliases(entry: RawEntry, context: DashboardCommandContext, category: string): string[] {
  const aliases = stringList(entry.aliases);
  const prefixed = String(entry.usage || "").includes("{bot_prefix}")
    || (category === "games" && context.gamesMode === "commands");
  if (!prefixed) return aliases;
  const prefix = context.prefixes.bot_prefix || "_";
  return aliases.map((alias) => alias.startsWith(prefix) ? alias : `${prefix}${alias}`);
}

export function buildDashboardCommands(context: DashboardCommandContext): DashboardCommandsPayload {
  const catalog = loadCatalog();
  const musicAvailable = musicWorkerAvailable();
  const commands: DashboardCommandEntry[] = [];

  for (const entry of catalog.entries) {
    if (String(entry.permission || "user") !== "user" || entry.show_in_category === false) continue;
    const sourceCategory = entry.category === null || entry.category === undefined ? "utilities" : String(entry.category).trim();
    if (!sourceCategory || sourceCategory === "server" || (sourceCategory === "music" && !musicAvailable)) continue;
    const key = String(entry.key || "").trim();
    const description = String(entry.description || "").trim();
    const usage = resolveUsage(entry.usage, context, sourceCategory);
    if (!key || !description || !usage) continue;
    commands.push({
      key,
      category: sourceCategory,
      group: String(entry.group || "").trim() || UTILITIES_CATEGORY.label,
      description,
      usage,
      aliases: resolveAliases(entry, context, sourceCategory),
      keywords: stringList(entry.search_terms),
    });
  }

  const usedCategories = new Set(commands.map((entry) => entry.category));
  const categories = catalog.categories
    .map((category) => ({
      key: String(category.key || "").trim(),
      label: String(category.label || "").trim(),
      emoji: String(category.emoji || "").trim(),
      description: String(category.description || "").trim(),
    }))
    .filter((category) => category.key !== "server" && usedCategories.has(category.key));
  if (usedCategories.has(UTILITIES_CATEGORY.key)) categories.push(UTILITIES_CATEGORY);

  return { catalogVersion: catalog.version, musicAvailable, categories, commands };
}

export function resetDashboardCommandsCachesForTests(): void {
  catalogCache = null;
  musicCache = null;
}
