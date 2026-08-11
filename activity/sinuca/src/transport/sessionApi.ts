import type { DashboardSessionPayload } from "../types/dashboard";
import { fetchDashboardJson } from "./httpClient";

export function openDiscordLogin(returnTo = "/dashboard") {
  const safeReturn = returnTo.startsWith("/") && !returnTo.startsWith("//") ? returnTo : "/dashboard";
  window.location.assign(`/api/auth/login?return_to=${encodeURIComponent(safeReturn)}`);
}

export async function fetchDashboardSession(signal?: AbortSignal): Promise<DashboardSessionPayload> {
  return await fetchDashboardJson<DashboardSessionPayload>("/auth/session", { method: "GET", signal }, 10000);
}

export async function logoutDashboard(): Promise<void> {
  await fetchDashboardJson<{ ok: boolean }>("/auth/logout", { method: "POST", body: "{}" }, 10000);
}
