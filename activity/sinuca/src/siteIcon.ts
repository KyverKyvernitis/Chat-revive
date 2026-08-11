type FaviconFallback = {
  href: string;
  type: string | null;
};

const faviconFallbacks = new WeakMap<HTMLLinkElement, FaviconFallback>();

export function normalizeSiteIconUrl(value: string | null | undefined, baseHref: string): string | null {
  const raw = value?.trim();
  if (!raw) return null;

  try {
    const base = new URL(baseHref);
    const candidate = new URL(raw, base);
    const isSecure = candidate.protocol === "https:";
    const isSameOriginHttp = candidate.protocol === "http:" && candidate.origin === base.origin;
    return isSecure || isSameOriginHttp ? candidate.href : null;
  } catch {
    return null;
  }
}

export function syncSiteIcon(avatarUrl: string | null | undefined, target: Document = document): boolean {
  const icon = target.querySelector<HTMLLinkElement>("#osk-site-icon");
  if (!icon) return false;

  if (!faviconFallbacks.has(icon)) {
    faviconFallbacks.set(icon, {
      href: icon.getAttribute("href") || "",
      type: icon.getAttribute("type"),
    });
  }

  const nextUrl = normalizeSiteIconUrl(avatarUrl, target.baseURI);
  if (nextUrl) {
    icon.setAttribute("href", nextUrl);
    icon.removeAttribute("type");
    return true;
  }

  const fallback = faviconFallbacks.get(icon);
  if (!fallback) return false;
  icon.setAttribute("href", fallback.href);
  if (fallback.type) icon.setAttribute("type", fallback.type);
  else icon.removeAttribute("type");
  return false;
}
