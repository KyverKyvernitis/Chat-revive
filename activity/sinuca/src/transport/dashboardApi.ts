import type {
  DashboardBootstrapPayload,
  DashboardFullPayload,
  DashboardInvitePayload,
  DashboardOptionsPayload,
  DashboardServersPayload,
  DashboardSettingsPayload,
  DashboardSummaryPayload,
  DashboardSectionSummary,
  DashboardSupportServerPayload,
  DashboardUserPayload,
} from "../types/dashboard";
import { DashboardHttpError, fetchDashboardJson, fetchDashboardNdjson } from "./httpClient";


export async function fetchDashboardIdentity(signal?: AbortSignal): Promise<{ ok: boolean; bot?: DashboardUserPayload | null; supportServer?: DashboardSupportServerPayload | null }> {
  return await fetchDashboardJson<{ ok: boolean; bot?: DashboardUserPayload | null; supportServer?: DashboardSupportServerPayload | null }>("/public/identity", { method: "GET", signal }, 10000);
}

export async function fetchDashboardServers(): Promise<DashboardServersPayload> {
  return await fetchDashboardJson<DashboardServersPayload>("/dashboard/servers");
}

export async function fetchDashboardInvite(guildId: string): Promise<DashboardInvitePayload> {
  return await fetchDashboardJson<DashboardInvitePayload>(`/dashboard/guild/${encodeURIComponent(guildId)}/invite`);
}

export async function fetchDashboardBootstrap(guildId: string): Promise<DashboardBootstrapPayload> {
  return await fetchDashboardJson<DashboardBootstrapPayload>(`/dashboard/bootstrap?guild_id=${encodeURIComponent(guildId)}`);
}

export async function fetchDashboardSummary(guildId: string): Promise<DashboardSummaryPayload> {
  return await fetchDashboardJson<DashboardSummaryPayload>(`/dashboard/guild/${encodeURIComponent(guildId)}/summary`);
}

export async function fetchDashboardSettings(guildId: string): Promise<DashboardSettingsPayload> {
  return await fetchDashboardJson<DashboardSettingsPayload>(`/dashboard/guild/${encodeURIComponent(guildId)}/settings`);
}

export async function fetchDashboardOptions(guildId: string): Promise<DashboardOptionsPayload> {
  return await fetchDashboardJson<DashboardOptionsPayload>(`/dashboard/guild/${encodeURIComponent(guildId)}/options`);
}

export async function fetchDashboardFull(
  guildId: string,
  signal?: AbortSignal,
  onProgress?: (progress: number) => void,
): Promise<DashboardFullPayload> {
  const encodedGuildId = encodeURIComponent(guildId);
  let result: DashboardFullPayload | null = null;
  try {
    await fetchDashboardNdjson<unknown>(
      `/dashboard/guild/${encodedGuildId}/full-progress`,
      (event) => {
        if (!event || typeof event !== "object") {
          throw new DashboardHttpError("A API progressiva devolveu um evento desconhecido.", 200, "invalid_progress_event", event);
        }
        const record = event as Record<string, unknown>;
        if (record.type === "progress") {
          const completed = Number(record.completed);
          const total = Number(record.total);
          if (!Number.isFinite(completed) || !Number.isFinite(total) || total <= 0) {
            throw new DashboardHttpError("A API progressiva devolveu um avanço inválido.", 200, "invalid_progress_event", event);
          }
          onProgress?.(Math.min(100, Math.max(0, Math.round((completed / total) * 100))));
          return;
        }
        if (record.type === "result" && record.payload && typeof record.payload === "object") {
          result = record.payload as DashboardFullPayload;
          return;
        }
        if (record.type === "error") {
          const status = Number(record.status) || 500;
          const message = typeof record.error === "string" && record.error.trim()
            ? record.error.trim()
            : "Não foi possível carregar o dashboard.";
          throw new DashboardHttpError(message, status, "stream_error", event);
        }
        throw new DashboardHttpError("A API progressiva devolveu um evento desconhecido.", 200, "invalid_progress_event", event);
      },
      { method: "GET", signal },
      18000,
    );
    if (!result) {
      throw new DashboardHttpError("A API progressiva terminou sem o dashboard.", 200, "missing_stream_result");
    }
    onProgress?.(100);
    return result;
  } catch (error) {
    const canUseLegacyEndpoint = error instanceof DashboardHttpError
      && ([404, 405, 501].includes(error.status) || error.code === "stream_unavailable");
    if (!canUseLegacyEndpoint) throw error;
    const payload = await fetchDashboardJson<DashboardFullPayload>(
      `/dashboard/guild/${encodedGuildId}/full`,
      { method: "GET", signal },
      18000,
    );
    onProgress?.(100);
    return payload;
  }
}

export async function patchDashboardSettings(
  guildId: string,
  updates: Record<string, unknown>,
): Promise<{
  ok: true;
  values: Record<string, unknown>;
  saved: string[];
  revision?: number;
  changed_sections?: string[];
  summary?: DashboardSectionSummary[] | null;
  summary_error?: string;
}> {
  return await fetchDashboardJson(`/dashboard/guild/${encodeURIComponent(guildId)}/settings`, {
    method: "PATCH",
    body: JSON.stringify({ updates }),
  }, 16000);
}
