import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(projectRoot, "src/components/VisualTemplates.tsx"), "utf8");

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
