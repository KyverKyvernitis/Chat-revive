import assert from "node:assert/strict";
import test from "node:test";
import {
  fetchDiscordAttachmentPreview,
  parseDiscordAttachmentPreviewUrl,
} from "../src/services/dashboardMediaPreviewService.js";

const attachmentUrl = "https://cdn.discordapp.com/attachments/123456789012345/987654321098765/image.png?ex=ffffffff&is=eeeeeeee&hm=abc";

test("aceita somente URLs HTTPS de anexos do Discord", () => {
  assert.equal(parseDiscordAttachmentPreviewUrl(attachmentUrl)?.hostname, "cdn.discordapp.com");
  assert.equal(parseDiscordAttachmentPreviewUrl(attachmentUrl.replace("cdn.discordapp.com", "media.discordapp.net"))?.hostname, "media.discordapp.net");
  assert.equal(parseDiscordAttachmentPreviewUrl("https://example.com/attachments/123456789012345/987654321098765/image.png"), null);
  assert.equal(parseDiscordAttachmentPreviewUrl("https://cdn.discordapp.com.evil.test/attachments/123456789012345/987654321098765/image.png"), null);
  assert.equal(parseDiscordAttachmentPreviewUrl("https://cdn.discordapp.com:444/attachments/123456789012345/987654321098765/image.png"), null);
  assert.equal(parseDiscordAttachmentPreviewUrl("https://cdn.discordapp.com/avatars/123456789012345/image.png"), null);
});

test("entrega somente imagens raster dentro do limite", async () => {
  const result = await fetchDiscordAttachmentPreview(attachmentUrl, async () => new Response(
    new Uint8Array([137, 80, 78, 71]),
    { status: 200, headers: { "Content-Type": "image/png", "Content-Length": "4" } },
  ));
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.equal(result.contentType, "image/png");
    assert.deepEqual(Array.from(result.body), [137, 80, 78, 71]);
  }

  const svg = await fetchDiscordAttachmentPreview(attachmentUrl, async () => new Response(
    "<svg/>",
    { status: 200, headers: { "Content-Type": "image/svg+xml" } },
  ));
  assert.deepEqual(svg, { ok: false, status: 415, error: "unsupported_preview_image_type" });
});

test("recusa redirecionamento para fora do CDN permitido", async () => {
  const result = await fetchDiscordAttachmentPreview(attachmentUrl, async () => new Response(null, {
    status: 302,
    headers: { Location: "https://example.com/image.png" },
  }));
  assert.deepEqual(result, { ok: false, status: 502, error: "discord_attachment_redirect_denied" });
});
