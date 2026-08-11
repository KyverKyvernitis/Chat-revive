import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const source = resolve(scriptDirectory, "../../../shared/help_catalog.json");
const destinationDirectory = resolve(scriptDirectory, "../dist/data");

await mkdir(destinationDirectory, { recursive: true });
await copyFile(source, resolve(destinationDirectory, "help_catalog.json"));
