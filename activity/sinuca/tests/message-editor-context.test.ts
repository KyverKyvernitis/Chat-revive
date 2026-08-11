import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DiscordRichText } from "../src/components/message-editor/DiscordRichText";
import { MessageVisualEditor } from "../src/components/message-editor/MessageVisualEditor";
import type { DashboardFieldDefinition } from "../src/types/dashboard";

const imageModeField: DashboardFieldDefinition = {
  id: "welcome.embed.thumbnail_mode",
  label: "Thumbnail",
  type: "select",
  scope: "welcome",
  path: "embed.thumbnail_mode",
  options: [
    { value: "none", label: "Sem thumbnail" },
    { value: "member", label: "Avatar do membro" },
    { value: "server", label: "Avatar do servidor" },
    { value: "custom", label: "Imagem personalizada" },
  ],
};

function renderEditor(contextual: boolean) {
  return renderToStaticMarkup(React.createElement(MessageVisualEditor, {
    fields: [imageModeField],
    baseline: { [imageModeField.id]: "none" },
    draft: { [imageModeField.id]: "member" },
    guildOptions: null,
    contextual,
    onChange() {},
  }));
}

test("abre escolhas curtas como dropdown contextual sem trocar a prévia", () => {
  const html = renderEditor(true);

  assert.match(html, /class="osk-message-context-options"/);
  assert.match(html, /role="listbox"/);
  assert.match(html, /Avatar do membro/);
  assert.match(html, /aria-selected="true"/);
  assert.doesNotMatch(html, /class="osk-select-trigger"/);
});

test("mantém o seletor reutilizável fora do editor contextual", () => {
  const html = renderEditor(false);

  assert.match(html, /class="osk-select-trigger"/);
  assert.doesNotMatch(html, /class="osk-message-context-options"/);
});

test("mantém o canvas montado enquanto propriedades, variáveis ou JSON estão abertos", () => {
  const source = readFileSync(new URL("../src/components/message-editor/MessageEditor.tsx", import.meta.url), "utf8");

  assert.match(source, /className="osk-message-editor__canvas-pane"/);
  assert.match(source, /view !== "canvas" && \(/);
  assert.match(source, /className="osk-message-editor__context-layer"/);
  assert.doesNotMatch(source, /\{view === "canvas" \? \(\s*<section className="osk-message-editor__canvas-pane"/);
});

test("o mesmo editor compartilhado continua atendendo todas as mensagens dos módulos", () => {
  const source = readFileSync(new URL("../src/components/SectionEditor.tsx", import.meta.url), "utf8");

  assert.match(source, /import \{ MessageEditor \} from "\.\/message-editor"/);
  assert.match(source, /<MessageEditor/);
  assert.match(source, /metadata\.editors\?\.length/);
});

test("a prévia interpreta as opções expostas pela barra de formatação", () => {
  const html = renderToStaticMarkup(React.createElement(DiscordRichText, {
    text: "__sublinhado__ ||segredo||\n> citação\n[documentação](https://example.com)",
  }));

  assert.match(html, /<u>sublinhado<\/u>/);
  assert.match(html, /class="osk-discord-spoiler"/);
  assert.match(html, /class="osk-discord-quote"/);
  assert.match(html, /class="osk-discord-link" title="https:\/\/example\.com">documentação/);
});
