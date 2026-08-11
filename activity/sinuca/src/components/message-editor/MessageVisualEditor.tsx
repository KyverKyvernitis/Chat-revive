import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { Check } from "lucide-react";
import { DashboardFieldControl } from "../DashboardFieldControl";

interface MessageVisualEditorProps {
  fields: DashboardFieldDefinition[];
  baseline: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onFocusField?(field: DashboardFieldDefinition): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  selectedFieldId?: string | null;
  selectedColorSlot?: number | null;
  colorSlotIds?: number[] | null;
  onColorSlotSelect?(slotNumber: number): void;
  contextual?: boolean;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function MessageVisualEditor({
  fields,
  baseline,
  draft,
  guildOptions,
  onChange,
  onFocusField,
  onTextSelection,
  selectedFieldId,
  selectedColorSlot,
  colorSlotIds,
  onColorSlotSelect,
  contextual = false,
}: MessageVisualEditorProps) {
  if (!fields.length) {
    return <div className="osk-message-empty">Nenhum campo está disponível nesta área.</div>;
  }

  return <div className="osk-message-form" data-contextual={contextual || undefined}>
    {fields.map((field) => {
      const changed = !valuesEqual(baseline[field.id], draft[field.id]);
      const currentText = typeof draft[field.id] === "string" ? String(draft[field.id]) : "";
      const configuredOptions = field.options ?? [];
      const contextualOptions = currentText && !configuredOptions.some((option) => option.value === currentText)
        ? [{ value: currentText, label: `${currentText} — valor atual` }, ...configuredOptions]
        : configuredOptions;
      return <section key={field.id} className="osk-message-form__field" data-changed={changed || undefined} data-selected={selectedFieldId === field.id || undefined} data-type={field.type} onFocusCapture={() => onFocusField?.(field)}>
        <header>
          <div><strong>{field.label}</strong>{field.description && <small>{field.description}</small>}</div>
          {field.maxLength && ["text", "textarea", "url"].includes(field.type) && <span>{currentText.length}/{field.maxLength}</span>}
        </header>
        {contextual && field.type === "select" && contextualOptions.length > 0 && contextualOptions.length <= 8 ? (
          <div className="osk-message-context-options" role="listbox" aria-label={field.label}>
            {contextualOptions.map((option) => {
              const selected = String(draft[field.id] ?? "") === option.value;
              return <button
                type="button"
                key={option.value}
                role="option"
                aria-selected={selected}
                data-selected={selected || undefined}
                onClick={() => onChange(field, option.value)}
              >
                <span>{option.label}</span>
                {selected && <Check size={14} aria-hidden="true" />}
              </button>;
            })}
          </div>
        ) : (
          <DashboardFieldControl field={field} value={draft[field.id]} guildOptions={guildOptions} onChange={onChange} onTextSelection={onTextSelection} selectedColorSlot={selectedColorSlot} colorSlotIds={colorSlotIds} onColorSlotSelect={onColorSlotSelect} />
        )}
      </section>;
    })}
  </div>;
}
