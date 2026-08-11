import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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

test("omite contagem de ativas e mantém os estados individuais", () => {
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

  assert.doesNotMatch(html, />4\/6</);
  assert.doesNotMatch(html, /funções ativas/);
  assert.match(html, /aria-label="1 configuração">1<\/small>/);
  assert.equal((html.match(/>Ativa</g) || []).length, 4);
  assert.equal((html.match(/>Desativada</g) || []).length, 2);
  assert.doesNotMatch(html, /Configuração parcial|Disponível|Não configurada/);
  assert.match(html, /aria-label="Geral"/);
  assert.doesNotMatch(html, /Geral: Desativada/);
});

test("não repete o estado da função em um cartão dentro do editor", () => {
  const source = readFileSync(new URL("../src/components/SectionEditor.tsx", import.meta.url), "utf8");

  assert.doesNotMatch(source, /Função ativa|Função desativada|osk-function-runtime/);
});
