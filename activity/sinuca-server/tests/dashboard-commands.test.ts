import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { buildDashboardCommands, resetDashboardCommandsCachesForTests } from "../src/services/dashboardCommandsService.js";

const prefixes = {
  bot_prefix: "_",
  atts_prefix: "%",
  teto_prefix: "'",
  gtts_prefix: ".",
  edge_prefix: ",",
};

function withWorkerRegistry(workers: Record<string, unknown>, run: () => void): void {
  const directory = mkdtempSync(join(tmpdir(), "osaka-command-test-"));
  const registry = join(directory, "workers.json");
  const previous = process.env.CORE_WORKERS_REGISTRY_PATH;
  writeFileSync(registry, JSON.stringify({ workers }));
  process.env.CORE_WORKERS_REGISTRY_PATH = registry;
  resetDashboardCommandsCachesForTests();
  try {
    run();
  } finally {
    if (previous === undefined) delete process.env.CORE_WORKERS_REGISTRY_PATH;
    else process.env.CORE_WORKERS_REGISTRY_PATH = previous;
    resetDashboardCommandsCachesForTests();
    rmSync(directory, { recursive: true, force: true });
  }
}

test("publica apenas comandos de usuário comum e aplica o modo por comandos", () => {
  withWorkerRegistry({}, () => {
    const payload = buildDashboardCommands({ prefixes, gamesMode: "commands" });
    const byKey = new Map(payload.commands.map((command) => [command.key, command]));

    assert.equal(payload.musicAvailable, false);
    assert.equal(byKey.get("clear")?.usage, "_clear");
    assert.equal(byKey.get("ficha")?.usage, "_ficha");
    assert.deepEqual(byKey.get("ficha")?.aliases, ["_fichas"]);
    assert.ok(byKey.has("help") && byKey.has("ping"));
    assert.ok(!byKey.has("economia"));
    assert.ok(!byKey.has("birthday"));
    assert.ok(!byKey.has("play"));
    assert.ok(payload.categories.some((category) => category.key === "utilities"));
    assert.ok(!payload.categories.some((category) => category.key === "server"));
  });
});

test("mantém gatilhos sem prefixo e só mostra música com worker compatível online", () => {
  withWorkerRegistry({
    music: {
      enabled: true,
      last_heartbeat_at: Date.now() / 1000,
      roles: ["phone-worker"],
      capabilities: ["music"],
    },
  }, () => {
    const payload = buildDashboardCommands({ prefixes: { ...prefixes, bot_prefix: "!" }, gamesMode: "triggers" });
    const byKey = new Map(payload.commands.map((command) => [command.key, command]));

    assert.equal(payload.musicAvailable, true);
    assert.equal(byKey.get("ficha")?.usage, "ficha");
    assert.equal(byKey.get("play")?.usage, "!play <nome ou link>");
    assert.deepEqual(byKey.get("play")?.aliases, ["!tocar", "!music", "!musica"]);
  });
});
