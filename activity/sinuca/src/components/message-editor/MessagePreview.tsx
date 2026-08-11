import { ArrowDown, ArrowUp, Boxes, Minus, Pencil, Plus, Settings2 } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import type {
  DashboardColorSlot,
  DashboardFieldDefinition,
  DashboardMessageEditorPresentation,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { SmartAvatar } from "../SmartAvatar";
import {
  COLOR_PANEL_OPTION_MAX,
  colorPanelFromEditorId,
  colorRoleHex,
  nextUnusedColorSlot,
  normalizeColorPanelLayout,
  updatePanelSlots,
} from "../color-roles/colorRolesModel";
import { DiscordRichText } from "./DiscordRichText";
import { MessageInlineTextEditor } from "./MessageInlineTextEditor";
import { isValidPreviewUrl, normalizePreviewUrl, previewImageCandidates, readableFieldLabel } from "./messageEditorUtils";

interface MessagePreviewProps {
  sectionId?: string;
  editorId?: string;
  groupLabel: string;
  presentation?: DashboardMessageEditorPresentation;
  fields: DashboardFieldDefinition[];
  senderFields?: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  botName?: string;
  botAvatarUrl?: string | null;
  guildName?: string;
  guildAvatarUrl?: string | null;
  interactive?: boolean;
  senderSelected?: boolean;
  selectedFieldId?: string | null;
  editingFieldId?: string | null;
  selectedColorSlot?: number | null;
  textSelection?: { fieldId: string; start: number; end: number } | null;
  onSelectSender?(): void;
  onEditSender?(): void;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  hasFieldOptions?(field: DashboardFieldDefinition): boolean;
  onOpenFieldOptions?(field: DashboardFieldDefinition): void;
  onFinishEdit?(): void;
  onChange?(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  onSelectColorSlot?(slotNumber: number, openInspector?: boolean): void;
}

type PreviewCoreProps = Omit<
  MessagePreviewProps,
  | "groupLabel"
  | "botName"
  | "botAvatarUrl"
  | "guildName"
  | "guildAvatarUrl"
  | "senderFields"
  | "senderSelected"
  | "onSelectSender"
  | "onEditSender"
  | "presentation"
>;

function findField(fields: DashboardFieldDefinition[], suffixes: string[]): DashboardFieldDefinition | undefined {
  return fields.find((item) => suffixes.some((suffix) => item.id.endsWith(suffix)));
}

function findExact(fields: DashboardFieldDefinition[], ...ids: string[]): DashboardFieldDefinition | undefined {
  return fields.find((field) => ids.includes(field.id));
}

function fieldValue(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): unknown {
  return field ? draft[field.id] : undefined;
}

function fieldString(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): string {
  const value = fieldValue(field, draft);
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

function normalizedColor(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  const normalized = trimmed.startsWith("#") ? trimmed : `#${trimmed}`;
  return /^#[0-9a-f]{6}$/i.test(normalized) ? normalized : null;
}

function previewColor(fields: DashboardFieldDefinition[], draft: Record<string, unknown>): string | null {
  const colorField = fields.find((field) => field.type === "color");
  return normalizedColor(colorField ? draft[colorField.id] : null);
}

function needsLightOutline(hex: string): boolean {
  const match = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!match) return false;
  const value = Number.parseInt(match[1], 16);
  const red = (value >> 16) & 0xff;
  const green = (value >> 8) & 0xff;
  const blue = value & 0xff;
  return (red * 0.299 + green * 0.587 + blue * 0.114) < 58;
}

function optionLabel(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): string | null {
  if (!field) return null;
  const value = fieldString(field, draft);
  if (!value || value === "none") return null;
  return field.options?.find((option) => option.value === value)?.label ?? readableFieldLabel(field);
}

function EditableRegion({
  field,
  interactive,
  selectedFieldId,
  onSelectField,
  onEditField,
  hasOptions = false,
  onOpenOptions,
  className,
  children,
  placeholder,
  textEditable = false,
}: {
  field?: DashboardFieldDefinition;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  hasOptions?: boolean;
  onOpenOptions?(field: DashboardFieldDefinition): void;
  className?: string;
  children?: ReactNode;
  placeholder?: string;
  textEditable?: boolean;
}) {
  if (!children && !interactive) return null;
  if (!interactive || !field || !onSelectField) return <div className={className}>{children}</div>;

  const selected = selectedFieldId === field.id;
  const selectOrEdit = () => {
    onSelectField(field);
    if (textEditable && onEditField) onEditField(field);
  };

  return (
    <div
      role="button"
      tabIndex={0}
      className={className ? `osk-message-editable ${className}` : "osk-message-editable"}
      data-selected={selected || undefined}
      data-text-editable={textEditable || undefined}
      data-message-field-anchor={field.id}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        selectOrEdit();
      }}
      onDoubleClick={(event) => {
        if (!textEditable || !onEditField) return;
        event.preventDefault();
        event.stopPropagation();
        onSelectField(field);
        onEditField(field);
      }}
      onKeyDown={(event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        selectOrEdit();
      }}
      title={textEditable ? `Editar ${field.label}` : `Configurar ${field.label}`}
    >
      {children ?? <span className="osk-message-preview__ghost">+ {placeholder ?? readableFieldLabel(field)}</span>}
      {selected && textEditable && <span className="osk-message-editable__pencil" aria-hidden="true"><Pencil size={11} /></span>}
      {selected && hasOptions && onOpenOptions && (
        <span
          role="button"
          tabIndex={0}
          className="osk-message-editable__options"
          data-with-pencil={textEditable || undefined}
          aria-label={`Abrir opções de ${field.label}`}
          title={`Opções de ${field.label}`}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onOpenOptions(field);
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            event.stopPropagation();
            onOpenOptions(field);
          }}
        >
          <Settings2 size={11} />
        </span>
      )}
    </div>
  );
}

function FieldText({
  field,
  draft,
  guildOptions,
  interactive,
  selectedFieldId,
  editingFieldId,
  textSelection,
  onSelectField,
  onEditField,
  hasFieldOptions,
  onOpenFieldOptions,
  onFinishEdit,
  onChange,
  onTextSelection,
  className,
  placeholder,
}: {
  field?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  guildOptions?: DashboardOptionsPayload | null;
  interactive?: boolean;
  selectedFieldId?: string | null;
  editingFieldId?: string | null;
  textSelection?: { fieldId: string; start: number; end: number } | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  onEditField?(field: DashboardFieldDefinition): void;
  hasFieldOptions?(field: DashboardFieldDefinition): boolean;
  onOpenFieldOptions?(field: DashboardFieldDefinition): void;
  onFinishEdit?(): void;
  onChange?(field: DashboardFieldDefinition, raw: unknown): void;
  onTextSelection?(field: DashboardFieldDefinition, start: number, end: number): void;
  className?: string;
  placeholder?: string;
}) {
  const value = fieldString(field, draft);
  const editing = Boolean(field && editingFieldId === field.id && onChange && onTextSelection && onFinishEdit);

  if (editing && field) {
    return (
      <div className={className ? `osk-message-editable osk-message-editable--editing ${className}` : "osk-message-editable osk-message-editable--editing"} data-selected="true" data-message-field-anchor={field.id}>
        <MessageInlineTextEditor
          field={field}
          value={value}
          selection={textSelection?.fieldId === field.id ? textSelection : null}
          onChange={(next) => onChange!(field, next)}
          onSelection={(start, end) => onTextSelection!(field, start, end)}
          onFinish={onFinishEdit!}
        />
      </div>
    );
  }

  return (
    <EditableRegion
      field={field}
      interactive={interactive}
      selectedFieldId={selectedFieldId}
      onSelectField={onSelectField}
      onEditField={onEditField}
      hasOptions={Boolean(field && hasFieldOptions?.(field))}
      onOpenOptions={onOpenFieldOptions}
      className={className}
      placeholder={placeholder}
      textEditable={Boolean(field && (field.type === "text" || field.type === "textarea"))}
    >
      {value.trim() ? <DiscordRichText text={value} guildOptions={guildOptions} /> : undefined}
    </EditableRegion>
  );
}

function MessageImage({ src, alt, className, placeholder }: { src: string; alt: string; className: string; placeholder: string }) {
  const normalizedSrc = normalizePreviewUrl(src);
  const candidates = useMemo(() => previewImageCandidates(normalizedSrc), [normalizedSrc]);
  const [candidateIndex, setCandidateIndex] = useState(0);
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    setCandidateIndex(0);
    setFailed(false);
    setLoaded(false);
  }, [normalizedSrc]);

  const currentSrc = candidates[candidateIndex] || "";
  if (failed || !currentSrc) {
    return <div className={`${className} osk-message-preview__image-placeholder`} data-error={failed || undefined}>{failed ? "Imagem indisponível" : placeholder}</div>;
  }

  const handleError = () => {
    if (candidateIndex + 1 < candidates.length) {
      setCandidateIndex((current) => current + 1);
      setLoaded(false);
      return;
    }
    setFailed(true);
  };

  return <span className={`osk-message-image-loader ${className}-loader`} data-loaded={loaded || undefined}>
    {!loaded && <span className={`${className} osk-message-preview__image-placeholder`}>Carregando imagem…</span>}
    <img
      key={currentSrc}
      className={className}
      src={currentSrc}
      alt={alt}
      loading="eager"
      decoding="async"
      referrerPolicy="no-referrer"
      onLoad={() => setLoaded(true)}
      onError={handleError}
    />
  </span>;
}

function ImageSlot({
  urlField,
  modeField,
  draft,
  interactive,
  selectedFieldId,
  onSelectField,
  className,
  alt,
  fallbackLabel,
}: {
  urlField?: DashboardFieldDefinition;
  modeField?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  className: string;
  alt: string;
  fallbackLabel: string;
}) {
  const url = fieldString(urlField, draft);
  const modeValue = fieldString(modeField, draft);
  const targetField = modeValue === "custom" && urlField ? urlField : modeField ?? urlField;
  const label = optionLabel(modeField, draft) ?? fallbackLabel;
  const shouldRender = isValidPreviewUrl(url) || interactive || Boolean(optionLabel(modeField, draft));
  if (!shouldRender) return null;
  return (
    <EditableRegion field={targetField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className={`${className}-wrap`} placeholder={label}>
      {isValidPreviewUrl(url)
        ? <MessageImage src={url} alt={alt} className={className} placeholder={label} />
        : <span className="osk-message-preview__image-placeholder">{label}</span>}
    </EditableRegion>
  );
}

function IconSlot({
  urlField,
  modeField,
  draft,
  interactive,
  selectedFieldId,
  onSelectField,
  alt,
  fallbackLabel,
}: {
  urlField?: DashboardFieldDefinition;
  modeField?: DashboardFieldDefinition;
  draft: Record<string, unknown>;
  interactive?: boolean;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  alt: string;
  fallbackLabel: string;
}) {
  const url = fieldString(urlField, draft);
  const modeValue = fieldString(modeField, draft);
  const targetField = modeValue === "custom" && urlField ? urlField : modeField ?? urlField;
  const label = optionLabel(modeField, draft) ?? fallbackLabel;
  const shouldRender = isValidPreviewUrl(url) || Boolean(optionLabel(modeField, draft));
  if (!shouldRender) return null;
  return (
    <EditableRegion field={targetField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__icon-wrap" placeholder={label}>
      {isValidPreviewUrl(url)
        ? <MessageImage src={url} alt={alt} className="osk-message-preview__icon" placeholder={label} />
        : <span className="osk-message-preview__icon-placeholder" title={label}>{label.slice(0, 1).toUpperCase()}</span>}
    </EditableRegion>
  );
}

function AccentControl({ field, selectedFieldId, onSelectField, label = "Editar cor de destaque" }: {
  field?: DashboardFieldDefinition;
  selectedFieldId?: string | null;
  onSelectField?(field: DashboardFieldDefinition): void;
  label?: string;
}) {
  if (!field || !onSelectField) return null;
  return (
    <button
      type="button"
      className="osk-message-preview__accent-control"
      data-selected={selectedFieldId === field.id || undefined}
      data-message-field-anchor={field.id}
      aria-label={label}
      onClick={(event) => { event.stopPropagation(); onSelectField(field); }}
    />
  );
}

function EmbedPreview(props: PreviewCoreProps) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const contentField = findField(fields, [".embed.content"]);
  const authorField = findField(fields, [".embed.author_name"]);
  const authorIconUrlField = findField(fields, [".embed.author_icon_url"]);
  const authorIconModeField = findField(fields, [".embed.author_icon_mode"]);
  const titleField = findField(fields, [".embed.title"]);
  const descriptionField = findField(fields, [".embed.description"]);
  const footerField = findField(fields, [".embed.footer_text"]);
  const footerIconUrlField = findField(fields, [".embed.footer_icon_url"]);
  const footerIconModeField = findField(fields, [".embed.footer_icon_mode"]);
  const imageUrlField = findField(fields, [".embed.image_url"]);
  const imageModeField = findField(fields, [".embed.image_mode"]);
  const thumbnailUrlField = findField(fields, [".embed.thumbnail_url"]);
  const thumbnailModeField = findField(fields, [".embed.thumbnail_mode"]);
  const colorField = findField(fields, [".embed.color"]);
  const colorModeField = findField(fields, [".embed.color_mode"]);
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;
  const colorTarget = fieldString(colorModeField, draft) === "fixed" && colorField ? colorField : colorModeField ?? colorField;
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };

  return (
    <div className="osk-message-preview__message">
      <FieldText {...textProps} field={contentField} className="osk-message-preview__content" placeholder="Adicionar conteúdo" />
      <div className="osk-message-preview__embed" style={style}>
        {interactive && <AccentControl field={colorTarget} selectedFieldId={selectedFieldId} onSelectField={onSelectField} label="Editar cor do embed" />}
        <div className="osk-message-preview__embed-main">
          <div className="osk-message-preview__author-row">
            <IconSlot urlField={authorIconUrlField} modeField={authorIconModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} alt="Ícone do autor" fallbackLabel="Ícone do autor" />
            <FieldText {...textProps} field={authorField} className="osk-message-preview__author" placeholder="Adicionar autor" />
          </div>
          <FieldText {...textProps} field={titleField} className="osk-message-preview__title" placeholder="Adicionar título" />
          <FieldText {...textProps} field={descriptionField} className="osk-message-preview__description" placeholder="Adicionar descrição" />
          <ImageSlot urlField={imageUrlField} modeField={imageModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__image" alt="Imagem da mensagem" fallbackLabel="Adicionar imagem" />
          <div className="osk-message-preview__footer-row">
            <IconSlot urlField={footerIconUrlField} modeField={footerIconModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} alt="Ícone do rodapé" fallbackLabel="Ícone do rodapé" />
            <FieldText {...textProps} field={footerField} className="osk-message-preview__footer" placeholder="Adicionar rodapé" />
          </div>
        </div>
        <ImageSlot urlField={thumbnailUrlField} modeField={thumbnailModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__thumbnail" alt="Thumbnail da mensagem" fallbackLabel="Adicionar thumbnail" />
      </div>
    </div>
  );
}


function WelcomeDmEmbedPreview(props: PreviewCoreProps) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const titleField = findExact(fields, "welcome.dm.title");
  const bodyField = findExact(fields, "welcome.dm.body");
  const footerField = findExact(fields, "welcome.dm.footer");
  const accent = normalizedColor(draft["welcome.accent_color"]) ?? "#5865F2";
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };
  return (
    <div className="osk-message-preview__message">
      <div className="osk-message-preview__embed" style={{ "--osk-message-accent": accent } as CSSProperties}>
        <div className="osk-message-preview__embed-main">
          <FieldText {...textProps} field={titleField} className="osk-message-preview__title" placeholder="Adicionar título" />
          <FieldText {...textProps} field={bodyField} className="osk-message-preview__description" placeholder="Adicionar mensagem" />
          <div className="osk-message-preview__footer-row">
            <FieldText {...textProps} field={footerField} className="osk-message-preview__footer" placeholder="Adicionar rodapé" />
          </div>
        </div>
      </div>
    </div>
  );
}

function V2BlockLabel({ children }: { children: ReactNode }) {
  return <span className="osk-v2-component-label"><Boxes size={11} />{children}</span>;
}

function WelcomeComponentsV2Preview(props: PreviewCoreProps & { dm?: boolean }) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection, dm } = props;
  const titleField = dm ? findExact(fields, "welcome.dm.title") : findExact(fields, "welcome.public.title");
  const bodyField = dm ? findExact(fields, "welcome.dm.body") : findExact(fields, "welcome.public.body");
  const footerField = dm ? findExact(fields, "welcome.dm.footer") : findExact(fields, "welcome.public.footer");
  const styleField = findExact(fields, "welcome.style");
  const accentModeField = findExact(fields, "welcome.accent_color_mode");
  const accentField = findExact(fields, "welcome.accent_color");
  const mediaModeField = findExact(fields, "welcome.media_mode");
  const mediaUrlField = findExact(fields, "welcome.media_url");
  const styleValue = fieldString(styleField, draft) || String(draft["welcome.style"] || "complete");
  const accent = normalizedColor(fieldValue(accentField, draft) ?? draft["welcome.accent_color"]) ?? "#5865F2";
  const accentTarget = String(draft["welcome.accent_color_mode"] || fieldString(accentModeField, draft) || "fixed") === "fixed" && accentField ? accentField : accentModeField ?? accentField;
  const showMedia = !dm && styleValue === "complete";
  const showFooter = styleValue !== "compact";
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };

  return (
    <div className="osk-v2-message">
      <div className="osk-v2-container" style={{ "--osk-message-accent": accent } as CSSProperties}>
        {interactive && <AccentControl field={accentTarget} selectedFieldId={selectedFieldId} onSelectField={onSelectField} />}
        <div className="osk-v2-container__toolbar">
          <EditableRegion field={styleField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-structure-chip" placeholder="Estilo do container">
            <V2BlockLabel>Container · {optionLabel(styleField, draft) ?? "Completo"}</V2BlockLabel>
          </EditableRegion>
        </div>
        <div className="osk-v2-text-display osk-v2-text-display--title">
          <V2BlockLabel>Texto</V2BlockLabel>
          <FieldText {...textProps} field={titleField} className="osk-v2-text" placeholder="Adicionar título" />
        </div>
        <div className="osk-v2-text-display">
          <V2BlockLabel>Texto</V2BlockLabel>
          <FieldText {...textProps} field={bodyField} className="osk-v2-text" placeholder="Adicionar mensagem" />
        </div>
        {showMedia && (interactive || fieldString(mediaUrlField, draft) || optionLabel(mediaModeField, draft)) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-media-gallery">
              <V2BlockLabel>Galeria de mídia</V2BlockLabel>
              <ImageSlot urlField={mediaUrlField} modeField={mediaModeField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-media" alt="Imagem da mensagem" fallbackLabel="Adicionar imagem" />
            </div>
          </>
        )}
        {showFooter && (interactive || fieldString(footerField, draft).trim()) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-text-display osk-v2-text-display--footer">
              <V2BlockLabel>Texto</V2BlockLabel>
              <FieldText {...textProps} field={footerField} className="osk-v2-text" placeholder="Adicionar rodapé" />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function ComponentsV2Preview(props: PreviewCoreProps) {
  const { editorId, fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const titleField = fields.find((field) => /(?:^|\.)(title)$/.test(field.id));
  const footerField = fields.find((field) => /(?:^|\.)(footer|footer_text)$/.test(field.id));
  const bodyFields = fields.filter((field) => (field.type === "text" || field.type === "textarea") && field !== titleField && field !== footerField && !/(button|emoji|placeholder)/i.test(`${field.id} ${field.label}`));
  const colorField = fields.find((field) => field.type === "color");
  const mediaField = fields.find((field) => field.type === "url" && /(media|image)(?:_url)?$/i.test(field.id) && !/side/i.test(field.id));
  const sideMediaField = fields.find((field) => field.type === "url" && /side.*image|image.*side/i.test(field.id));
  const buttonLabelField = fields.find((field) => /button_label$|approve_label$|reject_label$/i.test(field.id));
  const buttonEmojiField = fields.find((field) => /button_emoji$|approve_emoji$|reject_emoji$/i.test(field.id));
  const buttonStyleField = fields.find((field) => /button_style$|approve_style$|reject_style$/i.test(field.id));
  const placeholderField = fields.find((field) => /placeholder$/i.test(field.id));
  const isApproveDm = editorId === "forms-approve-dm";
  const isRejectDm = editorId === "forms-reject-dm";
  const accent = normalizedColor(fieldValue(colorField, draft)) ?? (isApproveDm ? "#248046" : isRejectDm ? "#DA373C" : "#5865F2");
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };
  const isTicketPanel = editorId === "tickets-panel";
  const isFormsResponse = editorId === "forms-response";
  const hasAction = Boolean(buttonLabelField || placeholderField);

  return (
    <div className="osk-v2-message">
      <div className="osk-v2-container" style={{ "--osk-message-accent": accent } as CSSProperties}>
        {interactive && <AccentControl field={colorField} selectedFieldId={selectedFieldId} onSelectField={onSelectField} />}
        <div className="osk-v2-container__toolbar"><V2BlockLabel>Container</V2BlockLabel></div>
        {(isApproveDm || isRejectDm) && (
          <div className="osk-v2-text-display osk-v2-text-display--title osk-v2-text-display--runtime">
            <V2BlockLabel>Texto fixo</V2BlockLabel>
            <div className="osk-v2-text">{isApproveDm ? "✅ Verificação aprovada" : "❌ Verificação rejeitada"}</div>
          </div>
        )}
        {isTicketPanel && sideMediaField ? (
          <div className="osk-v2-section">
            <div className="osk-v2-section__copy">
              <V2BlockLabel>Seção</V2BlockLabel>
              <FieldText {...textProps} field={titleField} className="osk-v2-text osk-v2-text--title" placeholder="Adicionar título" />
              {bodyFields.map((field) => <FieldText key={field.id} {...textProps} field={field} className="osk-v2-text" placeholder="Adicionar texto" />)}
            </div>
            <div className="osk-v2-section__accessory">
              <V2BlockLabel>Thumbnail</V2BlockLabel>
              <ImageSlot urlField={sideMediaField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-thumbnail" alt="Imagem lateral" fallbackLabel="Adicionar imagem lateral" />
            </div>
          </div>
        ) : (
          <>
            {titleField && <div className="osk-v2-text-display osk-v2-text-display--title"><V2BlockLabel>Texto</V2BlockLabel><FieldText {...textProps} field={titleField} className="osk-v2-text" placeholder="Adicionar título" /></div>}
            {bodyFields.map((field) => <div className="osk-v2-text-display" key={field.id}><V2BlockLabel>Texto</V2BlockLabel><FieldText {...textProps} field={field} className="osk-v2-text" placeholder="Adicionar texto" /></div>)}
          </>
        )}

        {isFormsResponse && (
          <div className="osk-v2-runtime-block" aria-label="Campos preenchidos em tempo de execução">
            <V2BlockLabel>Campos do formulário</V2BlockLabel>
            <strong>Nome</strong><span>Resposta do membro</span>
            <strong>Descrição</strong><span>Conteúdo enviado no formulário</span>
          </div>
        )}

        {mediaField && (interactive || isValidPreviewUrl(fieldString(mediaField, draft))) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-media-gallery"><V2BlockLabel>Galeria de mídia</V2BlockLabel><ImageSlot urlField={mediaField} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-v2-media" alt="Imagem da mensagem" fallbackLabel="Adicionar imagem" /></div>
          </>
        )}

        {footerField && (interactive || fieldString(footerField, draft).trim()) && (
          <><div className="osk-v2-separator" aria-hidden="true" /><div className="osk-v2-text-display osk-v2-text-display--footer"><V2BlockLabel>Texto</V2BlockLabel><FieldText {...textProps} field={footerField} className="osk-v2-text" placeholder="Adicionar rodapé" /></div></>
        )}

        {isFormsResponse && Boolean(draft["forms.approval.enabled"]) && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-action-row osk-v2-action-row--runtime">
              <V2BlockLabel>Linha de ações</V2BlockLabel>
              <div className="osk-message-preview__button-row">
                <span data-style={String(draft["forms.approval.approve_style"] || "success")}>{String(draft["forms.approval.approve_emoji"] || "✅")} {String(draft["forms.approval.approve_label"] || "Aprovar")}</span>
                <span data-style={String(draft["forms.approval.reject_style"] || "danger")}>{String(draft["forms.approval.reject_emoji"] || "❌")} {String(draft["forms.approval.reject_label"] || "Rejeitar")}</span>
              </div>
            </div>
          </>
        )}

        {hasAction && (
          <>
            <div className="osk-v2-separator" aria-hidden="true" />
            <div className="osk-v2-action-row">
              <V2BlockLabel>Linha de ações</V2BlockLabel>
              {placeholderField ? (
                <EditableRegion field={placeholderField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__component-wrap" placeholder="Menu de seleção">
                  <div className="osk-message-preview__select-sim"><span>{fieldString(placeholderField, draft) || "Escolha uma opção"}</span><span>⌄</span></div>
                </EditableRegion>
              ) : (
                <EditableRegion field={buttonLabelField ?? buttonStyleField} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__component-wrap" placeholder="Botão">
                  <div className="osk-message-preview__button-row"><span data-style={fieldString(buttonStyleField, draft) || "primary"}>{fieldString(buttonEmojiField, draft) && <><DiscordRichText text={fieldString(buttonEmojiField, draft)} guildOptions={guildOptions} compact /> </>}<DiscordRichText text={fieldString(buttonLabelField, draft) || "Continuar"} guildOptions={guildOptions} compact /></span></div>
                </EditableRegion>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function GenericMessagePreview(props: PreviewCoreProps) {
  const { fields, draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection } = props;
  const textFields = fields.filter((field) => field.type === "text" || field.type === "textarea");
  const imageFields = fields.filter((field) => field.type === "url" && /(image|media|banner|avatar)/i.test(`${field.id} ${field.label}`));
  const colorField = fields.find((field) => field.type === "color");
  const titleField = textFields.find((field) => /(?:^|\.)(title)$/.test(field.id) || /título/i.test(field.label));
  const footerField = textFields.find((field) => /(?:^|\.)(footer|footer_text)$/.test(field.id) || /rodapé/i.test(field.label));
  const bodyFields = textFields.filter((field) => field !== titleField && field !== footerField && !/(emoji|button|placeholder)/i.test(`${field.id} ${field.label}`));
  const accent = previewColor(fields, draft);
  const style = accent ? ({ "--osk-message-accent": accent } as CSSProperties) : undefined;
  const textProps = { draft, guildOptions, interactive, selectedFieldId, editingFieldId, textSelection, onSelectField, onEditField, hasFieldOptions, onOpenFieldOptions, onFinishEdit, onChange, onTextSelection };

  if (!textFields.length && !imageFields.length && !interactive) return <div className="osk-message-preview__placeholder">Adicione conteúdo para começar.</div>;
  return (
    <div className="osk-message-preview__message-card" style={style}>
      {interactive && <AccentControl field={colorField} selectedFieldId={selectedFieldId} onSelectField={onSelectField} />}
      {titleField && <FieldText {...textProps} field={titleField} className="osk-message-preview__card-title" placeholder="Adicionar título" />}
      {bodyFields.map((field) => <FieldText key={field.id} {...textProps} field={field} className="osk-message-preview__body" placeholder="Adicionar mensagem" />)}
      {imageFields.map((field) => <ImageSlot key={field.id} urlField={field} draft={draft} interactive={interactive} selectedFieldId={selectedFieldId} onSelectField={onSelectField} className="osk-message-preview__generic-image" alt={field.label} fallbackLabel={field.label} />)}
      {footerField && <FieldText {...textProps} field={footerField} className="osk-message-preview__card-footer" placeholder="Adicionar rodapé" />}
    </div>
  );
}

function ColorRolesPanelPreview(props: PreviewCoreProps) {
  const { editorId = "", fields, draft, guildOptions, interactive, selectedFieldId, selectedColorSlot, onSelectField, onChange, onSelectColorSlot } = props;
  const layoutField = fields.find((field) => field.id === "color_roles.panel_layout");
  const slotsField = fields.find((field) => field.id === "color_roles.slots");
  const layout = normalizeColorPanelLayout(draft["color_roles.panel_layout"]);
  const panel = colorPanelFromEditorId(layout, editorId) ?? layout[0];
  const panelIndex = Math.max(0, layout.findIndex((item) => item.id === panel?.id));
  const rawSlots = draft["color_roles.slots"];
  const slots = rawSlots && typeof rawSlots === "object" ? rawSlots as Record<string, DashboardColorSlot> : {};
  const panelSlots = (panel?.slots ?? []).map((number) => ({
    ...(slots[String(number)] || ({ number, name: `Cor ${number}`, text_hex: "", role_hex: "", role_id: 0, role_name: `Cor ${number}`, managed: false } as DashboardColorSlot)),
    number,
  }));

  const commitLayout = (next: typeof layout) => {
    if (layoutField && onChange) onChange(layoutField, next);
  };
  const moveOption = (index: number, direction: -1 | 1) => {
    if (!panel) return;
    const target = index + direction;
    if (target < 0 || target >= panel.slots.length) return;
    commitLayout(updatePanelSlots(layout, panel.id, (current) => {
      [current[index], current[target]] = [current[target], current[index]];
      return current;
    }));
  };
  const removeOption = (slotNumber: number) => {
    if (!panel || panel.slots.length <= 1) return;
    commitLayout(updatePanelSlots(layout, panel.id, (current) => current.filter((number) => number !== slotNumber)));
    if (selectedColorSlot === slotNumber) onSelectColorSlot?.(panel.slots.find((number) => number !== slotNumber) ?? panel.slots[0]);
  };
  const addOption = () => {
    if (!panel || panel.slots.length >= COLOR_PANEL_OPTION_MAX) return;
    const number = nextUnusedColorSlot(layout);
    if (number === null) return;
    commitLayout(updatePanelSlots(layout, panel.id, (current) => [...current, number]));
    if (slotsField) onSelectField?.(slotsField);
    onSelectColorSlot?.(number, true);
  };

  const selectedIndex = selectedColorSlot == null
    ? -1
    : panelSlots.findIndex((slot) => slot.number === selectedColorSlot);
  const selectedSlot = selectedIndex >= 0 ? panelSlots[selectedIndex] : null;
  const selectSlot = (slotNumber: number, openInspector = false) => {
    if (!interactive || !slotsField) return;
    onSelectField?.(slotsField);
    onSelectColorSlot?.(slotNumber, openInspector);
  };

  return (
    <div className="osk-color-panel-canvas" data-panel={panelIndex + 1}>
      <div className="osk-color-panel-canvas__image" aria-label={`Imagem de opções do Painel ${panelIndex + 1}`}>
        {panelSlots.map((slot, index) => {
          const color = colorRoleHex(slot, guildOptions);
          const selected = selectedColorSlot === slot.number;
          return (
            <button
              type="button"
              key={slot.number}
              className="osk-color-panel-canvas__slot"
              data-selected={selected || undefined}
              data-message-field-anchor={selected ? "color_roles.slots" : undefined}
              data-dark-color={needsLightOutline(color) || undefined}
              style={{
                "--osk-slot-color": color,
                color,
                WebkitTextFillColor: color,
              } as CSSProperties}
              disabled={!interactive || !slotsField}
              onClick={(event) => { event.stopPropagation(); selectSlot(slot.number); }}
              onDoubleClick={(event) => { event.stopPropagation(); selectSlot(slot.number, true); }}
            >
              <b>{index + 1}.</b><span>{String(slot.name || `Cor ${slot.number}`)}</span>
            </button>
          );
        })}
      </div>

      {interactive && selectedSlot && (
        <div className="osk-color-panel-canvas__selection-tools">
          <span><strong>Opção {selectedIndex + 1}</strong><small>{String(selectedSlot.name || `Cor ${selectedSlot.number}`)}</small></span>
          <span>
            <button type="button" disabled={selectedIndex <= 0} onClick={() => moveOption(selectedIndex, -1)} aria-label="Mover opção para cima"><ArrowUp size={14} /></button>
            <button type="button" disabled={selectedIndex < 0 || selectedIndex >= panelSlots.length - 1} onClick={() => moveOption(selectedIndex, 1)} aria-label="Mover opção para baixo"><ArrowDown size={14} /></button>
            <button type="button" disabled={panelSlots.length <= 1} onClick={() => removeOption(selectedSlot.number)} aria-label="Remover opção"><Minus size={15} /></button>
          </span>
        </div>
      )}

      {interactive && slotsField && panel && panel.slots.length < COLOR_PANEL_OPTION_MAX && nextUnusedColorSlot(layout) !== null && (
        <button type="button" className="osk-color-panel-canvas__add" onClick={(event) => { event.stopPropagation(); addOption(); }}><Plus size={15} />Adicionar opção</button>
      )}
    </div>
  );
}

function resolveSender({ senderFields, draft, botName, botAvatarUrl, guildName, guildAvatarUrl }: {
  senderFields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  botName: string;
  botAvatarUrl?: string | null;
  guildName?: string;
  guildAvatarUrl?: string | null;
}) {
  const enabled = senderFields.length > 0 && Boolean(draft["welcome.webhook.enabled"]);
  if (!enabled) return { enabled: false, name: botName, avatar: botAvatarUrl, badge: "BOT" };
  const nameMode = String(draft["welcome.webhook.name_mode"] || "server");
  const avatarMode = String(draft["welcome.webhook.avatar_mode"] || "server");
  const customName = String(draft["welcome.webhook.name"] || "").trim();
  const customAvatar = String(draft["welcome.webhook.avatar_url"] || "").trim();
  const name = nameMode === "fixed" ? (customName || botName)
    : nameMode === "member" ? "Novo membro"
      : nameMode === "inviter" ? "Quem convidou"
        : guildName || "Nome do servidor";
  const avatar = avatarMode === "custom" ? customAvatar
    : avatarMode === "server" ? guildAvatarUrl
      : avatarMode === "member" || avatarMode === "inviter" ? null
        : botAvatarUrl;
  return { enabled: true, name, avatar, badge: "APP" };
}

function SenderHeader({
  senderFields,
  draft,
  botName,
  botAvatarUrl,
  guildName,
  guildAvatarUrl,
  interactive,
  selected,
  onSelect,
  onEdit,
  fieldId,
}: {
  senderFields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  botName: string;
  botAvatarUrl?: string | null;
  guildName?: string;
  guildAvatarUrl?: string | null;
  interactive?: boolean;
  selected?: boolean;
  onSelect?(): void;
  onEdit?(): void;
  fieldId?: string;
}) {
  const sender = useMemo(() => resolveSender({ senderFields, draft, botName, botAvatarUrl, guildName, guildAvatarUrl }), [senderFields, draft, botName, botAvatarUrl, guildName, guildAvatarUrl]);
  const enabled = interactive && senderFields.length > 0 && onSelect;
  const activate = () => { if (selected && onEdit) onEdit(); else onSelect?.(); };
  const content = <>
    <SmartAvatar name={sender.name} src={sender.avatar} type="server" size={34} className="osk-message-preview__avatar" />
    <div><strong>{sender.name}</strong><span>{sender.badge}</span></div>
    {selected && <span className="osk-message-sender__pencil" aria-hidden="true"><Pencil size={12} /></span>}
  </>;
  if (!enabled) return <div className="osk-message-preview__header">{content}</div>;
  return (
    <div role="button" tabIndex={0} className="osk-message-preview__header osk-message-sender" data-selected={selected || undefined} data-webhook={sender.enabled || undefined} data-message-field-anchor={fieldId} onClick={(event) => { event.stopPropagation(); activate(); }} onDoubleClick={(event) => { event.stopPropagation(); onSelect?.(); onEdit?.(); }} onKeyDown={(event) => { if (event.key !== "Enter" && event.key !== " ") return; event.preventDefault(); activate(); }} aria-label="Editar remetente da mensagem" title="Configurar remetente">
      {content}
    </div>
  );
}

export function MessagePreview({
  sectionId,
  editorId,
  groupLabel,
  presentation = "generic",
  fields,
  senderFields = [],
  draft,
  guildOptions,
  botName = "Osaka",
  botAvatarUrl,
  guildName,
  guildAvatarUrl,
  interactive,
  senderSelected,
  selectedFieldId,
  editingFieldId,
  selectedColorSlot,
  textSelection,
  onSelectSender,
  onEditSender,
  onSelectField,
  onEditField,
  hasFieldOptions,
  onOpenFieldOptions,
  onFinishEdit,
  onChange,
  onTextSelection,
  onSelectColorSlot,
}: MessagePreviewProps) {
  const isDm = editorId === "welcome-dm";
  const welcomeMode = String(draft[isDm ? "welcome.dm_render_mode" : "welcome.render_mode"] || "components_v2");
  const isAdaptiveWelcome = presentation === "adaptive" && sectionId === "welcome";
  const renderKind = presentation === "color_panel" ? "color-panel"
    : presentation === "components_v2" || (isAdaptiveWelcome && welcomeMode === "components_v2") ? "components-v2"
      : isAdaptiveWelcome && welcomeMode === "embed" ? "embed"
        : "generic";
  const previewFields = isAdaptiveWelcome
    ? fields.filter((field) => {
        if (isDm) return field.id.startsWith("welcome.dm.");
        if (welcomeMode === "embed") return field.id.includes(".embed.");
        if (welcomeMode === "normal" && field.id === "welcome.public.footer") return false;
        return !field.id.includes(".embed.");
      })
    : fields;
  const shared: PreviewCoreProps = {
    sectionId,
    editorId,
    fields: previewFields,
    draft,
    guildOptions,
    interactive,
    selectedFieldId,
    editingFieldId,
    selectedColorSlot,
    textSelection,
    onSelectField,
    onEditField,
    hasFieldOptions,
    onOpenFieldOptions,
    onFinishEdit,
    onChange,
    onTextSelection,
    onSelectColorSlot,
  };

  return (
    <div className="osk-message-preview" data-interactive={interactive ? "true" : "false"} data-editor-kind={renderKind}>
      <SenderHeader senderFields={senderFields} draft={draft} botName={botName} botAvatarUrl={botAvatarUrl} guildName={guildName} guildAvatarUrl={guildAvatarUrl} interactive={interactive} selected={senderSelected} onSelect={onSelectSender} onEdit={onEditSender} fieldId={senderFields.find((field) => field.id === "welcome.webhook.enabled")?.id ?? senderFields[0]?.id} />
      <div className="osk-message-preview__canvas" aria-label={`Mensagem editável de ${groupLabel}`}>
        {renderKind === "color-panel" ? <ColorRolesPanelPreview {...shared} />
          : isAdaptiveWelcome && welcomeMode === "components_v2" ? <WelcomeComponentsV2Preview {...shared} dm={isDm} />
            : renderKind === "components-v2" ? <ComponentsV2Preview {...shared} />
              : renderKind === "embed" && isDm ? <WelcomeDmEmbedPreview {...shared} />
                : renderKind === "embed" ? <EmbedPreview {...shared} />
                  : <GenericMessagePreview {...shared} />}
      </div>
    </div>
  );
}
