import assert from "node:assert/strict";
import test from "node:test";
import { fetchDashboardFull } from "../src/transport/dashboardApi";
import { DashboardHttpError } from "../src/transport/httpClient";
import type { DashboardFullPayload } from "../src/types/dashboard";

const payload: DashboardFullPayload = {
  ok: true,
  guildId: "123456789012345",
  sections: [],
  values: {},
  summary: [],
  options: { ok: true, guildId: "123456789012345", channels: [], roles: [] },
};

function withBrowserTimers() {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", { configurable: true, value: globalThis });
  return () => {
    if (descriptor) Object.defineProperty(globalThis, "window", descriptor);
    else Reflect.deleteProperty(globalThis, "window");
  };
}

test("processa eventos NDJSON fragmentados e informa progresso real", async () => {
  const restoreWindow = withBrowserTimers();
  const originalFetch = globalThis.fetch;
  const encoder = new TextEncoder();
  const progress: number[] = [];
  try {
    globalThis.fetch = (async () => new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('{"type":"progress","completed":1,'));
        controller.enqueue(encoder.encode('"total":5,"step":"access"}\n{"type":"progress","completed":3,"total":5,"step":"summary"}\n'));
        controller.enqueue(encoder.encode(`${JSON.stringify({ type: "result", payload })}\n`));
        controller.close();
      },
    }), { headers: { "Content-Type": "application/x-ndjson; charset=utf-8" } })) as typeof fetch;

    const result = await fetchDashboardFull(payload.guildId, undefined, (value) => progress.push(value));
    assert.deepEqual(result, payload);
    assert.deepEqual(progress, [20, 60, 100]);
  } finally {
    globalThis.fetch = originalFetch;
    restoreWindow();
  }
});

test("usa o endpoint JSON antigo apenas quando o progressivo não existe", async () => {
  const restoreWindow = withBrowserTimers();
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  const progress: number[] = [];
  try {
    globalThis.fetch = (async (input) => {
      requests.push(String(input));
      if (requests.length === 1) {
        return new Response(JSON.stringify({ error: "not_found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;

    const result = await fetchDashboardFull(payload.guildId, undefined, (value) => progress.push(value));
    assert.deepEqual(result, payload);
    assert.deepEqual(progress, [100]);
    assert.deepEqual(requests, [
      `/api/dashboard/guild/${payload.guildId}/full-progress`,
      `/api/dashboard/guild/${payload.guildId}/full`,
    ]);
  } finally {
    globalThis.fetch = originalFetch;
    restoreWindow();
  }
});

test("propaga falhas do fluxo sem repetir uma carga pesada", async () => {
  const restoreWindow = withBrowserTimers();
  const originalFetch = globalThis.fetch;
  let requests = 0;
  try {
    globalThis.fetch = (async () => {
      requests += 1;
      return new Response('{"type":"error","status":500,"error":"settings_failed"}\n', {
        headers: { "Content-Type": "application/x-ndjson" },
      });
    }) as typeof fetch;

    await assert.rejects(
      () => fetchDashboardFull(payload.guildId),
      (error: unknown) => error instanceof DashboardHttpError && error.status === 500 && error.message === "settings_failed",
    );
    assert.equal(requests, 1);
  } finally {
    globalThis.fetch = originalFetch;
    restoreWindow();
  }
});
