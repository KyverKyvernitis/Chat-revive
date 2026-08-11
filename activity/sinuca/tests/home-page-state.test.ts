import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Settings } from "lucide-react";
import { HomePage } from "../src/components/HomePage";
import type { DashboardVisualModule } from "../src/moduleCatalog";

function module(id: string, state: "active" | "inactive", group: "main" | "system" = "main"): DashboardVisualModule {
  return {
    id,
    label: id === "general" ? "Geral" : `Função ${id}`,
    emoji: "",
    description: "Descrição breve",
    enabled: group === "main" ? state === "active" : null,
    state,
    configured: 0,
    total: 0,
    status: group === "main" ? (state === "active" ? "Ativa" : "Desativada") : "",
    issues: [],
    icon: Settings,
    group,
    available: true,
  };
}

test("mostra contagem de ativas e omite estado de Geral", () => {
  const modules = [
    module("welcome", "active"),
    module("forms", "active"),
    module("tickets", "inactive"),
    module("color_roles", "active"),
    module("birthday", "inactive"),
    module("tts", "active"),
    module("general", "inactive", "system"),
  ];
  const html = renderToStaticMarkup(React.createElement(HomePage, { modules, onOpen() {} }));

  assert.match(html, />4\/6</);
  assert.equal((html.match(/>Ativa</g) || []).length, 4);
  assert.equal((html.match(/>Desativada</g) || []).length, 2);
  assert.doesNotMatch(html, /Configuração parcial|Disponível|Não configurada/);
  assert.match(html, /aria-label="Geral"/);
  assert.doesNotMatch(html, /Geral: Desativada/);
});
