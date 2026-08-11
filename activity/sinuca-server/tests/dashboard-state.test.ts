import assert from "node:assert/strict";
import test from "node:test";
import { applyLegacyFeatureFlags, createDashboardConfigService, resolveDashboardSectionState } from "../src/services/dashboardConfigService.js";

test("expõe somente os estados ativa e desativada", () => {
  const values: Record<string, unknown> = {
    "welcome.enabled": true,
    "welcome.channel_id": "123",
    "forms.enabled": true,
    "forms.form_channel_id": "123",
    "forms.responses_channel_id": "456",
    "tickets.feature_enabled": true,
    "tickets.panel.channel_id": "123",
    "tickets.enabled.other": true,
    "color_roles.enabled": true,
    "color_roles.channel_id": "123",
    "color_roles.slots": { "1": { role_id: "456" } },
    "birthday.enabled": true,
    "birthday.register_channel_id": "123",
    "tts.enabled": true,
  };

  const states = ["welcome", "forms", "tickets", "color_roles", "birthday", "tts"]
    .map((section) => resolveDashboardSectionState(section, values).state);
  assert.deepEqual(new Set(states), new Set(["active"]));

  for (const field of ["welcome.enabled", "forms.enabled", "tickets.feature_enabled", "color_roles.enabled", "birthday.enabled", "tts.enabled"]) {
    values[field] = false;
  }
  const disabledStates = ["welcome", "forms", "tickets", "color_roles", "birthday", "tts"]
    .map((section) => resolveDashboardSectionState(section, values).state);
  assert.deepEqual(new Set(disabledStates), new Set(["inactive"]));
});

test("não considera ativa uma função ligada sem requisitos mínimos", () => {
  const forms = resolveDashboardSectionState("forms", {
    "forms.enabled": true,
    "forms.form_channel_id": "123",
    "forms.responses_channel_id": "",
  });
  assert.equal(forms.state, "inactive");
  assert.equal(forms.status, "Desativada");
  assert.deepEqual(forms.issues, ["Selecione o canal de respostas."]);

  const birthday = resolveDashboardSectionState("birthday", { "birthday.enabled": true });
  assert.equal(birthday.state, "inactive");
  assert.equal(birthday.issues.length, 1);
});

test("configuração Geral não recebe estado de função", () => {
  const general = resolveDashboardSectionState("general", {});
  assert.equal(general.enabled, null);
  assert.equal(general.status, "");
  assert.deepEqual(general.issues, []);
});

test("centraliza o fuso horário em Geral", () => {
  const service = createDashboardConfigService({ mongoUri: "", mongoDbName: "test", mongoCollectionName: "test" });
  const sections = service.listSections();
  const general = sections.find((section) => section.id === "general");
  const birthday = sections.find((section) => section.id === "birthday");
  const welcome = sections.find((section) => section.id === "welcome");

  assert.ok(general?.fields.some((field) => field.id === "general.timezone"));
  assert.ok(!birthday?.fields.some((field) => field.id.includes("timezone")));
  assert.ok(!welcome?.fields.some((field) => field.id.includes("timezone")));
});

test("migra funções já publicadas sem alterar o comportamento existente", () => {
  const raw = {
    forms: { form_channel_id: "1", responses_channel_id: "2", active_message_id: "3" },
    tickets: { panel: { channel_id: "4", message_id: "5" } },
    color_roles: { channel_id: "6", message_ids: ["7"] },
  };
  const merged = structuredClone(raw) as Record<string, unknown>;
  applyLegacyFeatureFlags("guild", raw, merged);

  assert.equal((merged.forms as Record<string, unknown>).enabled, true);
  assert.equal((merged.tickets as Record<string, unknown>).feature_enabled, true);
  assert.equal((merged.color_roles as Record<string, unknown>).enabled, true);
  assert.equal(merged.tts_enabled, true);

  const explicit = { forms: { enabled: false, form_channel_id: "1", responses_channel_id: "2", active_message_id: "3" } };
  const explicitMerged = structuredClone(explicit) as Record<string, unknown>;
  applyLegacyFeatureFlags("guild", explicit, explicitMerged);
  assert.equal((explicitMerged.forms as Record<string, unknown>).enabled, false);
});
