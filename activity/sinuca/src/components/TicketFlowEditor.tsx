import { ChevronDown, MessageSquareText, Route, Settings2 } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import type { DashboardFieldDefinition } from "../types/dashboard";
import { DashboardFieldControl } from "./DashboardFieldControl";

const FLOW_DEFINITIONS = [
  { id: "partnership", label: "Parceria", fallback: "Atendimento para propostas de parceria." },
  { id: "report", label: "Denúncia", fallback: "Atendimento com formulário para denúncias." },
  { id: "suggestion", label: "Sugestão", fallback: "Envio de sugestões para a equipe." },
  { id: "other", label: "Outros", fallback: "Atendimento geral para outros assuntos." },
] as const;

interface TicketFlowEditorProps {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}

function fieldValue(draft: Record<string, unknown>, field: DashboardFieldDefinition | undefined): string {
  const value = field ? draft[field.id] : "";
  return value === null || value === undefined ? "" : String(value);
}

function shortField(field: DashboardFieldDefinition, flowLabel: string): DashboardFieldDefinition {
  return {
    ...field,
    label: field.label.replace(new RegExp(`^${flowLabel}:\\s*`, "i"), ""),
  };
}

export function TicketFlowEditor({ fields, draft, renderFields, onChange }: TicketFlowEditorProps) {
  const [openFlow, setOpenFlow] = useState<string | null>(null);

  const flowData = useMemo(() => FLOW_DEFINITIONS.map((definition) => {
    const enabledField = fields.find((field) => field.id === `tickets.enabled.${definition.id}`);
    const optionFields = fields.filter((field) => field.id.startsWith(`tickets.option_items.${definition.id}.`));
    const labelField = optionFields.find((field) => field.id.endsWith(".label"));
    const descriptionField = optionFields.find((field) => field.id.endsWith(".description"));
    const flowField = optionFields.find((field) => field.id.endsWith(".flow"));
    return { definition, enabledField, optionFields, labelField, descriptionField, flowField };
  }), [fields]);

  return <div className="osk-ticket-flows">
    {flowData.map(({ definition, enabledField, optionFields, labelField, descriptionField, flowField }) => {
      if (!enabledField) return null;
      const open = openFlow === definition.id;
      const enabled = Boolean(draft[enabledField.id]);
      const label = fieldValue(draft, labelField).trim() || definition.label;
      const description = fieldValue(draft, descriptionField).trim() || definition.fallback;
      const behavior = flowField?.options?.find((option) => option.value === fieldValue(draft, flowField))?.label || "Comportamento padrão";
      const flow = fieldValue(draft, flowField);
      const panelId = `ticket-flow-panel-${definition.id}`;
      const visibleOptionFields = optionFields.filter((field) => {
        if (field.id.endsWith(".confirmation_text")) return flow === "confirm_ticket";
        if ([".modal_title", ".modal_notice", ".subject_label", ".body_label"].some((suffix) => field.id.endsWith(suffix))) {
          return flow === "modal_ticket" || flow === "modal_channel";
        }
        if (field.id.endsWith(".target_channel_id")) return flow === "modal_channel";
        if (field.id.endsWith(".use_report_types")) return definition.id === "report" && (flow === "modal_ticket" || flow === "modal_channel");
        return true;
      }).map((field) => shortField(field, definition.label));

      const identityFields = visibleOptionFields.filter((field) => [".label", ".emoji", ".description"].some((suffix) => field.id.endsWith(suffix)));
      const behaviorFields = visibleOptionFields.filter((field) => [".flow", ".target_channel_id", ".use_report_types"].some((suffix) => field.id.endsWith(suffix)));
      const messageFields = visibleOptionFields.filter((field) => [".confirmation_text", ".opening_text"].some((suffix) => field.id.endsWith(suffix)));
      const modalFields = visibleOptionFields.filter((field) => [".modal_title", ".modal_notice", ".subject_label", ".body_label"].some((suffix) => field.id.endsWith(suffix)));

      return <article key={definition.id} className="osk-ticket-flow" data-open={open || undefined} data-enabled={enabled || undefined}>
        <div className="osk-ticket-flow__header">
          <button type="button" className="osk-ticket-flow__summary" onClick={() => setOpenFlow((current) => current === definition.id ? null : definition.id)} aria-expanded={open} aria-controls={panelId}>
            <span className="osk-ticket-flow__icon"><Route size={18} /></span>
            <span><strong>{label}</strong><small>{description}</small><em>{behavior}</em></span>
            <ChevronDown size={18} className="osk-ticket-flow__chevron" />
          </button>
          <DashboardFieldControl field={enabledField} value={draft[enabledField.id]} guildOptions={null} onChange={onChange} />
        </div>
        {open && <div className="osk-ticket-flow__panel" id={panelId}>
          <div className="osk-ticket-flow__panel-inner">
            {!enabled && <div className="osk-inline-note">O fluxo está desativado. Os valores abaixo serão preservados e voltam a valer quando ele for ativado.</div>}
            {identityFields.length > 0 && <section className="osk-ticket-flow__block"><h3><Settings2 size={16} />Identidade</h3>{renderFields(identityFields)}</section>}
            {behaviorFields.length > 0 && <section className="osk-ticket-flow__block"><h3><Route size={16} />Comportamento</h3>{renderFields(behaviorFields)}</section>}
            {messageFields.length > 0 && <section className="osk-ticket-flow__block"><h3><MessageSquareText size={16} />Mensagens</h3>{renderFields(messageFields)}</section>}
            {modalFields.length > 0 && <section className="osk-ticket-flow__block"><h3><MessageSquareText size={16} />Formulário</h3>{renderFields(modalFields)}</section>}
          </div>
        </div>}
      </article>;
    })}
  </div>;
}
