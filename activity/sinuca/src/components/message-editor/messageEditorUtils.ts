import type {
  DashboardFieldDefinition,
  DashboardTemplateSyntax,
} from "../../types/dashboard";
import type { JsonFieldChange } from "./messageEditorTypes";

export function formatTemplateVariable(syntax: DashboardTemplateSyntax, key: string): string {
  return syntax === "dollar_curly" ? `\${${key}}` : `{${key}}`;
}

function valueForJson(field: DashboardFieldDefinition, value: unknown): unknown {
  if (field.type === "boolean") return Boolean(value);
  if (field.type === "number") {
    const numeric = typeof value === "number" ? value : Number(value);
    return Number.isFinite(numeric) ? numeric : 0;
  }
  if (value === null || value === undefined) return "";
  return String(value);
}

export function messageFieldsObject(
  fields: DashboardFieldDefinition[],
  draft: Record<string, unknown>,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const field of fields) {
    payload[field.id] = valueForJson(field, draft[field.id]);
  }
  return payload;
}

export function serializeMessageFields(
  fields: DashboardFieldDefinition[],
  draft: Record<string, unknown>,
): string {
  return JSON.stringify(messageFieldsObject(fields, draft), null, 2);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeStringField(field: DashboardFieldDefinition, raw: unknown): string {
  if (typeof raw !== "string") {
    throw new Error(`“${field.id}” precisa ser uma string.`);
  }
  if (field.maxLength !== undefined && raw.length > field.maxLength) {
    throw new Error(`“${field.id}” excede o limite de ${field.maxLength} caracteres.`);
  }

  if (field.type === "select") {
    const allowed = new Set((field.options ?? []).map((option) => option.value));
    if (!allowed.has(raw)) {
      throw new Error(`“${field.id}” possui uma opção inválida.`);
    }
  }

  if (field.type === "url" && raw.trim()) {
    let parsed: URL;
    try {
      parsed = new URL(raw.trim());
    } catch {
      throw new Error(`“${field.id}” precisa conter uma URL válida.`);
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error(`“${field.id}” aceita apenas URLs HTTP ou HTTPS.`);
    }
    return raw.trim();
  }

  if (field.type === "color" && raw.trim()) {
    const color = raw.trim().startsWith("#") ? raw.trim() : `#${raw.trim()}`;
    if (!/^#[0-9a-fA-F]{6}$/.test(color)) {
      throw new Error(`“${field.id}” precisa usar uma cor hexadecimal como #5865F2.`);
    }
    return color.toUpperCase();
  }

  if ((field.type === "channel" || field.type === "role") && raw.trim() && !/^\d{15,25}$/.test(raw.trim())) {
    throw new Error(`“${field.id}” precisa conter um ID válido.`);
  }

  return raw;
}

function normalizeJsonValue(field: DashboardFieldDefinition, raw: unknown): { raw: string | boolean; expected: unknown } {
  if (field.type === "boolean") {
    if (typeof raw !== "boolean") {
      throw new Error(`“${field.id}” precisa ser true ou false.`);
    }
    return { raw, expected: raw };
  }

  if (field.type === "number") {
    if (typeof raw !== "number" || !Number.isFinite(raw) || !Number.isInteger(raw)) {
      throw new Error(`“${field.id}” precisa ser um número inteiro.`);
    }
    if (field.min !== undefined && raw < field.min) {
      throw new Error(`“${field.id}” não pode ser menor que ${field.min}.`);
    }
    if (field.max !== undefined && raw > field.max) {
      throw new Error(`“${field.id}” não pode ser maior que ${field.max}.`);
    }
    return { raw: String(raw), expected: raw };
  }

  const normalized = normalizeStringField(field, raw);
  return { raw: normalized, expected: normalized };
}

function comparableCurrentValue(field: DashboardFieldDefinition, value: unknown): unknown {
  if (field.type === "boolean") return Boolean(value);
  if (field.type === "number") {
    const numeric = typeof value === "number" ? value : Number(value);
    return Number.isFinite(numeric) ? Math.trunc(numeric) : 0;
  }
  if (value === null || value === undefined) return "";
  return String(value);
}

export function parseMessageJson(
  text: string,
  fields: DashboardFieldDefinition[],
  draft: Record<string, unknown>,
  baseline: Record<string, unknown>,
): JsonFieldChange[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const message = error instanceof Error ? error.message : "JSON inválido";
    throw new Error(`Não foi possível ler o JSON: ${message}`);
  }

  if (!isPlainObject(parsed)) {
    throw new Error("O JSON precisa ser um objeto plano.");
  }

  const fieldsById = new Map(fields.map((field) => [field.id, field]));
  const unknownKeys = Object.keys(parsed).filter((key) => !fieldsById.has(key));
  if (unknownKeys.length > 0) {
    throw new Error(`Chave desconhecida: ${unknownKeys[0]}.`);
  }

  const changes: JsonFieldChange[] = [];
  for (const [key, rawValue] of Object.entries(parsed)) {
    const field = fieldsById.get(key);
    if (!field) continue;
    if (Object.is(rawValue, baseline[field.id])) continue;

    const current = comparableCurrentValue(field, draft[field.id]);
    const samePrimitiveType = field.type === "boolean"
      ? typeof rawValue === "boolean"
      : field.type === "number"
        ? typeof rawValue === "number"
        : typeof rawValue === "string";
    if (samePrimitiveType && Object.is(current, rawValue)) continue;

    const normalized = normalizeJsonValue(field, rawValue);
    if (!Object.is(current, normalized.expected)) {
      changes.push({ field, raw: normalized.raw, expected: normalized.expected });
    }
  }
  return changes;
}

export function pendingChangesReachedDraft(
  changes: JsonFieldChange[],
  draft: Record<string, unknown>,
): boolean {
  return changes.every(({ field, expected }) => {
    const current = comparableCurrentValue(field, draft[field.id]);
    return Object.is(current, expected);
  });
}

export function readableFieldLabel(field: DashboardFieldDefinition): string {
  return field.label
    .replace(/^Embed:\s*/i, "")
    .replace(/^DM:\s*/i, "")
    .trim();
}

export function normalizePreviewUrl(value: unknown): string {
  if (typeof value !== "string") return "";
  let normalized = value.trim();
  if (!normalized) return "";
  if ((normalized.startsWith("<") && normalized.endsWith(">"))
    || (normalized.startsWith('"') && normalized.endsWith('"'))
    || (normalized.startsWith("'") && normalized.endsWith("'"))) {
    normalized = normalized.slice(1, -1).trim();
  }
  normalized = normalized.replace(/&amp;/gi, "&");
  return normalized;
}

export function isValidPreviewUrl(value: unknown): value is string {
  const normalized = normalizePreviewUrl(value);
  if (!normalized) return false;
  try {
    const parsed = new URL(normalized);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}
