const DISCORD_ATTACHMENT_HOSTS = new Set(["cdn.discordapp.com", "media.discordapp.net"]);
const DISCORD_ATTACHMENT_PATH = /^\/attachments\/\d{15,24}\/\d{15,24}\/[^/]+$/;
const SAFE_IMAGE_TYPES = new Set(["image/avif", "image/gif", "image/jpeg", "image/png", "image/webp"]);
const MAX_PREVIEW_BYTES = 12 * 1024 * 1024;
const MAX_REDIRECTS = 2;
const PREVIEW_TIMEOUT_MS = 8_000;

export type DashboardMediaPreviewResult = {
  ok: true;
  body: Uint8Array;
  contentType: string;
} | {
  ok: false;
  status: number;
  error: string;
};

/**
 * Aceita somente anexos raster servidos pelos dois hosts oficiais do CDN do
 * Discord. A validação estrita impede que a rota de prévia vire um proxy SSRF.
 */
export function parseDiscordAttachmentPreviewUrl(value: unknown): URL | null {
  if (typeof value !== "string") return null;
  const raw = value.trim();
  if (!raw || raw.length > 4_096) return null;
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== "https:") return null;
    if (parsed.username || parsed.password || parsed.port || parsed.hash) return null;
    if (!DISCORD_ATTACHMENT_HOSTS.has(parsed.hostname.toLowerCase())) return null;
    if (!DISCORD_ATTACHMENT_PATH.test(parsed.pathname)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function upstreamErrorStatus(status: number): number {
  if (status === 404 || status === 410) return 410;
  if (status === 401 || status === 403) return 422;
  if (status === 413) return 413;
  return 502;
}

export async function fetchDiscordAttachmentPreview(
  value: unknown,
  fetchImpl: typeof fetch = fetch,
): Promise<DashboardMediaPreviewResult> {
  let currentUrl = parseDiscordAttachmentPreviewUrl(value);
  if (!currentUrl) return { ok: false, status: 400, error: "invalid_discord_attachment_url" };

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), PREVIEW_TIMEOUT_MS);
  try {
    for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects += 1) {
      const response = await fetchImpl(currentUrl, {
        method: "GET",
        redirect: "manual",
        signal: controller.signal,
        headers: {
          Accept: "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.1",
          "User-Agent": "OsakaDashboard/2.0",
        },
      });

      if (response.status >= 300 && response.status < 400) {
        const location = response.headers.get("location");
        if (!location || redirects >= MAX_REDIRECTS) {
          return { ok: false, status: 502, error: "discord_attachment_redirect_failed" };
        }
        currentUrl = parseDiscordAttachmentPreviewUrl(new URL(location, currentUrl).toString());
        if (!currentUrl) return { ok: false, status: 502, error: "discord_attachment_redirect_denied" };
        continue;
      }

      if (!response.ok) {
        return {
          ok: false,
          status: upstreamErrorStatus(response.status),
          error: response.status === 404 || response.status === 410
            ? "discord_attachment_expired"
            : "discord_attachment_unavailable",
        };
      }

      const contentType = (response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
      if (!SAFE_IMAGE_TYPES.has(contentType)) {
        return { ok: false, status: 415, error: "unsupported_preview_image_type" };
      }
      const declaredLength = Number(response.headers.get("content-length") || 0);
      if (Number.isFinite(declaredLength) && declaredLength > MAX_PREVIEW_BYTES) {
        return { ok: false, status: 413, error: "preview_image_too_large" };
      }

      const body = new Uint8Array(await response.arrayBuffer());
      if (body.byteLength > MAX_PREVIEW_BYTES) {
        return { ok: false, status: 413, error: "preview_image_too_large" };
      }
      return { ok: true, body, contentType };
    }
    return { ok: false, status: 502, error: "discord_attachment_redirect_failed" };
  } catch (error) {
    return {
      ok: false,
      status: 502,
      error: error instanceof Error && error.name === "AbortError"
        ? "preview_image_timeout"
        : "preview_image_fetch_failed",
    };
  } finally {
    clearTimeout(timeout);
  }
}
