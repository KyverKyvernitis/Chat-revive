import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  Copy,
  GripVertical,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type {
  DashboardChannelOption,
  DashboardColorSlot,
  DashboardFieldDefinition,
  DashboardFormField,
  DashboardOptionsPayload,
  DashboardRoleOption,
} from "../types/dashboard";
import { SmartSelect, type SmartSelectOption } from "./SmartSelect";
import { colorRoleHex, colorRoleLabel } from "./color-roles/colorRolesModel";

interface DashboardFieldControlProps {
  field: DashboardFieldDefinition;
  value: unknown;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  selectedColorSlot?: number | null;
  colorSlotIds?: number[] | null;
  onColorSlotSelect?(slotNumber: number): void;
}

const TEXT_LIKE_CHANNEL_TYPES = new Set([0, 5, 15, 16]);
const VOICE_CHANNEL_TYPES = new Set([2, 13]);
const CATEGORY_CHANNEL_TYPE = 4;
const FUNCTION_TOGGLE_IDS = new Set([
  "welcome.enabled",
  "forms.enabled",
  "tickets.feature_enabled",
  "color_roles.enabled",
  "birthday.enabled",
  "tts.enabled",
]);

function channelKindForField(field: DashboardFieldDefinition): "category" | "voice" | "text" {
  const hint = `${field.id} ${field.path}`.toLowerCase();
  if (hint.includes("category")) return "category";
  if (hint.includes("voice")) return "voice";
  return "text";
}

export function channelOptionsForField(field: DashboardFieldDefinition, channels: DashboardChannelOption[]): SmartSelectOption[] {
  const kind = channelKindForField(field);
  return channels.filter((channel) => {
    if (kind === "category") return channel.type === CATEGORY_CHANNEL_TYPE;
    if (kind === "voice") return VOICE_CHANNEL_TYPES.has(channel.type);
    return TEXT_LIKE_CHANNEL_TYPES.has(channel.type);
  }).map((channel) => {
    const permissionKnown = channel.permissionsKnown === true;
    const isWebhookChannel = field.id.includes("webhook.channel_id");
    const permissionAllowed = isWebhookChannel
      ? channel.webhookManageable !== false
      : kind === "category"
      ? channel.manageable !== false
      : kind === "voice"
        ? channel.connectable !== false
        : channel.sendable !== false;
    const permissionHint = !permissionKnown || permissionAllowed
      ? null
      : isWebhookChannel
        ? "A Osaka não pode gerenciar webhooks neste canal"
        : kind === "category"
        ? "A Osaka não pode gerenciar canais nesta categoria"
        : kind === "voice"
          ? "A Osaka não pode visualizar ou conectar neste canal"
          : "A Osaka não pode visualizar ou enviar mensagens neste canal";
    const organizationHint = channel.parentId ? "Canal organizado em uma categoria" : null;
    return {
      value: channel.id,
      label: kind === "category" ? `📁 ${channel.name}` : kind === "voice" ? `🔊 ${channel.name}` : `# ${channel.name}`,
      hint: permissionHint ?? organizationHint ?? undefined,
      disabled: Boolean(permissionHint),
    };
  });
}

export function stringifyDashboardValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  return String(value);
}

export function displayDashboardValue(field: DashboardFieldDefinition, value: unknown, guildOptions: DashboardOptionsPayload | null): string {
  if (field.type === "boolean") return value ? "Ligado" : "Desligado";
  if (field.type === "role_multi") return Array.isArray(value) && value.length ? `${value.length} cargo${value.length === 1 ? "" : "s"}` : "Nenhum cargo";
  if (field.type === "string_list") return Array.isArray(value) && value.length ? `${value.length} item${value.length === 1 ? "" : "s"}` : "Lista vazia";
  if (field.type === "form_fields") return Array.isArray(value) && value.length ? `${value.length} pergunta${value.length === 1 ? "" : "s"}` : "Nenhuma pergunta";
  if (field.type === "color_slots") return value && typeof value === "object" ? `${Object.keys(value as object).length} opções` : "Nenhuma opção";
  if (field.type === "color_panel_layout") return Array.isArray(value) ? `${value.length} painel${value.length === 1 ? "" : "éis"}` : "Nenhum painel";
  if (field.type === "channel" || field.type === "role") {
    const id = stringifyDashboardValue(value);
    if (!id || Number(id) <= 0) return "Não configurado";
    const list = field.type === "channel" ? guildOptions?.channels : guildOptions?.roles;
    const match = list?.find((item) => item.id === id);
    if (match) return field.type === "channel" ? `#${match.name}` : `@${match.name}`;
    return id;
  }
  if (field.type === "select") {
    const raw = stringifyDashboardValue(value);
    return (field.options?.find((item) => item.value === raw)?.label ?? raw) || "Não configurado";
  }
  const text = stringifyDashboardValue(value).trim();
  return text || "Não configurado";
}

export function DashboardFieldControl({ field, value, guildOptions, onChange, onTextSelection, selectedColorSlot, colorSlotIds, onColorSlotSelect }: DashboardFieldControlProps) {
  const currentValue = stringifyDashboardValue(value);
  const channelOptions = field.type === "channel" && guildOptions?.ok
    ? (() => {
      const available = channelOptionsForField(field, guildOptions.channels);
      if (!currentValue || available.some((option) => option.value === currentValue)) return available;
      const channel = guildOptions.channels.find((item) => item.id === currentValue);
      const kind = channelKindForField(field);
      const label = channel
        ? kind === "category" ? `📁 ${channel.name}` : kind === "voice" ? `🔊 ${channel.name}` : `# ${channel.name}`
        : `Canal ${currentValue}`;
      return [{
        value: currentValue,
        label,
        hint: "Canal atual indisponível para esta configuração",
        disabled: true,
      }, ...available];
    })()
    : null;
  const roleOptions = (field.type === "role" || field.type === "role_multi") && guildOptions?.ok
    ? (() => {
      const available = guildOptions.roles
        .filter((role) => !role.managed && role.assignable !== false)
        .map((role) => ({ value: role.id, label: `@${role.name}`, hint: role.color ? `Cor do cargo: #${role.color.toString(16).padStart(6, "0")}` : undefined }));
      const currentIds = field.type === "role_multi" && Array.isArray(value) ? value.map(String) : currentValue ? [currentValue] : [];
      const unavailable = currentIds
        .filter((id) => !available.some((option) => option.value === id))
        .map((id) => {
          const role = guildOptions.roles.find((item) => item.id === id);
          return { value: id, label: role ? `@${role.name}` : `Cargo ${id}`, hint: "Cargo atual indisponível para atribuição" };
        });
      return [...unavailable, ...available];
    })()
    : null;

  if (field.type === "boolean") {
    const stateLabel = FUNCTION_TOGGLE_IDS.has(field.id)
      ? Boolean(value) ? "Ativa" : "Desativada"
      : Boolean(value) ? "Ativado" : "Desativado";
    return <label className="osk-switch"><input type="checkbox" aria-label={field.label} checked={Boolean(value)} onChange={(event) => onChange(field, event.target.checked)} /><span className="osk-switch-track" /><span className="osk-switch-state">{stateLabel}</span></label>;
  }

  if (field.id === "tts.rate") {
    const numeric = Math.max(-100, Math.min(100, Number.parseInt(currentValue, 10) || 0));
    return <div className="osk-range-control"><input type="range" aria-label={field.label} min={-100} max={100} step={5} value={numeric} onChange={(event) => { const next = Number(event.target.value); onChange(field, `${next >= 0 ? "+" : ""}${next}%`); }} /><output>{numeric >= 0 ? "+" : ""}{numeric}%</output></div>;
  }

  if (field.id === "tts.pitch") {
    const numeric = Math.max(-100, Math.min(100, Number.parseInt(currentValue, 10) || 0));
    return <div className="osk-range-control"><input type="range" aria-label={field.label} min={-100} max={100} step={5} value={numeric} onChange={(event) => { const next = Number(event.target.value); onChange(field, `${next >= 0 ? "+" : ""}${next}Hz`); }} /><output>{numeric >= 0 ? "+" : ""}{numeric}Hz</output></div>;
  }

  if (field.type === "select") {
    const configuredOptions = field.options ?? [];
    const options = currentValue && !configuredOptions.some((option) => option.value === currentValue)
      ? [{ value: currentValue, label: `${currentValue} — valor atual` }, ...configuredOptions]
      : configuredOptions;
    return <SmartSelect id={`field-${field.id}`} ariaLabel={field.label} value={currentValue} options={options} onChange={(next) => onChange(field, next)} placeholder="Selecione uma opção" />;
  }

  if (field.type === "channel" && channelOptions) {
    return <SmartSelect id={`field-${field.id}`} ariaLabel={field.label} value={currentValue} options={[{ value: "", label: "Nenhum" }, ...channelOptions]} onChange={(next) => onChange(field, next)} placeholder="Selecione um canal" emptyLabel="Nenhum canal compatível encontrado" />;
  }

  if (field.type === "role" && roleOptions) {
    return <SmartSelect id={`field-${field.id}`} ariaLabel={field.label} value={currentValue} options={[{ value: "", label: "Nenhum" }, ...roleOptions]} onChange={(next) => onChange(field, next)} placeholder="Selecione um cargo" emptyLabel="Nenhum cargo encontrado" />;
  }

  if (field.type === "role_multi") {
    return <RoleMultiEditor field={field} value={value} options={roleOptions ?? []} onChange={onChange} />;
  }

  if (field.type === "string_list") {
    return <StringListEditor field={field} value={value} onChange={onChange} />;
  }

  if (field.type === "form_fields") {
    return <FormFieldsEditor field={field} value={value} onChange={onChange} />;
  }

  if (field.type === "color_slots") {
    return <ColorSlotsEditor
      field={field}
      value={value}
      roles={guildOptions?.ok ? guildOptions.roles : []}
      selectedSlot={selectedColorSlot}
      visibleSlotIds={colorSlotIds}
      onSelectSlot={onColorSlotSelect}
      onChange={onChange}
    />;
  }

  if (field.type === "textarea") {
    return <textarea
      aria-label={field.label}
      data-message-field-id={field.id}
      value={currentValue}
      maxLength={field.maxLength}
      placeholder={field.placeholder}
      rows={Math.min(8, Math.max(3, currentValue.split("\n").length + 1))}
      onFocus={(event) => onTextSelection?.(field, event.currentTarget.selectionStart, event.currentTarget.selectionEnd)}
      onSelect={(event) => onTextSelection?.(field, event.currentTarget.selectionStart, event.currentTarget.selectionEnd)}
      onChange={(event) => {
        onTextSelection?.(field, event.currentTarget.selectionStart, event.currentTarget.selectionEnd);
        onChange(field, event.target.value);
      }}
    />;
  }

  const suffix = field.id === "tts.speech_limit_seconds" ? "segundos" : null;
  return <div className={`${field.type === "color" ? "osk-color-input" : ""}${suffix ? " osk-input-suffix" : ""}`.trim()}>
    {field.type === "color" && <input type="color" value={/^#[0-9a-f]{6}$/i.test(currentValue) ? currentValue : "#5865f2"} onChange={(event) => onChange(field, event.target.value)} aria-label={field.label} />}
    <input
      aria-label={field.label}
      data-message-field-id={field.type === "text" ? field.id : undefined}
      type={field.type === "number" ? "number" : field.type === "url" ? "url" : "text"}
      min={field.min}
      max={field.max}
      maxLength={field.maxLength}
      value={currentValue}
      placeholder={field.placeholder ?? (field.type === "channel" ? "ID do canal" : field.type === "role" ? "ID do cargo" : field.type === "url" ? "https://..." : "")}
      onFocus={field.type === "text" ? (event) => onTextSelection?.(field, event.currentTarget.selectionStart ?? currentValue.length, event.currentTarget.selectionEnd ?? currentValue.length) : undefined}
      onSelect={field.type === "text" ? (event) => onTextSelection?.(field, event.currentTarget.selectionStart ?? currentValue.length, event.currentTarget.selectionEnd ?? currentValue.length) : undefined}
      onChange={(event) => {
        if (field.type === "text") onTextSelection?.(field, event.currentTarget.selectionStart ?? event.currentTarget.value.length, event.currentTarget.selectionEnd ?? event.currentTarget.value.length);
        onChange(field, event.target.value);
      }}
    />
    {suffix && <span>{suffix}</span>}
  </div>;
}

function RoleMultiEditor({ field, value, options, onChange }: { field: DashboardFieldDefinition; value: unknown; options: SmartSelectOption[]; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const selected = Array.isArray(value) ? value.map(String) : [];
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [working, setWorking] = useState<string[]>(selected);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (!open) setWorking(selected); }, [open, value]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : triggerRef.current;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => searchRef.current?.focus(), 0);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = Array.from(panelRef.current.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
      )).filter((element) => !element.hasAttribute("hidden"));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKey);
      window.requestAnimationFrame(() => previousFocus?.focus({ preventScroll: true }));
    };
  }, [open]);

  if (!options.length) return <input aria-label={field.label} value={selected.join(", ")} placeholder="IDs separados por vírgula" onChange={(event) => onChange(field, event.target.value.split(/[\s,]+/).filter(Boolean))} />;

  const selectedOptions = selected.map((id) => options.find((option) => option.value === id) ?? { value: id, label: `Cargo ${id}` });
  const needle = query.trim().toLocaleLowerCase("pt-BR");
  const filtered = needle ? options.filter((option) => `${option.label} ${option.hint || ""}`.toLocaleLowerCase("pt-BR").includes(needle)) : options;
  const toggle = (roleId: string) => setWorking((current) => current.includes(roleId) ? current.filter((id) => id !== roleId) : [...current, roleId]);

  const modal = open ? <div className="osk-root osk-multi-sheet" role="dialog" aria-modal="true" aria-label={`Selecionar ${field.label}`}>
    <button type="button" className="osk-multi-sheet__backdrop" onClick={() => setOpen(false)} aria-label="Fechar" />
    <div className="osk-multi-sheet__panel" ref={panelRef}>
      <header><div><strong>{field.label}</strong><small>{working.length} selecionado{working.length === 1 ? "" : "s"}</small></div><button type="button" onClick={() => setOpen(false)} aria-label="Fechar"><X size={18} /></button></header>
      <label className="osk-multi-sheet__search"><Search size={16} /><input ref={searchRef} aria-label="Buscar cargo" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar cargo" /></label>
      <div className="osk-multi-sheet__list">
        {filtered.map((option) => <button key={option.value} type="button" data-selected={working.includes(option.value) || undefined} onClick={() => toggle(option.value)}>
          <span><strong>{option.label}</strong>{option.hint && <small>{option.hint}</small>}</span>{working.includes(option.value) && <Check size={17} />}
        </button>)}
        {!filtered.length && <div className="osk-message-empty">Nenhum cargo encontrado.</div>}
      </div>
      <footer><button type="button" className="osk-secondary-button" onClick={() => { setWorking([]); }}>Limpar</button><button type="button" className="osk-primary-button" onClick={() => { onChange(field, working); setOpen(false); }}>Concluir</button></footer>
    </div>
  </div> : null;

  return <div className="osk-role-multi">
    <div className="osk-role-multi__chips">
      {selectedOptions.map((option) => <span key={option.value}>{option.label}<button type="button" onClick={() => onChange(field, selected.filter((id) => id !== option.value))} aria-label={`Remover ${option.label}`}><X size={13} /></button></span>)}
      {!selectedOptions.length && <small>Nenhum cargo selecionado.</small>}
    </div>
    <button ref={triggerRef} type="button" className="osk-secondary-button osk-role-multi__open" aria-haspopup="dialog" aria-expanded={open} onClick={() => { setWorking(selected); setOpen(true); }}><Plus size={15} />Selecionar cargos</button>
    {modal && createPortal(modal, document.body)}
  </div>;
}

function StringListEditor({ field, value, onChange }: { field: DashboardFieldDefinition; value: unknown; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const items = Array.isArray(value) ? value.map(String) : stringifyDashboardValue(value).split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const update = (index: number, next: string) => onChange(field, items.map((item, itemIndex) => itemIndex === index ? next : item));
  const remove = (index: number) => onChange(field, items.filter((_, itemIndex) => itemIndex !== index));
  const move = (index: number, direction: -1 | 1) => {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= items.length) return;
    const next = [...items];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    onChange(field, next);
  };
  return <div className="osk-string-list-editor">
    {items.map((item, index) => <div key={index}>
      <GripVertical size={15} />
      <input aria-label={`${field.label}, item ${index + 1}`} value={item} onChange={(event) => update(index, event.target.value)} placeholder={`Item ${index + 1}`} />
      <button type="button" onClick={() => move(index, -1)} disabled={index === 0} aria-label="Mover para cima"><ArrowUp size={14} /></button>
      <button type="button" onClick={() => move(index, 1)} disabled={index === items.length - 1} aria-label="Mover para baixo"><ArrowDown size={14} /></button>
      <button type="button" data-danger onClick={() => remove(index)} aria-label="Remover item"><Trash2 size={14} /></button>
    </div>)}
    <button type="button" className="osk-add-row" onClick={() => onChange(field, [...items, ""])}><Plus size={15} />Adicionar item</button>
  </div>;
}

function FormFieldsEditor({ field, value, onChange }: { field: DashboardFieldDefinition; value: unknown; onChange(field: DashboardFieldDefinition, raw: unknown): void }) {
  const fields = Array.isArray(value) ? value.map((item, index) => normalizeFormField(item, index)) : [];
  const [openId, setOpenId] = useState<string | null>(fields[0]?.id ?? null);
  const update = (index: number, patch: Partial<DashboardFormField>) => onChange(field, fields.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  const remove = (index: number) => {
    const target = fields[index];
    if ((target.label || target.placeholder) && !window.confirm(`Excluir “${target.label || `Pergunta ${index + 1}`}”?`)) return;
    onChange(field, fields.filter((_, itemIndex) => itemIndex !== index));
  };
  const add = () => {
    if (fields.length >= 5) return;
    const next = normalizeFormField({ id: `field-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}` }, fields.length);
    onChange(field, [...fields, next]);
    setOpenId(next.id);
  };
  const duplicate = (index: number) => {
    if (fields.length >= 5) return;
    const copy = { ...fields[index], id: `${fields[index].id}-copy-${Date.now().toString(36)}`, label: `${fields[index].label} (cópia)` };
    onChange(field, [...fields.slice(0, index + 1), copy, ...fields.slice(index + 1)]);
    setOpenId(copy.id);
  };
  const move = (index: number, direction: -1 | 1) => {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= fields.length) return;
    const next = [...fields];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    onChange(field, next);
  };

  return <div className="osk-form-fields-editor">
    <div className="osk-form-fields-limit"><strong>{fields.length} de 5 perguntas</strong><small>O Discord aceita no máximo cinco campos por formulário.</small></div>
    {fields.map((item, index) => {
      const open = openId === item.id;
      const panelId = `form-question-panel-${item.id || index}`;
      return <article key={item.id || index} data-open={open || undefined} data-enabled={item.enabled || undefined}>
        <div className="osk-form-question-head">
          <button type="button" className="osk-form-question-summary" onClick={() => setOpenId((current) => current === item.id ? null : item.id)} aria-expanded={open} aria-controls={panelId}>
            <span><strong>{item.label || `Pergunta ${index + 1}`}</strong><small>{item.enabled ? "Ativa" : "Desativada"} · {item.required ? "Obrigatória" : "Opcional"} · {item.long ? "Resposta longa" : "Resposta curta"}</small></span><ChevronDown size={16} />
          </button>
          <div className="osk-form-question-actions">
            <button type="button" onClick={() => duplicate(index)} disabled={fields.length >= 5} aria-label="Duplicar pergunta"><Copy size={14} /></button>
            <button type="button" onClick={() => move(index, -1)} disabled={index === 0} aria-label="Mover para cima"><ArrowUp size={14} /></button>
            <button type="button" onClick={() => move(index, 1)} disabled={index === fields.length - 1} aria-label="Mover para baixo"><ArrowDown size={14} /></button>
            <button type="button" data-danger onClick={() => remove(index)} aria-label="Remover pergunta"><Trash2 size={14} /></button>
          </div>
        </div>
        {open && <div className="osk-form-question-panel" id={panelId}>
          <div className="osk-form-question-panel-inner">
            <div className="osk-inline-grid"><label><span>Rótulo</span><input value={item.label} maxLength={45} onChange={(event) => update(index, { label: event.target.value })} /></label><label><span>Título na resposta da equipe</span><input value={item.response_label} maxLength={80} onChange={(event) => update(index, { response_label: event.target.value })} /></label></div>
            <label><span>Exemplo ou instrução</span><input value={item.placeholder} maxLength={100} onChange={(event) => update(index, { placeholder: event.target.value })} /></label>
            <div className="osk-form-question-switches">
              <SwitchRow label="Exibir pergunta" description="Exibe este campo no formulário." checked={item.enabled} onChange={(checked) => update(index, { enabled: checked })} />
              <SwitchRow label="Resposta obrigatória" description="Impede o envio sem preencher." checked={item.required} onChange={(checked) => update(index, { required: checked })} />
              <SwitchRow label="Campo de resposta longa" description="Usa uma área maior para textos extensos." checked={item.long} onChange={(checked) => update(index, { long: checked, max_length: checked ? Math.max(item.max_length, 1000) : Math.min(item.max_length, 120) })} />
              <SwitchRow label="Mostrar no resumo" description="Inclui a resposta na mensagem enviada à equipe." checked={item.show_in_response} onChange={(checked) => update(index, { show_in_response: checked })} />
            </div>
          </div>
        </div>}
      </article>;
    })}
    <button type="button" className="osk-add-row" onClick={add} disabled={fields.length >= 5}><Plus size={15} />Adicionar pergunta</button>
  </div>;
}

function SwitchRow({ label, description, checked, onChange }: { label: string; description: string; checked: boolean; onChange(value: boolean): void }) {
  return <label className="osk-inline-switch-row"><span><strong>{label}</strong><small>{description}</small></span><span className="osk-switch"><input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} /><span className="osk-switch-track" /></span></label>;
}

function normalizeFormField(value: unknown, index: number): DashboardFormField {
  const raw = value && typeof value === "object" ? value as Partial<DashboardFormField> : {};
  return {
    id: String(raw.id || `field${index + 1}`), label: String(raw.label || `Pergunta ${index + 1}`), placeholder: String(raw.placeholder || ""),
    response_label: String(raw.response_label || raw.label || `Pergunta ${index + 1}`), required: raw.required !== false, long: Boolean(raw.long),
    show_in_response: raw.show_in_response !== false, enabled: raw.enabled !== false, min_length: Number(raw.min_length || 0), max_length: Number(raw.max_length || (raw.long ? 1000 : 120)),
  };
}

function ColorSlotsEditor({
  field,
  value,
  roles,
  selectedSlot,
  visibleSlotIds,
  onSelectSlot,
  onChange,
}: {
  field: DashboardFieldDefinition;
  value: unknown;
  roles: DashboardRoleOption[];
  selectedSlot?: number | null;
  visibleSlotIds?: number[] | null;
  onSelectSlot?(slotNumber: number): void;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}) {
  const slots = value && typeof value === "object" ? value as Record<string, DashboardColorSlot> : {};
  const order = visibleSlotIds?.length ? visibleSlotIds : Object.keys(slots).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
  const visible = selectedSlot && order.includes(selectedSlot) ? [selectedSlot] : order;
  const availableRoles = roles
    .filter((role) => !role.managed && role.assignable !== false)
    .map((role) => {
      const roleColor = Math.max(0, Number(role.color || 0));
      return {
        value: role.id,
        label: `@${role.name}`,
        hint: roleColor > 0
          ? `Cor do cargo: #${roleColor.toString(16).padStart(6, "0").slice(-6).toUpperCase()}`
          : "Cargo sem cor própria",
      };
    });

  const update = (slotNumber: number, patch: Partial<DashboardColorSlot>) => {
    const key = String(slotNumber);
    const current = slots[key] || ({ number: slotNumber, name: `Cor ${slotNumber}`, text_hex: "#ffffff", role_hex: "#ffffff", role_id: 0, role_name: `Cor ${slotNumber}`, managed: false } as DashboardColorSlot);
    onChange(field, { ...slots, [key]: { ...current, ...patch, number: slotNumber } });
  };

  if (!visible.length) return <div className="osk-inline-note">Nenhuma opção está disponível neste painel.</div>;

  return <div className="osk-color-slot-inspector">
    {visible.map((slotNumber) => {
      const key = String(slotNumber);
      const slot = slots[key] || ({ number: slotNumber, name: `Cor ${slotNumber}`, text_hex: "#ffffff", role_hex: "#ffffff", role_id: 0, role_name: `Cor ${slotNumber}`, managed: false } as DashboardColorSlot);
      const currentRoleId = String(slot.role_id || "");
      const currentRole = roles.find((role) => role.id === currentRoleId);
      const selectOptions = currentRoleId && !availableRoles.some((role) => role.value === currentRoleId)
        ? [{ value: currentRoleId, label: `@${currentRole?.name || slot.role_name || currentRoleId}`, hint: "Cargo atual indisponível para atribuição" }, ...availableRoles]
        : availableRoles;
      const color = colorRoleHex(slot, { ok: true, channels: [], roles });
      const roleHasOwnColor = Boolean(currentRole && Number(currentRole.color || 0) > 0);
      const colorDescription = currentRole
        ? (roleHasOwnColor ? `${color} · atualizada automaticamente pelo Discord` : "Cargo sem cor própria · prévia neutra")
        : `${color} · cor do preset usada enquanto não há cargo válido`;
      const visibleIndex = visibleSlotIds?.indexOf(slotNumber) ?? -1;
      return <article key={key} data-slot-number={slotNumber}>
        <header>
          <span className="osk-color-slot-swatch" style={{ background: color }}><b>{visibleIndex >= 0 ? visibleIndex + 1 : slotNumber}</b></span>
          <div><strong>{String(slot.name || `Cor ${slotNumber}`)}</strong><small>{colorRoleLabel(slot, { ok: true, channels: [], roles })}</small></div>
        </header>
        <label><span>Nome exibido</span><input value={String(slot.name || "")} maxLength={80} onFocus={() => onSelectSlot?.(slotNumber)} onChange={(event) => update(slotNumber, { name: event.target.value })} /></label>
        <label><span>Cargo vinculado</span>{availableRoles.length || currentRoleId ? <SmartSelect id={`color-slot-${key}`} ariaLabel={`Cargo vinculado à opção ${visibleIndex >= 0 ? visibleIndex + 1 : slotNumber}`} value={currentRoleId} options={[{ value: "", label: "Nenhum cargo" }, ...selectOptions]} onChange={(next) => {
          const selected = roles.find((role) => role.id === next);
          update(slotNumber, { role_id: next, role_name: selected?.name || String(slot.name || `Cor ${slotNumber}`), managed: false });
        }} placeholder="Selecione o cargo" /> : <div className="osk-inline-note">Nenhum cargo atribuível foi encontrado.</div>}</label>
        <div className="osk-color-slot-derived"><span className="osk-color-slot-derived__dot" style={{ background: color }} /><div><strong>Cor do cargo</strong><small>{colorDescription}</small></div></div>
      </article>;
    })}
  </div>;
}
