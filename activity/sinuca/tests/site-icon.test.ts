import assert from "node:assert/strict";
import test from "node:test";
import { normalizeSiteIconUrl, syncSiteIcon } from "../src/siteIcon";

test("aceita o avatar HTTPS do bot e URLs locais", () => {
  assert.equal(
    normalizeSiteIconUrl(
      "https://cdn.discordapp.com/avatars/123/avatar.png?size=128",
      "https://osaka.example/dashboard",
    ),
    "https://cdn.discordapp.com/avatars/123/avatar.png?size=128",
  );
  assert.equal(
    normalizeSiteIconUrl("/favicon.png", "http://localhost:5173/dashboard"),
    "http://localhost:5173/favicon.png",
  );
});

test("recusa esquemas inseguros e HTTP externo", () => {
  assert.equal(normalizeSiteIconUrl("javascript:alert(1)", "https://osaka.example"), null);
  assert.equal(normalizeSiteIconUrl("data:image/png;base64,abc", "https://osaka.example"), null);
  assert.equal(normalizeSiteIconUrl("http://example.net/avatar.png", "https://osaka.example"), null);
});

test("troca o favicon e restaura o fallback quando o avatar não existe", () => {
  const attributes = new Map<string, string>([
    ["href", "data:image/svg+xml,fallback"],
    ["type", "image/svg+xml"],
  ]);
  const icon = {
    getAttribute: (name: string) => attributes.get(name) ?? null,
    setAttribute: (name: string, value: string) => attributes.set(name, value),
    removeAttribute: (name: string) => attributes.delete(name),
  } as unknown as HTMLLinkElement;
  const target = {
    baseURI: "https://osaka.example/",
    querySelector: () => icon,
  } as unknown as Document;

  assert.equal(syncSiteIcon("https://cdn.discordapp.com/avatars/123/avatar.png", target), true);
  assert.equal(attributes.get("href"), "https://cdn.discordapp.com/avatars/123/avatar.png");
  assert.equal(attributes.has("type"), false);

  assert.equal(syncSiteIcon(null, target), false);
  assert.equal(attributes.get("href"), "data:image/svg+xml,fallback");
  assert.equal(attributes.get("type"), "image/svg+xml");
});
