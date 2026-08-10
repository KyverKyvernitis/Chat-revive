import assert from "node:assert/strict";
import test from "node:test";
import { channelOptionsForField, stringifyDashboardValue } from "../src/components/DashboardFieldControl";
import type { DashboardChannelOption, DashboardFieldDefinition } from "../src/types/dashboard";

test("mantém zero como valor numérico válido", () => {
  assert.equal(stringifyDashboardValue(0), "0");
  assert.equal(stringifyDashboardValue(-1), "-1");
  assert.equal(stringifyDashboardValue(Number.NaN), "");
});

test("inclui fóruns e canais de mídia nos seletores de texto", () => {
  const field: DashboardFieldDefinition = {
    id: "welcome.channel_id",
    label: "Canal",
    type: "channel",
    scope: "welcome",
    path: "channel_id",
  };
  const channels: DashboardChannelOption[] = [
    { id: "1", name: "texto", type: 0, sendable: true },
    { id: "2", name: "fórum", type: 15, sendable: true },
    { id: "3", name: "mídia", type: 16, sendable: true },
    { id: "4", name: "voz", type: 2, connectable: true },
  ];
  assert.deepEqual(channelOptionsForField(field, channels).map((option) => option.value), ["1", "2", "3"]);
});
