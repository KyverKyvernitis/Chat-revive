import express from "express";
import { createServer } from "http";
import { registerDashboardRoutes } from "./routes/registerDashboardRoutes.js";
import { createDashboardConfigService } from "./services/dashboardConfigService.js";
import { createDashboardSessionService } from "./services/dashboardSessionService.js";

const app = express();
const configuredProxyHops = Number.parseInt(String(process.env.DASHBOARD_TRUST_PROXY_HOPS || "1"), 10);
app.set("trust proxy", Number.isFinite(configuredProxyHops) ? Math.max(0, Math.min(10, configuredProxyHops)) : 1);

const discordClientId = process.env.DISCORD_CLIENT_ID || process.env.VITE_DISCORD_CLIENT_ID || "";
const discordClientSecret = process.env.DISCORD_CLIENT_SECRET || process.env.CLIENT_SECRET || "";
const mongoUri = process.env.MONGODB_URI || process.env.MONGO_URI || "";
const mongoDbName = process.env.MONGODB_DB || process.env.MONGO_DB_NAME || process.env.MONGODB_DB_NAME || "chat_revive";
const mongoCollectionName = process.env.MONGODB_COLLECTION || process.env.MONGO_COLLECTION_NAME || process.env.MONGODB_COLLECTION_NAME || "settings";
const sessionSecret = process.env.DASHBOARD_SESSION_SECRET || discordClientSecret || (process.env.NODE_ENV === "production" ? "" : "dashboard-local-development-secret-change-me");

function normalizedOrigin(value: string): string {
  try {
    return new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`).origin;
  } catch {
    return "";
  }
}

const publicOrigin = normalizedOrigin(process.env.DASHBOARD_PUBLIC_URL || process.env.PUBLIC_HOST || "");
if (process.env.NODE_ENV === "production" && !publicOrigin) {
  console.warn("[dashboard-config] DASHBOARD_PUBLIC_URL ausente; OAuth ficará indisponível por segurança");
}
const developmentOrigins = process.env.NODE_ENV === "production"
  ? []
  : ["http://localhost:4173", "http://127.0.0.1:4173"];
const allowedOrigins = new Set([
  publicOrigin,
  ...developmentOrigins,
  ...String(process.env.DASHBOARD_ALLOWED_ORIGINS || "").split(",").map((item) => normalizedOrigin(item.trim())),
].filter(Boolean));

app.use((req, res, next) => {
  const origin = typeof req.headers.origin === "string" ? normalizedOrigin(req.headers.origin) : "";
  if (origin && allowedOrigins.has(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
    res.setHeader("Access-Control-Allow-Credentials", "true");
  }
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,PATCH,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Requested-With");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
  res.setHeader("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()");
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin-allow-popups");
  res.setHeader("Cross-Origin-Resource-Policy", "same-site");
  if (req.secure || req.headers["x-forwarded-proto"] === "https") {
    res.setHeader("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
  }

  if (req.method === "OPTIONS") {
    if (origin && !allowedOrigins.has(origin)) {
      res.status(403).end();
      return;
    }
    res.status(204).end();
    return;
  }
  next();
});

app.use(express.json({ limit: "512kb" }));
app.use(express.urlencoded({ extended: false, limit: "128kb" }));
app.use((req, _res, next) => {
  const path = req.path || "/";
  console.log("[dashboard-http]", JSON.stringify({
    method: req.method,
    path,
    origin: req.headers.origin ?? null,
    ip: req.ip,
  }));
  next();
});

type OAuthTokenResult = {
  ok: boolean;
  accessToken: string | null;
  refreshToken: string | null;
  expiresIn: number | null;
  error: string | null;
  detail: string | null;
};

async function requestDiscordOAuthToken(params: URLSearchParams, logType: string): Promise<OAuthTokenResult> {
  if (!discordClientId || !discordClientSecret) {
    return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "oauth_not_configured", detail: null };
  }

  try {
    params.set("client_id", discordClientId);
    params.set("client_secret", discordClientSecret);
    const response = await fetch("https://discord.com/api/v10/oauth2/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: params,
    });
    const raw = await response.text();
    let data: { access_token?: string; refresh_token?: string; expires_in?: number; error?: string; error_description?: string } = {};
    try {
      data = JSON.parse(raw) as typeof data;
    } catch {
      console.error("[dashboard-oauth] resposta inválida", JSON.stringify({ type: logType, status: response.status }));
      return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "token_invalid_json", detail: null };
    }

    if (!response.ok || !data.access_token) {
      console.error("[dashboard-oauth] troca recusada", JSON.stringify({
        type: logType,
        status: response.status,
        error: data.error ?? "token_exchange_failed",
      }));
      return {
        ok: false,
        accessToken: null,
        refreshToken: null,
        expiresIn: null,
        error: data.error ?? "token_exchange_failed",
        detail: data.error_description ?? null,
      };
    }

    const expiresIn = typeof data.expires_in === "number" && Number.isFinite(data.expires_in) ? data.expires_in : null;
    console.log("[dashboard-oauth] token atualizado", JSON.stringify({ type: logType, status: response.status, expiresIn }));
    return {
      ok: true,
      accessToken: data.access_token,
      refreshToken: typeof data.refresh_token === "string" && data.refresh_token.trim() ? data.refresh_token : null,
      expiresIn,
      error: null,
      detail: null,
    };
  } catch (error) {
    console.error("[dashboard-oauth] erro na troca", error instanceof Error ? error.message : String(error));
    return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "token_exchange_exception", detail: null };
  }
}

async function exchangeDiscordCode(code: string, redirectUri: string): Promise<OAuthTokenResult> {
  if (!code) return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "missing_code", detail: null };
  const params = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
  });
  return await requestDiscordOAuthToken(params, "authorization_code");
}

async function refreshDiscordToken(refreshToken: string): Promise<OAuthTokenResult> {
  if (!refreshToken) return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "missing_refresh_token", detail: null };
  const params = new URLSearchParams({ grant_type: "refresh_token", refresh_token: refreshToken });
  return await requestDiscordOAuthToken(params, "refresh_token");
}

const dashboardConfigService = createDashboardConfigService({
  mongoUri,
  mongoDbName,
  mongoCollectionName,
});

const dashboardSessionService = createDashboardSessionService({
  mongoUri,
  mongoDbName,
  mongoCollectionName: process.env.DASHBOARD_SESSION_COLLECTION || "dashboard_sessions",
  secret: sessionSecret,
  refreshDiscordToken,
});

registerDashboardRoutes({
  app,
  configService: dashboardConfigService,
  sessionService: dashboardSessionService,
  discordClientId,
  exchangeDiscordCode,
  publicOrigin,
  allowedOrigins,
});

const server = createServer(app);
const port = Number(process.env.PORT || 8787);
server.listen(port, () => {
  console.log(`[dashboard-server] ouvindo na porta ${port}`);
});
