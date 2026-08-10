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
import { fetchDashboardJson } from "./httpClient";


export async function fetchDashboardIdentity(): Promise<{ ok: boolean; bot?: DashboardUserPayload | null; supportServer?: DashboardSupportServerPayload | null }> {
  return await fetchDashboardJson<{ ok: boolean; bot?: DashboardUserPayload | null; supportServer?: DashboardSupportServerPayload | null }>("/public/identity", { method: "GET" }, 10000);
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

export async function fetchDashboardFull(guildId: string, signal?: AbortSignal): Promise<DashboardFullPayload> {
  return await fetchDashboardJson<DashboardFullPayload>(
    `/dashboard/guild/${encodeURIComponent(guildId)}/full`,
    { method: "GET", signal },
    18000,
  );
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
