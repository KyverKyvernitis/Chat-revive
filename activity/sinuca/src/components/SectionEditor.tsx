import {
  AlertTriangle,
  ArrowLeft,
  AudioLines,
  Bell,
  CalendarDays,
  Check,
  ChevronDown,
  FileText,
  Image,
  ListChecks,
  LockKeyhole,
  Mail,
  MessageSquare,
  Palette,
  PencilLine,
  Power,
  X,
  Route,
  Send,
  Settings,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Type,
  Users,
  Webhook,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type {
  DashboardFieldDefinition,
  DashboardMessageEditorDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardTemplateVariables,
} from "../types/dashboard";
import type { DashboardVisualModule } from "../moduleCatalog";
import { DashboardFieldControl, displayDashboardValue } from "./DashboardFieldControl";
import { MessageEditor } from "./message-editor";
import { TicketFlowEditor } from "./TicketFlowEditor";
import { TicketPermissionsEditor } from "./TicketPermissionsEditor";
import { ColorRolesPanelManager } from "./color-roles/ColorRolesPanelManager";

interface SectionEditorProps {
  section: DashboardSectionDefinition;
  module: DashboardVisualModule | null;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  previewBotName?: string;
  previewBotAvatarUrl?: string | null;
  previewGuildName?: string;
  previewGuildAvatarUrl?: string | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onMessageEditorActiveChange?(active: boolean): void;
  onBack(): void;
}

interface ActiveMessageEditor {
  id: string;
  label: string;
  description?: string;
  fields: DashboardFieldDefinition[];
  senderFieldIds?: string[];
  presentation?: DashboardMessageEditorDefinition["presentation"];
  variables?: DashboardTemplateVariables;
  baseline: Record<string, unknown>;
}

const GROUP_DESCRIPTIONS: Record<string, string> = {
  "Ativação": "Controle quando esta função pode executar novas ações.",
  "Mensagem de entrada": "Canal, formato e conteúdo enviado quando alguém entra.",
  "Mensagem privada": "Mensagem enviada diretamente ao novo membro.",
  "Aparência": "Cores, imagem e detalhes visuais compartilhados.",
  "Cargos": "Cargos entregues automaticamente na entrada.",
  "Webhook": "Nome, avatar e canal usados no envio.",
  "Canais": "Escolha onde esta função será usada.",
  "Painel": "Conteúdo e aparência da mensagem pública.",
  "Perguntas": "Campos exibidos no formulário do Discord.",
  "Resposta": "Como as respostas chegam para a equipe.",
  "Aprovação": "Ações após aprovar ou rejeitar uma resposta.",
  "Atendimento": "Categoria, equipe e destinos do atendimento.",
  "Comportamento": "Limites e regras automáticas da função.",
  "Fluxos": "Tipos de atendimento e o que cada opção faz.",
  "Textos": "Mensagens usadas durante o atendimento.",
  "Denúncias": "Categorias disponíveis para denúncias.",
  "Permissões": "Acesso da equipe, do autor e dos demais membros.",
  "Mensagens": "Respostas mostradas ao aplicar ou remover cores.",
  "Cores": "Lista visual de cores e cargos vinculados.",
  "Geral": "Preferências principais desta função.",
  "Registro de datas": "Mensagens e regras usadas no cadastro.",
  "Avisos": "Horário e textos dos anúncios automáticos.",
  "Calendário": "Conteúdo do calendário de aniversários.",
  "Voz": "Mecanismo, idioma, voz e ritmo da leitura.",
  "Prefixos": "Caracteres que iniciam cada mecanismo de voz.",
};

function groupIcon(group: string) {
  const normalized = group.toLocaleLowerCase("pt-BR");
  if (normalized.includes("ativação")) return Power;
  if (normalized.includes("webhook")) return Webhook;
  if (normalized.includes("mensagem privada")) return Mail;
  if (normalized.includes("mensagem") || normalized.includes("textos")) return MessageSquare;
  if (normalized.includes("aparência")) return Image;
  if (normalized.includes("cargo") || normalized.includes("permiss")) return ShieldCheck;
  if (normalized.includes("atendimento") || normalized.includes("canal")) return Users;
  if (normalized.includes("painel")) return FileText;
  if (normalized.includes("pergunta")) return ListChecks;
  if (normalized.includes("resposta")) return Send;
  if (normalized.includes("aprovação")) return LockKeyhole;
  if (normalized.includes("fluxo")) return Route;
  if (normalized.includes("denúncia")) return AlertTriangle;
  if (normalized.includes("cor")) return Palette;
  if (normalized.includes("registro")) return CalendarDays;
  if (normalized.includes("aviso")) return Bell;
  if (normalized.includes("calendário")) return CalendarDays;
  if (normalized.includes("voz")) return AudioLines;
  if (normalized.includes("prefix")) return Type;
  if (normalized.includes("comport")) return SlidersHorizontal;
  if (normalized.includes("geral")) return Settings2;
  return Settings;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

function editorEnabled(sectionId: string, group: string, draft: Record<string, unknown>): boolean {
  if (sectionId === "welcome" && group === "Mensagem de entrada") return Boolean(draft["welcome.enabled"]);
  if (sectionId === "welcome" && group === "Mensagem privada") return Boolean(draft["welcome.enabled"]) && Boolean(draft["welcome.dm_enabled"]);
  if (sectionId === "forms" && group === "Aprovação") return Boolean(draft["forms.approval.enabled"]);
  return true;
}

function fieldVisible(sectionId: string, field: DashboardFieldDefinition, draft: Record<string, unknown>): boolean {
  if (field.id === "tts.ignored_tts_role_enabled") return false;
  if (sectionId === "tts") {
    const engine = String(draft["tts.engine"] || "edge");
    if (field.id === "tts.language") return engine === "gtts";
    if (["tts.voice", "tts.rate", "tts.pitch"].includes(field.id)) return engine === "edge";
  }

  if (sectionId === "welcome") {
    if (field.id === "welcome.style") return String(draft["welcome.render_mode"] || "") === "components_v2";
    if (field.id === "welcome.accent_color") return String(draft["welcome.accent_color_mode"] || "fixed") === "fixed";
    if (field.id === "welcome.media_url") return String(draft["welcome.media_mode"] || "") === "custom";
    if (field.id.startsWith("welcome.webhook.") && field.id !== "welcome.webhook.enabled" && !Boolean(draft["welcome.webhook.enabled"])) return false;
    if (field.id === "welcome.webhook.name") return String(draft["welcome.webhook.name_mode"] || "") === "fixed";
    if (field.id === "welcome.webhook.avatar_url") return String(draft["welcome.webhook.avatar_mode"] || "") === "custom";
  }

  if (sectionId === "forms" && field.id.startsWith("forms.approval.") && field.id !== "forms.approval.enabled") {
    return Boolean(draft["forms.approval.enabled"]);
  }

  return true;
}

function createLegacyEditor(group: string, fields: DashboardFieldDefinition[]): DashboardMessageEditorDefinition {
  return {
    id: `legacy-${group.toLocaleLowerCase("pt-BR").replace(/[^a-z0-9]+/g, "-")}`,
    label: group,
    description: "Conteúdo e aparência desta mensagem.",
    fieldIds: fields.map((field) => field.id),
  };
}

export function SectionEditor({
  section, module, values, draft, guildOptions, previewBotName, previewBotAvatarUrl,
  previewGuildName, previewGuildAvatarUrl, onChange, onMessageEditorActiveChange, onBack,
}: SectionEditorProps) {
  const Icon = module?.icon ?? Settings;
  const groups = useMemo(() => section.groups?.length ? section.groups : null, [section.groups]);
  const [openGroup, setOpenGroup] = useState<string | null>(() => section.groups?.[0] ?? null);
  const [activeEditor, setActiveEditor] = useState<ActiveMessageEditor | null>(null);
  const groupRefs = useRef<Record<string, HTMLElement | null>>({});
  const pageScrollRef = useRef(0);

  useEffect(() => {
    setOpenGroup(section.groups?.[0] ?? null);
    setActiveEditor(null);
  }, [section.id]); // Reinicializa somente ao trocar de função.

  useEffect(() => {
    onMessageEditorActiveChange?.(Boolean(activeEditor));
  }, [activeEditor, onMessageEditorActiveChange]);

  const restorePagePosition = useCallback(() => {
    window.requestAnimationFrame(() => window.scrollTo({ top: pageScrollRef.current, behavior: "auto" }));
  }, []);

  const finishEditor = useCallback(() => {
    setActiveEditor(null);
    restorePagePosition();
  }, [restorePagePosition]);

  const openMessageEditor = useCallback((editor: DashboardMessageEditorDefinition, fallbackVariables?: DashboardTemplateVariables) => {
    const requestedFieldIds = [...editor.fieldIds, ...(editor.senderFieldIds ?? [])];
    const editorFields = requestedFieldIds
      .map((id) => section.fields.find((field) => field.id === id))
      .filter((field): field is DashboardFieldDefinition => Boolean(field));
    const colorPanelMatch = section.id === "color_roles" ? editor.id.match(/^color-panel-([1-3])$/) : null;
    if (colorPanelMatch) {
      const slotsField = section.fields.find((field) => field.id === "color_roles.slots");
      if (slotsField && !editorFields.some((field) => field.id === slotsField.id)) editorFields.push(slotsField);
    }
    if (!editorFields.length) return;
    pageScrollRef.current = window.scrollY;
    setActiveEditor({
      id: editor.id,
      label: editor.label,
      description: editor.description,
      fields: editorFields,
      senderFieldIds: editor.senderFieldIds,
      presentation: editor.presentation,
      variables: editor.variables ?? fallbackVariables,
      baseline: Object.fromEntries(editorFields.map((field) => [field.id, draft[field.id]])),
    });
  }, [draft, section.fields]);

  const closeEditorDiscard = useCallback(() => {
    if (!activeEditor) return;
    for (const field of activeEditor.fields) onChange(field, activeEditor.baseline[field.id]);
    finishEditor();
  }, [activeEditor, finishEditor, onChange]);

  function toggleGroup(group: string, scroll = false) {
    const next = openGroup === group ? null : group;
    setOpenGroup(next);
    if (next && scroll) {
      window.setTimeout(() => groupRefs.current[group]?.scrollIntoView({ behavior: "smooth", block: "start" }), 70);
    }
  }

  function renderFields(fields: DashboardFieldDefinition[]): ReactNode {
    return <FieldsPanel sectionId={section.id} fields={fields} values={values} draft={draft} guildOptions={guildOptions} onChange={onChange} />;
  }

  return <>
  <section className="osk-dashboard-page osk-section-page" aria-hidden={activeEditor ? true : undefined}>
    <button className="osk-page-back" onClick={onBack}><ArrowLeft size={16} />Funções</button>
    <header className="osk-function-heading">
      <span className="osk-function-heading-icon"><Icon size={24} /></span>
      <div><h1>{section.label}</h1><p>{module?.description || section.description}</p></div>
    </header>

    {groups && groups.length > 3 && <nav className="osk-section-jump-nav" aria-label={`Áreas de ${section.label}`}>
      {groups.map((group) => <button key={group} type="button" data-active={openGroup === group || undefined} onClick={() => toggleGroup(group, true)}>{group}</button>)}
    </nav>}

    {groups ? (
      <div className="osk-accordion-list">
        {groups.map((group, index) => {
          const GroupIcon = groupIcon(group);
          const groupFields = section.fields.filter((field) => field.group === group);
          const changed = groupFields.filter((field) => !valuesEqual(values[field.id], draft[field.id])).length;
          const open = openGroup === group;
          const metadata = section.groupMetadata?.[group];
          const panelId = `section-panel-${section.id}-${index}`;
          return <article
            key={group}
            id={`section-group-${section.id}-${index}`}
            ref={(node) => { groupRefs.current[group] = node; }}
            className="osk-accordion"
            data-open={open || undefined}
            style={{ "--osk-card-index": index } as CSSProperties}
          >
            <button type="button" className="osk-accordion-trigger" onClick={() => toggleGroup(group)} aria-expanded={open} aria-controls={panelId}>
              <span className="osk-accordion-icon"><GroupIcon size={19} /></span>
              <span className="osk-accordion-copy"><strong>{group}</strong><small>{GROUP_DESCRIPTIONS[group] || "Ajustes desta função."}</small></span>
              {changed > 0 && <em>Alterado</em>}
              <ChevronDown size={18} className="osk-accordion-chevron" />
            </button>
            {open && <div className="osk-accordion-panel" id={panelId}>
              <div className="osk-accordion-panel-inner">
                {section.id === "color_roles" && group === "Painel" ? (
                  <ColorRolesPanelManager
                    fields={groupFields}
                    values={values}
                    draft={draft}
                    guildOptions={guildOptions}
                    onChange={onChange}
                    onOpenEditor={(editor) => openMessageEditor(editor)}
                  />
                ) : section.id === "tickets" && group === "Atendimento" ? (
                  <TicketAttendancePanel fields={groupFields} renderFields={renderFields} />
                ) : section.id === "tickets" && group === "Fluxos" ? (
                  <TicketFlowEditor fields={groupFields} draft={draft} renderFields={renderFields} onChange={onChange} />
                ) : section.id === "tickets" && group === "Permissões" ? (
                  <TicketPermissionsEditor fields={groupFields} draft={draft} renderFields={renderFields} onChange={onChange} />
                ) : metadata?.kind === "message" ? (
                  <MessageGroupPanel
                    sectionId={section.id}
                    group={group}
                    fields={groupFields}
                    metadata={metadata}
                    values={values}
                    draft={draft}
                    guildOptions={guildOptions}
                    renderFields={renderFields}
                    onOpenEditor={openMessageEditor}
                  />
                ) : (
                  renderFields(groupFields)
                )}
              </div>
            </div>}
          </article>;
        })}
      </div>
    ) : (
      <div className="osk-settings-panel">{renderFields(section.fields)}</div>
    )}
  </section>
  {activeEditor && <MessageEditor
    editorId={activeEditor.id}
    sectionId={section.id}
    sectionLabel={section.label}
    groupLabel={activeEditor.label}
    description={activeEditor.description}
    fields={activeEditor.fields}
    baseline={activeEditor.baseline}
    draft={draft}
    guildOptions={guildOptions}
    botName={previewBotName}
    botAvatarUrl={previewBotAvatarUrl}
    guildName={previewGuildName}
    guildAvatarUrl={previewGuildAvatarUrl}
    senderFieldIds={activeEditor.senderFieldIds}
    presentation={activeEditor.presentation}
    variables={activeEditor.variables}
    onChange={onChange}
    onApply={finishEditor}
    onDiscard={closeEditorDiscard}
  />}
  </>;
}

function MessageGroupPanel({
  sectionId, group, fields, metadata, values, draft, guildOptions, renderFields, onOpenEditor,
}: {
  sectionId: string;
  group: string;
  fields: DashboardFieldDefinition[];
  metadata: NonNullable<DashboardSectionDefinition["groupMetadata"]>[string];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
  onOpenEditor(editor: DashboardMessageEditorDefinition, fallbackVariables?: DashboardTemplateVariables): void;
}) {
  const editors = metadata.editors?.length ? metadata.editors : [createLegacyEditor(group, fields)];
  const editorFieldIds = new Set(editors.flatMap((editor) => [...editor.fieldIds, ...(editor.senderFieldIds ?? [])]));
  const settingsIds = new Set(metadata.settingsFieldIds ?? fields.filter((field) => !editorFieldIds.has(field.id)).map((field) => field.id));
  const settingsFields = fields.filter((field) => settingsIds.has(field.id));
  const enabled = editorEnabled(sectionId, group, draft);

  return <div className="osk-message-group-panel">
    {settingsFields.length > 0 && <div className="osk-message-group-settings">
      {sectionId === "forms" && group === "Aprovação"
        ? <FormsApprovalSettings fields={settingsFields} draft={draft} renderFields={renderFields} />
        : renderFields(settingsFields)}
    </div>}
    {!enabled && <div className="osk-inline-note">Ative esta opção para usar as mensagens abaixo. O conteúdo atual continuará preservado.</div>}
    <div className="osk-message-launcher-list">
      {editors.map((editor) => {
        const editorFields = [...editor.fieldIds, ...(editor.senderFieldIds ?? [])].map((id) => fields.find((field) => field.id === id)).filter((field): field is DashboardFieldDefinition => Boolean(field));
        const changed = editorFields.some((field) => !valuesEqual(values[field.id], draft[field.id]));
        const summaryFields = editorFields.filter((field) => ["text", "textarea", "select"].includes(field.type)).slice(0, 2);
        const summary = summaryFields.map((field) => `${field.label}: ${displayDashboardValue(field, draft[field.id], guildOptions)}`).join(" · ");
        return <article key={editor.id} className="osk-message-launcher" data-changed={changed || undefined} data-disabled={!enabled || undefined}>
          <span className="osk-message-launcher__icon"><PencilLine size={19} /></span>
          <div><strong>{editor.label}</strong><small>{editor.description || "Edite a mensagem diretamente e aplique ao painel."}</small>{summary && <em>{summary}</em>}</div>
          <button type="button" className="osk-message-launcher__action" disabled={!enabled} onClick={() => onOpenEditor(editor, metadata.variables)}>Editar</button>
        </article>;
      })}
    </div>
  </div>;
}

function TicketAttendancePanel({ fields, renderFields }: {
  fields: DashboardFieldDefinition[];
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
}) {
  const organization = fields.filter((field) => field.id.includes(".channels."));
  const generalTeam = fields.filter((field) => field.id === "tickets.roles.staff_role_id");
  const specializedTeams = fields.filter((field) => field.id.startsWith("tickets.roles.") && field.id !== "tickets.roles.staff_role_id");
  return <div className="osk-settings-groups">
    {organization.length > 0 && <section><header><Users size={17} /><span><strong>Organização</strong><small>Categoria e canais usados pelo atendimento.</small></span></header>{renderFields(organization)}</section>}
    {generalTeam.length > 0 && <section><header><ShieldCheck size={17} /><span><strong>Equipe geral</strong><small>Cargo usado quando um fluxo não possui equipe específica.</small></span></header>{renderFields(generalTeam)}</section>}
    {specializedTeams.length > 0 && <section><header><Users size={17} /><span><strong>Equipes específicas</strong><small>Deixe como Nenhum para usar a equipe geral.</small></span></header>{renderFields(specializedTeams)}</section>}
  </div>;
}

function FormsApprovalSettings({ fields, draft, renderFields }: {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  renderFields(fields: DashboardFieldDefinition[]): ReactNode;
}) {
  const enabledField = fields.find((field) => field.id === "forms.approval.enabled");
  const roleField = fields.find((field) => field.id === "forms.approval.role_id");
  const approveFields = fields.filter((field) => /forms\.approval\.approve_(label|emoji|style)$/.test(field.id));
  const rejectFields = fields.filter((field) => /forms\.approval\.reject_(label|emoji|style)$/.test(field.id));
  const enabled = Boolean(draft["forms.approval.enabled"]);
  const buttonPreview = (kind: "approve" | "reject") => {
    const label = String(draft[`forms.approval.${kind}_label`] || (kind === "approve" ? "Aprovar" : "Rejeitar"));
    const emoji = String(draft[`forms.approval.${kind}_emoji`] || "");
    const style = String(draft[`forms.approval.${kind}_style`] || (kind === "approve" ? "success" : "danger"));
    return <span className="osk-action-button-preview" data-style={style}>{emoji && <b>{emoji}</b>}{label}</span>;
  };
  return <div className="osk-approval-settings">
    {enabledField && renderFields([enabledField])}
    {!enabled && <div className="osk-inline-note">Ative a aprovação para configurar o cargo, os botões e as mensagens enviadas ao membro.</div>}
    {enabled && <>
      {roleField && <section className="osk-approval-settings__result"><header><LockKeyhole size={17} /><span><strong>Resultado da aprovação</strong><small>Cargo concedido quando a equipe aprova a resposta.</small></span></header>{renderFields([roleField])}</section>}
      <div className="osk-approval-settings__buttons">
        <section><header><span><Check size={17} /><strong>Botão Aprovar</strong></span>{buttonPreview("approve")}</header>{renderFields(approveFields)}</section>
        <section><header><span><X size={17} /><strong>Botão Rejeitar</strong></span>{buttonPreview("reject")}</header>{renderFields(rejectFields)}</section>
      </div>
    </>}
  </div>;
}

function FieldsPanel({
  sectionId, fields, values, draft, guildOptions, onChange,
}: {
  sectionId: string;
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}) {
  const ignoredEnabledField = fields.find((field) => field.id === "tts.ignored_tts_role_enabled");
  const visibleFields = fields.filter((field) => fieldVisible(sectionId, field, draft));

  if (visibleFields.length === 0) return <div className="osk-inline-note">Nenhuma opção adicional é necessária para a configuração atual.</div>;

  return <div className="osk-compact-fields">
    {visibleFields.map((field) => {
      const isIgnoredRole = field.id === "tts.ignored_tts_role_id" && Boolean(ignoredEnabledField);
      const changed = !valuesEqual(draft[field.id], values[field.id])
        || (isIgnoredRole && !valuesEqual(draft[ignoredEnabledField!.id], values[ignoredEnabledField!.id]));
      const isComplex = ["textarea", "role_multi", "string_list", "form_fields", "color_slots"].includes(field.type);
      const displayField = isIgnoredRole
        ? { ...field, label: "Ignorar mensagens deste cargo", description: "Selecione Nenhum para desativar esta regra." }
        : field;
      const controlValue = isIgnoredRole && !Boolean(draft[ignoredEnabledField!.id]) ? "" : draft[field.id];
      const handleChange = (changedField: DashboardFieldDefinition, raw: unknown) => {
        onChange(changedField, raw);
        if (changedField.id === "tts.ignored_tts_role_id" && ignoredEnabledField) onChange(ignoredEnabledField, Boolean(String(raw || "").trim()));
      };

      return <div key={field.id} className="osk-compact-field" data-type={field.type} data-changed={changed || undefined} data-complex={isComplex || undefined}>
        <div className="osk-compact-field-copy">
          <strong>{displayField.label}</strong>
          {displayField.description && <small>{displayField.description}</small>}
        </div>
        <div className="osk-compact-field-control"><DashboardFieldControl field={displayField} value={controlValue} guildOptions={guildOptions} onChange={handleChange} /></div>
      </div>;
    })}
  </div>;
}
