import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from "crypto";
import { MongoClient, type Collection, type Document } from "mongodb";

export interface DashboardOAuthTokenResult {
  ok: boolean;
  accessToken: string | null;
  refreshToken?: string | null;
  expiresIn?: number | null;
  error: string | null;
  detail: string | null;
}

export interface DashboardSession {
  id: string;
  accessToken: string;
  refreshToken: string | null;
  accessExpiresAt: number | null;
  expiresAt: number;
}

interface StoredSession extends Document {
  type: "dashboard_session";
  session_hash: string;
  access_token: string;
  refresh_token: string | null;
  access_expires_at: Date | null;
  expires_at: Date;
  created_at: Date;
  updated_at: Date;
}

type RefreshedSessionTokens = Pick<DashboardSession, "accessToken" | "refreshToken" | "accessExpiresAt">;

interface CreateDashboardSessionServiceOptions {
  mongoUri: string;
  mongoDbName: string;
  mongoCollectionName?: string;
  secret: string;
  refreshDiscordToken(refreshToken: string): Promise<DashboardOAuthTokenResult>;
  sessionLifetimeMs?: number;
}

const SESSION_COOKIE = "osk_dashboard_session";
const OAUTH_COOKIE = "osk_dashboard_oauth";
const DEFAULT_SESSION_LIFETIME_MS = 30 * 24 * 60 * 60 * 1000;
const REFRESH_MARGIN_MS = 5 * 60 * 1000;

function base64Url(input: Buffer | string): string {
  return Buffer.from(input).toString("base64url");
}

function safeReturnPath(value: unknown): string {
  const raw = typeof value === "string" ? value.trim() : "";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return "/dashboard";
  try {
    const parsed = new URL(raw, "https://dashboard.invalid");
    if (parsed.origin !== "https://dashboard.invalid") return "/dashboard";
    const dashboardPath = parsed.pathname === "/dashboard" || parsed.pathname.startsWith("/dashboard/");
    if (!dashboardPath && parsed.pathname !== "/") return "/dashboard";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/dashboard";
  }
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

function cookieValue(rawHeader: string | undefined, name: string): string {
  if (!rawHeader) return "";
  for (const part of rawHeader.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return "";
}

function serializeCookie(
  name: string,
  value: string,
  options: { maxAgeSeconds?: number; secure?: boolean; path?: string; sameSite?: "Lax" | "Strict"; httpOnly?: boolean } = {},
): string {
  const parts = [`${name}=${encodeURIComponent(value)}`, `Path=${options.path ?? "/"}`];
  if (typeof options.maxAgeSeconds === "number") parts.push(`Max-Age=${Math.max(0, Math.trunc(options.maxAgeSeconds))}`);
  if (options.httpOnly !== false) parts.push("HttpOnly");
  parts.push(`SameSite=${options.sameSite ?? "Lax"}`);
  if (options.secure) parts.push("Secure");
  return parts.join("; ");
}

function sessionHash(sessionId: string): string {
  return createHash("sha256").update(sessionId).digest("hex");
}

function deriveKey(secret: string): Buffer {
  return createHash("sha256").update(secret).digest();
}

function encryptText(value: string, key: Buffer): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return `${base64Url(iv)}.${base64Url(tag)}.${base64Url(encrypted)}`;
}

function decryptText(value: string, key: Buffer): string {
  const [ivRaw, tagRaw, encryptedRaw] = value.split(".");
  if (!ivRaw || !tagRaw || !encryptedRaw) throw new Error("invalid_encrypted_session_value");
  const decipher = createDecipheriv("aes-256-gcm", key, Buffer.from(ivRaw, "base64url"));
  decipher.setAuthTag(Buffer.from(tagRaw, "base64url"));
  return Buffer.concat([
    decipher.update(Buffer.from(encryptedRaw, "base64url")),
    decipher.final(),
  ]).toString("utf8");
}

export interface DashboardSessionService {
  readonly sessionCookieName: string;
  readonly oauthCookieName: string;
  issueOAuthState(returnTo: unknown, secure: boolean): { state: string; setCookie: string };
  validateOAuthState(state: unknown, cookieHeader: string | undefined): { ok: true; returnTo: string } | { ok: false; reason: string };
  clearOAuthCookie(secure: boolean): string;
  createSession(tokens: DashboardOAuthTokenResult, secure: boolean): Promise<{ session: DashboardSession; setCookie: string }>;
  getSession(cookieHeader: string | undefined): Promise<DashboardSession | null>;
  destroySession(cookieHeader: string | undefined): Promise<void>;
  clearSessionCookie(secure: boolean): string;
}

export function createDashboardSessionService(options: CreateDashboardSessionServiceOptions): DashboardSessionService {
  if (!options.secret || options.secret.length < 24) {
    throw new Error("dashboard_session_secret_too_short");
  }

  const encryptionKey = deriveKey(options.secret);
  const lifetimeMs = options.sessionLifetimeMs ?? DEFAULT_SESSION_LIFETIME_MS;
  let client: MongoClient | null = null;
  let collection: Collection<StoredSession> | null = null;
  let indexesReady = false;
  const refreshFlights = new Map<string, Promise<RefreshedSessionTokens | null>>();

  async function getCollection(): Promise<Collection<StoredSession>> {
    if (!options.mongoUri) throw new Error("mongodb_not_configured");
    if (!collection) {
      client = new MongoClient(options.mongoUri);
      await client.connect();
      collection = client
        .db(options.mongoDbName)
        .collection<StoredSession>(options.mongoCollectionName || "dashboard_sessions");
    }
    if (!indexesReady) {
      await Promise.all([
        collection.createIndex({ session_hash: 1 }, { unique: true, name: "dashboard_session_hash" }),
        collection.createIndex({ expires_at: 1 }, { expireAfterSeconds: 0, name: "dashboard_session_ttl" }),
      ]);
      indexesReady = true;
    }
    return collection;
  }

  async function refreshSessionTokens(
    coll: Collection<StoredSession>,
    doc: StoredSession,
    refreshToken: string,
  ): Promise<RefreshedSessionTokens | null> {
    const refreshed = await options.refreshDiscordToken(refreshToken);
    if (!refreshed.ok || !refreshed.accessToken) {
      await coll.deleteOne({ _id: doc._id });
      return null;
    }

    const nextRefreshToken = refreshed.refreshToken ?? refreshToken;
    const nextAccessExpiresAt = typeof refreshed.expiresIn === "number" && refreshed.expiresIn > 0
      ? Date.now() + refreshed.expiresIn * 1000
      : null;
    const update = await coll.updateOne(
      { _id: doc._id },
      {
        $set: {
          access_token: encryptText(refreshed.accessToken, encryptionKey),
          refresh_token: nextRefreshToken ? encryptText(nextRefreshToken, encryptionKey) : null,
          access_expires_at: nextAccessExpiresAt ? new Date(nextAccessExpiresAt) : null,
          updated_at: new Date(),
        },
      },
    );
    if (update.matchedCount !== 1) return null;
    return {
      accessToken: refreshed.accessToken,
      refreshToken: nextRefreshToken,
      accessExpiresAt: nextAccessExpiresAt,
    };
  }

  function signPayload(payload: string): string {
    return base64Url(createHmac("sha256", options.secret).update(payload).digest());
  }

  function issueOAuthState(returnTo: unknown, secure: boolean) {
    const payload = base64Url(JSON.stringify({
      nonce: base64Url(randomBytes(24)),
      returnTo: safeReturnPath(returnTo),
      exp: Date.now() + 10 * 60 * 1000,
    }));
    const state = `${payload}.${signPayload(payload)}`;
    return {
      state,
      setCookie: serializeCookie(OAUTH_COOKIE, state, {
        maxAgeSeconds: 10 * 60,
        secure,
        sameSite: "Lax",
      }),
    };
  }

  function validateOAuthState(state: unknown, cookieHeader: string | undefined) {
    const incoming = typeof state === "string" ? state.trim() : "";
    const stored = cookieValue(cookieHeader, OAUTH_COOKIE);
    if (!incoming || !stored || !constantTimeEqual(incoming, stored)) {
      return { ok: false as const, reason: "oauth_state_mismatch" };
    }
    const [payload, signature] = incoming.split(".");
    if (!payload || !signature || !constantTimeEqual(signature, signPayload(payload))) {
      return { ok: false as const, reason: "oauth_state_invalid" };
    }
    try {
      const parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as { exp?: number; returnTo?: string };
      if (!Number.isFinite(parsed.exp) || Number(parsed.exp) < Date.now()) {
        return { ok: false as const, reason: "oauth_state_expired" };
      }
      return { ok: true as const, returnTo: safeReturnPath(parsed.returnTo) };
    } catch {
      return { ok: false as const, reason: "oauth_state_invalid" };
    }
  }

  function clearOAuthCookie(secure: boolean) {
    return serializeCookie(OAUTH_COOKIE, "", { maxAgeSeconds: 0, secure, sameSite: "Lax" });
  }

  async function createSession(tokens: DashboardOAuthTokenResult, secure: boolean) {
    if (!tokens.ok || !tokens.accessToken) throw new Error(tokens.error || "missing_access_token");
    const rawSessionId = base64Url(randomBytes(32));
    const now = Date.now();
    const expiresAt = now + lifetimeMs;
    const accessExpiresAt = typeof tokens.expiresIn === "number" && tokens.expiresIn > 0
      ? now + tokens.expiresIn * 1000
      : null;
    const coll = await getCollection();
    await coll.insertOne({
      type: "dashboard_session",
      session_hash: sessionHash(rawSessionId),
      access_token: encryptText(tokens.accessToken, encryptionKey),
      refresh_token: tokens.refreshToken ? encryptText(tokens.refreshToken, encryptionKey) : null,
      access_expires_at: accessExpiresAt ? new Date(accessExpiresAt) : null,
      expires_at: new Date(expiresAt),
      created_at: new Date(now),
      updated_at: new Date(now),
    });
    return {
      session: {
        id: rawSessionId,
        accessToken: tokens.accessToken,
        refreshToken: tokens.refreshToken ?? null,
        accessExpiresAt,
        expiresAt,
      },
      setCookie: serializeCookie(SESSION_COOKIE, rawSessionId, {
        maxAgeSeconds: Math.floor(lifetimeMs / 1000),
        secure,
        sameSite: "Lax",
      }),
    };
  }

  async function getSession(cookieHeader: string | undefined): Promise<DashboardSession | null> {
    const rawSessionId = cookieValue(cookieHeader, SESSION_COOKIE);
    if (!rawSessionId) return null;
    const coll = await getCollection();
    const hashedSessionId = sessionHash(rawSessionId);
    const doc = await coll.findOne({ type: "dashboard_session", session_hash: hashedSessionId });
    if (!doc || doc.expires_at.getTime() <= Date.now()) {
      if (doc) await coll.deleteOne({ _id: doc._id });
      return null;
    }

    let accessToken: string;
    let refreshToken: string | null;
    try {
      accessToken = decryptText(doc.access_token, encryptionKey);
      refreshToken = doc.refresh_token ? decryptText(doc.refresh_token, encryptionKey) : null;
    } catch {
      await coll.deleteOne({ _id: doc._id });
      return null;
    }

    let accessExpiresAt = doc.access_expires_at?.getTime() ?? null;
    if (accessExpiresAt && accessExpiresAt <= Date.now() + REFRESH_MARGIN_MS) {
      if (!refreshToken) {
        await coll.deleteOne({ _id: doc._id });
        return null;
      }
      let flight = refreshFlights.get(hashedSessionId);
      if (!flight) {
        flight = refreshSessionTokens(coll, doc, refreshToken);
        refreshFlights.set(hashedSessionId, flight);
        const clearFlight = () => {
          if (refreshFlights.get(hashedSessionId) === flight) refreshFlights.delete(hashedSessionId);
        };
        void flight.then(clearFlight, clearFlight);
      }
      const refreshed = await flight;
      if (!refreshed) return null;
      accessToken = refreshed.accessToken;
      refreshToken = refreshed.refreshToken;
      accessExpiresAt = refreshed.accessExpiresAt;
    }

    return {
      id: rawSessionId,
      accessToken,
      refreshToken,
      accessExpiresAt,
      expiresAt: doc.expires_at.getTime(),
    };
  }

  async function destroySession(cookieHeader: string | undefined) {
    const rawSessionId = cookieValue(cookieHeader, SESSION_COOKIE);
    if (!rawSessionId) return;
    const coll = await getCollection();
    await coll.deleteOne({ type: "dashboard_session", session_hash: sessionHash(rawSessionId) });
  }

  function clearSessionCookie(secure: boolean) {
    return serializeCookie(SESSION_COOKIE, "", { maxAgeSeconds: 0, secure, sameSite: "Lax" });
  }

  return {
    sessionCookieName: SESSION_COOKIE,
    oauthCookieName: OAUTH_COOKIE,
    issueOAuthState,
    validateOAuthState,
    clearOAuthCookie,
    createSession,
    getSession,
    destroySession,
    clearSessionCookie,
  };
}
