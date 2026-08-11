import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { resolveCompletedImageState } from "../src/components/VisualTemplates";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(projectRoot, "src/components/VisualTemplates.tsx"), "utf8");
const theme = readFileSync(join(projectRoot, "src/yin-yang-theme.css"), "utf8");

test("usa os assets enviados como padrão dos templates visuais", () => {
  assert.match(source, /BUNDLED_DECORATIVE_IMAGE_URL = "\/assets\/osaka-landing-character\.jpg"/);
  assert.match(source, /BUNDLED_LOADING_GIF_URL = "\/assets\/osaka-loading\.gif"/);
});

test("mantém imagens válidas no diretório público", () => {
  const decorativeImage = readFileSync(join(projectRoot, "public/assets/osaka-landing-character.jpg"));
  const loadingGif = readFileSync(join(projectRoot, "public/assets/osaka-loading.gif"));

  assert.deepEqual([...decorativeImage.subarray(0, 3)], [0xff, 0xd8, 0xff]);
  assert.equal(loadingGif.subarray(0, 6).toString("ascii"), "GIF89a");
  assert.ok(decorativeImage.byteLength > 50_000);
  assert.ok(loadingGif.byteLength > 3_000_000);
});

test("reconhece imediatamente imagens restauradas do cache", () => {
  assert.equal(resolveCompletedImageState(null), null);
  assert.equal(resolveCompletedImageState({ complete: false, naturalWidth: 0 }), null);
  assert.equal(resolveCompletedImageState({ complete: true, naturalWidth: 555 }), "ready");
  assert.equal(resolveCompletedImageState({ complete: true, naturalWidth: 0 }), "failed");
});

test("não mantém os assets invisíveis enquanto aguarda eventos do navegador", () => {
  assert.match(theme, /\.osk-decorative-template\s*\{[\s\S]*?opacity:\s*var\(--osk-decorative-opacity\)/);
  assert.match(theme, /\.osk-loading-visual img\s*\{[\s\S]*?opacity:\s*1/);
});
