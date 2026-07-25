import { ArrowDown, ArrowUp, Minus, PencilLine, Plus } from "lucide-react";
import type {
  DashboardFieldDefinition,
  DashboardMessageEditorDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";
import {
  COLOR_PANEL_MAX,
  colorPanelEditorId,
  createColorPanelId,
  nextUnusedColorSlot,
  normalizeColorPanelLayout,
} from "./colorRolesModel";

interface ColorRolesPanelManagerProps {
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onOpenEditor(editor: DashboardMessageEditorDefinition): void;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function ColorRolesPanelManager({ fields, values, draft, guildOptions, onChange, onOpenEditor }: ColorRolesPanelManagerProps) {
  const channelField = fields.find((field) => field.id === "color_roles.channel_id");
  const layoutField = fields.find((field) => field.id === "color_roles.panel_layout");
  const slotsField = fields.find((field) => field.id === "color_roles.slots");
  if (!layoutField || !slotsField) return <div className="osk-inline-note">A configuração dos painéis não está disponível nesta versão.</div>;

  const layout = normalizeColorPanelLayout(draft[layoutField.id]);
  const changed = !valuesEqual(values[layoutField.id], draft[layoutField.id]);
  const nextSlot = nextUnusedColorSlot(layout);

  const commit = (next: typeof layout) => onChange(layoutField, next);
  const movePanel = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= layout.length) return;
    const next = [...layout];
    [next[index], next[target]] = [next[target], next[index]];
    commit(next);
  };
  const removePanel = (index: number) => {
    if (layout.length <= 1) return;
    const panel = layout[index];
    if (panel.slots.length && !window.confirm(`Excluir o Painel ${index + 1} e suas ${panel.slots.length} opção(ões) do envio?`)) return;
    commit(layout.filter((_, current) => current !== index));
  };
  const addPanel = () => {
    if (layout.length >= COLOR_PANEL_MAX || nextSlot === null) return;
    commit([...layout, { id: createColorPanelId(), slots: [nextSlot] }]);
  };
  const editPanel = (index: number) => {
    const panel = layout[index];
    onOpenEditor({
      id: colorPanelEditorId(panel),
      label: `Painel ${index + 1}`,
      description: "Edite a imagem e organize as opções deste painel.",
      presentation: "color_panel",
      fieldIds: [layoutField.id, slotsField.id],
    });
  };

  return <div className="osk-color-panel-manager" data-changed={changed || undefined}>
    {channelField && <div className="osk-color-panel-manager__channel">
      <div><strong>{channelField.label}</strong>{channelField.description && <small>{channelField.description}</small>}</div>
      <DashboardFieldControl field={channelField} value={draft[channelField.id]} guildOptions={guildOptions} onChange={onChange} />
    </div>}

    <div className="osk-color-panel-manager__heading">
      <div><strong>Painéis</strong><small>Até 3 painéis, com no máximo 10 opções em cada um.</small></div>
      <span>{layout.length}/{COLOR_PANEL_MAX}</span>
    </div>

    <div className="osk-color-panel-manager__list">
      {layout.map((panel, index) => <article key={panel.id} className="osk-color-panel-manager__item">
        <div className="osk-color-panel-manager__order">
          <button type="button" disabled={index === 0} onClick={() => movePanel(index, -1)} aria-label={`Mover Painel ${index + 1} para cima`}><ArrowUp size={15} /></button>
          <button type="button" disabled={index === layout.length - 1} onClick={() => movePanel(index, 1)} aria-label={`Mover Painel ${index + 1} para baixo`}><ArrowDown size={15} /></button>
        </div>
        <div className="osk-color-panel-manager__copy"><strong>Painel {index + 1}</strong><small>{panel.slots.length} de 10 opções</small></div>
        <button type="button" className="osk-color-panel-manager__edit" onClick={() => editPanel(index)}><PencilLine size={16} />Editar</button>
        <button type="button" className="osk-color-panel-manager__remove" disabled={layout.length <= 1} onClick={() => removePanel(index)} aria-label={`Remover Painel ${index + 1}`}><Minus size={18} /></button>
      </article>)}
    </div>

    <button type="button" className="osk-color-panel-manager__add" disabled={layout.length >= COLOR_PANEL_MAX || nextSlot === null} onClick={addPanel}>
      <Plus size={17} />{layout.length >= COLOR_PANEL_MAX ? "Limite de 3 painéis" : "Adicionar painel"}
    </button>
  </div>;
}
