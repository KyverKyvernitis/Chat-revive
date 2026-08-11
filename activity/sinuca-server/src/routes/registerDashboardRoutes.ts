import type { Express, Request, Response } from "express";
import { DashboardConfigValidationError, type DashboardConfigService } from "../services/dashboardConfigService.js";
import type { DashboardOAuthTokenResult, DashboardSessionService } from "../services/dashboardSessionService.js";
import {
  createDashboardInviteUrl,
  getDiscordBotIdentity,
  getDiscordSupportServerIdentity,
  getDiscordUserIdentity,
  listDashboardServers,
  listGuildChannelsAndRoles,
  verifyDashboardAccess,
  verifyDashboardInviteAccess,
} from "../services/discordAuthService.js";

export interface RegisterDashboardRoutesOptions {
  app: Express;
  configService: DashboardConfigService;
  sessionService: DashboardSessionService;
  discordClientId: string;
  publicOrigin: string;
  allowedOrigins: Set<string>;
  exchangeDiscordCode(code: string, redirectUri: string): Promise<DashboardOAuthTokenResult>;
}

type SessionAuth = {
  accessToken: string;
  user: {
    id: string;
    username?: string | null;
    global_name?: string | null;
    avatar?: string | null;
    avatarUrl?: string | null;
  };
};

type GuildAuth = SessionAuth & { guildId: string };
type DashboardLoadStep = "settings" | "summary" | "options" | "bot";

const rateBuckets = new Map<string, { count: number; resetAt: number }>();
const MAX_RATE_BUCKETS = 10_000;
let rateLimitChecks = 0;

function sendNoStoreJson(res: Response, status: number, payload: unknown) {
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.type("application/json");
  res.status(status).json(payload);
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "bigint") return String(value).trim();
  }
  return "";
}

function dashboardGuildId(req: Request): string {
  const body = req.body && typeof req.body === "object" ? req.body as Record<string, unknown> : {};
  return firstString(req.params.guildId, req.query.guild_id, body.guild_id, body.guildId);
}

function isSecureRequest(req: Request): boolean {
  return req.secure || firstString(req.headers["x-forwarded-proto"]).split(",")[0].trim().toLowerCase() === "https";
}

function requestOrigin(req: Request, configuredOrigin: string): string {
  if (configuredOrigin) return configuredOrigin;
  if (process.env.NODE_ENV === "production") return "";
  const protocol = isSecureRequest(req) ? "https" : "http";
  const host = firstString(req.headers["x-forwarded-host"], req.headers.host);
  return host ? `${protocol}://${host}` : "";
}

function callbackUrl(req: Request, configuredOrigin: string): string {
  const origin = requestOrigin(req, configuredOrigin);
  return origin ? `${origin}/api/auth/callback` : "";
}

function mutationOriginAllowed(req: Request, configuredOrigin: string, allowedOrigins: Set<string>): boolean {
  const origin = firstString(req.headers.origin);
  if (!origin) return true;
  try {
    const normalized = new URL(origin).origin;
    const expected = requestOrigin(req, configuredOrigin);
    return normalized === expected || allowedOrigins.has(normalized);
  } catch {
    return false;
  }
}

function takeRateLimit(req: Request, bucket: string, limit: number, windowMs: number): boolean {
  const key = `${bucket}:${req.ip || req.socket.remoteAddress || "unknown"}`;
  const now = Date.now();
  rateLimitChecks += 1;
  if (rateLimitChecks % 128 === 0 || rateBuckets.size >= MAX_RATE_BUCKETS) {
    for (const [storedKey, entry] of rateBuckets) {
      if (entry.resetAt <= now) rateBuckets.delete(storedKey);
    }
  }
  if (!rateBuckets.has(key) && rateBuckets.size >= MAX_RATE_BUCKETS) return false;
  const current = rateBuckets.get(key);
  if (!current || current.resetAt <= now) {
    rateBuckets.set(key, { count: 1, resetAt: now + windowMs });
    return true;
  }
  if (current.count >= limit) return false;
  current.count += 1;
  return true;
}

function appendCookie(res: Response, value: string) {
  res.append("Set-Cookie", value);
}

function authErrorRedirect(req: Request, publicOrigin: string, code: string): string {
  const origin = requestOrigin(req, publicOrigin);
  const url = new URL(origin || "http://localhost");
  url.pathname = "/";
  url.searchParams.set("auth_error", code);
  return `${url.pathname}${url.search}`;
}

async function requireSession(
  req: Request,
  res: Response,
  sessionService: DashboardSessionService,
): Promise<SessionAuth | null> {
  let session;
  try {
    session = await sessionService.getSession(req.headers.cookie);
  } catch (error) {
    console.error("[dashboard-session] falha ao ler sessão", error instanceof Error ? error.message : String(error));
    sendNoStoreJson(res, 503, { ok: false, authenticated: false, error: "session_store_unavailable" });
    return null;
  }
  if (!session) {
    sendNoStoreJson(res, 401, { ok: false, authenticated: false, error: "session_required" });
    return null;
  }
  const identity = await getDiscordUserIdentity(session.accessToken);
  if (!identity.ok || !identity.user) {
    if (identity.status === 401 || identity.status === 403) {
      await sessionService.destroySession(req.headers.cookie).catch(() => undefined);
      appendCookie(res, sessionService.clearSessionCookie(isSecureRequest(req)));
      sendNoStoreJson(res, 401, { ok: false, authenticated: false, error: "session_invalid" });
    } else {
      sendNoStoreJson(res, 502, { ok: false, authenticated: false, error: "discord_unavailable" });
    }
    return null;
  }
  return { accessToken: session.accessToken, user: identity.user };
}

async function requireDashboardAccess(
  req: Request,
  res: Response,
  sessionService: DashboardSessionService,
): Promise<GuildAuth | null> {
  const session = await requireSession(req, res, sessionService);
  if (!session) return null;
  const guildId = dashboardGuildId(req);
  const access = await verifyDashboardAccess(session.accessToken, guildId, session.user);
  if (!access.ok || !access.user) {
    sendNoStoreJson(res, access.status, { ok: false, error: access.reason || "access_denied", detail: access.detail ?? null });
    return null;
  }
  return { ...session, guildId, user: access.user };
}

export function registerDashboardRoutes({
  app,
  configService,
  sessionService,
  discordClientId,
  exchangeDiscordCode,
  publicOrigin,
  allowedOrigins,
}: RegisterDashboardRoutesOptions) {
  const loadFullDashboard = async (
    auth: GuildAuth,
    onTaskCompleted?: (step: DashboardLoadStep) => void,
  ) => {
    const track = async <T>(step: DashboardLoadStep, task: Promise<T>): Promise<T> => {
      const result = await task;
      onTaskCompleted?.(step);
      return result;
    };
    const [settings, summary, optionsResult, bot] = await Promise.all([
      track("settings", configService.getSettings(auth.guildId)),
      track("summary", configService.getSummary(auth.guildId)),
      track("options", listGuildChannelsAndRoles(auth.guildId).catch(() => ({ ok: false, channels: [], roles: [], error: "options_failed" }))),
      track("bot", getDiscordBotIdentity().catch(() => null)),
    ]);
    return {
      ok: true as const,
      guildId: auth.guildId,
      user: auth.user,
      bot,
      sections: settings.sections,
      values: settings.values,
      summary: summary.sections,
      options: {
        ok: optionsResult.ok,
        guildId: auth.guildId,
        channels: optionsResult.channels,
        roles: optionsResult.roles,
        error: optionsResult.error ?? null,
      },
    };
  };

  app.get(["/health", "/api/health"], (_req, res) => {
    sendNoStoreJson(res, 200, {
      ok: true,
      service: "osaka-dashboard",
      version: "2.0.0",
      runtime: "web",
      time: new Date().toISOString(),
    });
  });

  app.get("/api/auth/login", (req, res) => {
    if (!takeRateLimit(req, "auth-login", 20, 10 * 60 * 1000)) {
      sendNoStoreJson(res, 429, { ok: false, error: "rate_limited" });
      return;
    }
    const redirectUri = callbackUrl(req, publicOrigin);
    if (!discordClientId || !redirectUri) {
      sendNoStoreJson(res, 503, { ok: false, error: "oauth_not_configured" });
      return;
    }
    const issued = sessionService.issueOAuthState(req.query.return_to, isSecureRequest(req));
    appendCookie(res, issued.setCookie);
    const params = new URLSearchParams({
      client_id: discordClientId,
      redirect_uri: redirectUri,
      response_type: "code",
      scope: "identify guilds",
      state: issued.state,
      prompt: "consent",
    });
    res.setHeader("Cache-Control", "no-store");
    res.redirect(302, `https://discord.com/oauth2/authorize?${params.toString()}`);
  });

  app.get("/api/auth/callback", async (req, res) => {
    if (!takeRateLimit(req, "auth-callback", 30, 10 * 60 * 1000)) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, "rate_limited"));
      return;
    }
    const secure = isSecureRequest(req);
    appendCookie(res, sessionService.clearOAuthCookie(secure));
    const state = sessionService.validateOAuthState(req.query.state, req.headers.cookie);
    if (!state.ok) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, state.reason));
      return;
    }
    const oauthError = firstString(req.query.error);
    if (oauthError) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, oauthError));
      return;
    }
    const code = firstString(req.query.code);
    const redirectUri = callbackUrl(req, publicOrigin);
    const exchanged = await exchangeDiscordCode(code, redirectUri);
    if (!exchanged.ok || !exchanged.accessToken) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, exchanged.error || "oauth_exchange_failed"));
      return;
    }
    try {
      const created = await sessionService.createSession(exchanged, secure);
      appendCookie(res, created.setCookie);
      res.setHeader("Cache-Control", "no-store");
      res.redirect(302, state.returnTo);
    } catch (error) {
      console.error("[dashboard-session] falha ao criar sessão", error instanceof Error ? error.message : String(error));
      res.redirect(302, authErrorRedirect(req, publicOrigin, "session_create_failed"));
    }
  });

  app.get("/api/public/identity", async (_req, res) => {
    const [bot, supportServer] = await Promise.all([
      getDiscordBotIdentity().catch(() => null),
      getDiscordSupportServerIdentity().catch(() => null),
    ]);
    res.setHeader("Cache-Control", "public, max-age=300, stale-while-revalidate=900");
    res.type("application/json");
    res.status(200).json({ ok: true, bot, supportServer });
  });

  app.get("/api/auth/session", async (req, res) => {
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    sendNoStoreJson(res, 200, { ok: true, authenticated: true, user: session.user });
  });

  app.post("/api/auth/logout", async (req, res) => {
    if (!mutationOriginAllowed(req, publicOrigin, allowedOrigins)) {
      sendNoStoreJson(res, 403, { ok: false, error: "origin_denied" });
      return;
    }
    await sessionService.destroySession(req.headers.cookie).catch(() => undefined);
    appendCookie(res, sessionService.clearSessionCookie(isSecureRequest(req)));
    sendNoStoreJson(res, 200, { ok: true });
  });

  app.get("/api/dashboard/servers", async (req, res) => {
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    const result = await listDashboardServers(session.accessToken, session.user);
    sendNoStoreJson(res, result.status, result.ok
      ? { ok: true, user: result.user, manageable: result.manageable, needsInvite: result.needsInvite }
      : { ok: false, user: result.user, manageable: [], needsInvite: [], error: result.error || "servers_failed" });
  });

  app.get("/api/dashboard/guild/:guildId/invite", async (req, res) => {
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    const guildId = dashboardGuildId(req);
    const access = await verifyDashboardInviteAccess(session.accessToken, guildId);
    if (!access.ok) {
      sendNoStoreJson(res, access.status, { ok: false, error: access.reason });
      return;
    }
    const inviteUrl = createDashboardInviteUrl(guildId);
    if (!inviteUrl) {
      sendNoStoreJson(res, 500, { ok: false, error: "invite_not_configured" });
      return;
    }
    sendNoStoreJson(res, 200, { ok: true, guild_id: guildId, invite_url: inviteUrl });
  });

  app.get("/api/dashboard/bootstrap", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    const bot = await getDiscordBotIdentity().catch(() => null);
    sendNoStoreJson(res, 200, {
      ok: true,
      user: auth.user,
      bot,
      guild_id: auth.guildId,
      sections: configService.listSections().map(({ id, label, emoji, description }) => ({ id, label, emoji, description })),
    });
  });

  app.get("/api/dashboard/guild/:guildId/full", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      sendNoStoreJson(res, 200, await loadFullDashboard(auth));
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "dashboard_load_failed" });
    }
  });

  app.get("/api/dashboard/guild/:guildId/full-progress", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;

    res.status(200);
    res.setHeader("Cache-Control", "no-store, max-age=0, no-transform");
    res.setHeader("Content-Type", "application/x-ndjson; charset=utf-8");
    res.setHeader("X-Accel-Buffering", "no");
    res.flushHeaders();

    const total = 5;
    let completed = 1;
    const writeEvent = (event: unknown) => {
      if (res.destroyed || res.writableEnded) return;
      res.write(`${JSON.stringify(event)}\n`);
    };

    writeEvent({ type: "progress", completed, total, step: "access" });
    try {
      const payload = await loadFullDashboard(auth, (step) => {
        completed += 1;
        writeEvent({ type: "progress", completed, total, step });
      });
      writeEvent({ type: "result", payload });
    } catch (error) {
      writeEvent({
        type: "error",
        status: 500,
        error: error instanceof Error ? error.message : "dashboard_load_failed",
      });
    } finally {
      if (!res.destroyed && !res.writableEnded) res.end();
    }
  });

  app.get("/api/dashboard/guild/:guildId/summary", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      sendNoStoreJson(res, 200, { ok: true, ...await configService.getSummary(auth.guildId) });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "summary_failed" });
    }
  });

  app.get("/api/dashboard/guild/:guildId/settings", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      sendNoStoreJson(res, 200, { ok: true, ...await configService.getSettings(auth.guildId) });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "settings_failed" });
    }
  });

  app.get("/api/dashboard/guild/:guildId/options", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    const result = await listGuildChannelsAndRoles(auth.guildId);
    sendNoStoreJson(res, result.ok ? 200 : 502, {
      ok: result.ok,
      guildId: auth.guildId,
      channels: result.channels,
      roles: result.roles,
      error: result.error ?? null,
    });
  });

  app.patch("/api/dashboard/guild/:guildId/settings", async (req, res) => {
    if (!mutationOriginAllowed(req, publicOrigin, allowedOrigins)) {
      sendNoStoreJson(res, 403, { ok: false, error: "origin_denied" });
      return;
    }
    if (!takeRateLimit(req, "settings-save", 120, 10 * 60 * 1000)) {
      sendNoStoreJson(res, 429, { ok: false, error: "rate_limited" });
      return;
    }
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      const updates = req.body && typeof req.body.updates === "object" && !Array.isArray(req.body.updates)
        ? req.body.updates as Record<string, unknown>
        : {};
      const result = await configService.updateSettings(auth.guildId, updates);
      try {
        const summary = await configService.getSummary(auth.guildId);
        sendNoStoreJson(res, 200, { ...result, summary: summary.sections });
      } catch {
        sendNoStoreJson(res, 200, { ...result, summary: null, summary_error: "summary_refresh_failed" });
      }
    } catch (error) {
      if (error instanceof DashboardConfigValidationError) {
        sendNoStoreJson(res, 400, { ok: false, error: error.code, message: error.message, section: error.sectionId, issues: error.issues });
        return;
      }
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "save_failed" });
    }
  });

  app.all(["/token", "/api/token", "/session", "/api/session"], (_req, res) => {
    sendNoStoreJson(res, 410, { ok: false, error: "legacy_api_removed" });
  });

  app.use("/api", (req, res) => {
    sendNoStoreJson(res, 404, { ok: false, error: "api_route_not_found", detail: `${req.method} ${req.path}` });
  });
}
