import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
} from "../../types/dashboard";
import { Check, ChevronDown } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { discordAttachmentUrlInfo, isValidPreviewUrl } from "./messageEditorUtils";

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

function isPreviewImageUrlField(field: DashboardFieldDefinition): boolean {
  return field.type === "url" && /(?:^|\.)(?:image|thumbnail|media|avatar|author_icon|footer_icon)_url$/i.test(field.id);
}

interface ContextualSelectProps {
  field: DashboardFieldDefinition;
  options: NonNullable<DashboardFieldDefinition["options"]>;
  value: string;
  onChange(value: string): void;
}

function ContextualSelect({ field, options, value, onChange }: ContextualSelectProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [position, setPosition] = useState({ left: 8, top: 8, width: 220, maxHeight: 246, placement: "below" as "above" | "below" });
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selected = selectedIndex >= 0 ? options[selectedIndex] : null;
  const listboxId = `context-select-${field.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;

  function enabledIndex(start: number, direction: -1 | 1) {
    if (!options.length) return -1;
    return (start + direction + options.length) % options.length;
  }

  function updatePlacement() {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const triggerRect = trigger.getBoundingClientRect();
    const visualViewport = window.visualViewport;
    const viewportLeft = visualViewport?.offsetLeft ?? 0;
    const viewportTop = visualViewport?.offsetTop ?? 0;
    const viewportWidth = visualViewport?.width ?? window.innerWidth;
    const viewportHeight = visualViewport?.height ?? window.innerHeight;
    const viewportRight = viewportLeft + viewportWidth;
    const viewportBottom = viewportTop + viewportHeight;
    const expectedHeight = Math.min(246, Math.max(48, options.length * 42 + 8));
    const roomBelow = viewportBottom - triggerRect.bottom - 8;
    const roomAbove = triggerRect.top - viewportTop - 8;
    const placement = roomBelow < Math.min(expectedHeight, 168) && roomAbove > roomBelow ? "above" : "below";
    const availableHeight = placement === "above" ? roomAbove : roomBelow;
    const maxHeight = Math.min(expectedHeight, Math.max(88, availableHeight));
    const width = Math.min(triggerRect.width, Math.max(180, viewportWidth - 16));
    const left = Math.min(
      Math.max(viewportLeft + 8, triggerRect.left),
      Math.max(viewportLeft + 8, viewportRight - width - 8),
    );
    const top = placement === "above"
      ? Math.max(viewportTop + 8, triggerRect.top - maxHeight - 6)
      : Math.min(viewportBottom - maxHeight - 8, triggerRect.bottom + 6);
    setPosition({ left, top, width, maxHeight, placement });
  }

  function close(restoreFocus = true) {
    setOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
  }

  function show(initialIndex = selectedIndex) {
    const fallback = enabledIndex(-1, 1);
    setActiveIndex(initialIndex >= 0 ? initialIndex : fallback);
    updatePlacement();
    setOpen(true);
  }

  function commit(index: number) {
    const option = options[index];
    if (!option) return;
    onChange(option.value);
    close();
  }

  useEffect(() => {
    if (!open) return;
    const viewBody = rootRef.current?.closest<HTMLElement>(".osk-message-editor__view-body");
    const selectedOption = menuRef.current?.querySelector<HTMLElement>("[data-selected]");
    const frame = window.requestAnimationFrame(() => {
      menuRef.current?.focus({ preventScroll: true });
      selectedOption?.scrollIntoView({ block: "nearest" });
      updatePlacement();
    });
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) close(false);
    }
    function handleEscape(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      close();
    }
    document.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleEscape, true);
    window.addEventListener("resize", updatePlacement);
    window.visualViewport?.addEventListener("resize", updatePlacement);
    window.visualViewport?.addEventListener("scroll", updatePlacement);
    viewBody?.addEventListener("scroll", updatePlacement, { passive: true });
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleEscape, true);
      window.removeEventListener("resize", updatePlacement);
      window.visualViewport?.removeEventListener("resize", updatePlacement);
      window.visualViewport?.removeEventListener("scroll", updatePlacement);
      viewBody?.removeEventListener("scroll", updatePlacement);
    };
  }, [open, options.length, selectedIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const origin = selectedIndex >= 0 ? selectedIndex - direction : direction === 1 ? -1 : 0;
    show(enabledIndex(origin, direction));
  }

  function handleMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Tab") {
      close(false);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setActiveIndex(enabledIndex(event.key === "Home" ? -1 : 0, event.key === "Home" ? 1 : -1));
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => enabledIndex(current, event.key === "ArrowDown" ? 1 : -1));
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      commit(activeIndex);
    }
  }

  const menuStyle = {
    "--osk-context-select-left": `${position.left}px`,
    "--osk-context-select-top": `${position.top}px`,
    "--osk-context-select-width": `${position.width}px`,
    "--osk-context-select-max-height": `${position.maxHeight}px`,
  } as CSSProperties;
  const portalTarget = typeof document !== "undefined"
    ? rootRef.current?.closest<HTMLElement>(".osk-message-editor") ?? document.body
    : null;

  const menu = open && portalTarget ? createPortal(<div
    ref={menuRef}
    id={listboxId}
    className="osk-message-context-select__menu"
    data-placement={position.placement}
    style={menuStyle}
    role="listbox"
    aria-label={field.label}
    aria-activedescendant={activeIndex >= 0 ? `${listboxId}-option-${activeIndex}` : undefined}
    tabIndex={0}
    onKeyDown={handleMenuKeyDown}
  >
    {options.map((option, index) => {
      const isSelected = option.value === value;
      const isActive = index === activeIndex;
      return <button
        type="button"
        id={`${listboxId}-option-${index}`}
        key={option.value}
        role="option"
        aria-selected={isSelected}
        data-selected={isSelected || undefined}
        data-active={isActive || undefined}
        onPointerMove={() => setActiveIndex(index)}
        onClick={() => commit(index)}
      >
        <span>{option.label}</span>
        {isSelected && <Check size={14} aria-hidden="true" />}
      </button>;
    })}
  </div>, portalTarget) : null;

  return <div className="osk-message-context-select" data-open={open || undefined} data-placement={position.placement} ref={rootRef}>
    <button
      ref={triggerRef}
      type="button"
      className="osk-message-context-select__trigger"
      aria-label={field.label}
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-controls={open ? listboxId : undefined}
      onClick={() => open ? close() : show()}
      onKeyDown={handleTriggerKeyDown}
    >
      <span>{selected?.label ?? "Selecione uma opção"}</span>
      <ChevronDown size={16} aria-hidden="true" />
    </button>
    {menu}
  </div>;
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
      const imageUrlField = isPreviewImageUrlField(field);
      const validImageUrl = !currentText || isValidPreviewUrl(currentText);
      const discordAttachment = imageUrlField ? discordAttachmentUrlInfo(currentText) : null;
      return <section key={field.id} className="osk-message-form__field" data-changed={changed || undefined} data-selected={selectedFieldId === field.id || undefined} data-type={field.type} data-has-description={field.description ? "true" : undefined} onFocusCapture={() => onFocusField?.(field)}>
        <header>
          <div><strong>{field.label}</strong>{field.description && <small>{field.description}</small>}</div>
          {field.maxLength && ["text", "textarea", "url"].includes(field.type) && <span>{currentText.length}/{field.maxLength}</span>}
        </header>
        {contextual && field.type === "select" && contextualOptions.length > 0 && contextualOptions.length <= 8 ? (
          <ContextualSelect
            field={field}
            options={contextualOptions}
            value={String(draft[field.id] ?? "")}
            onChange={(value) => onChange(field, value)}
          />
        ) : (
          <DashboardFieldControl field={field} value={draft[field.id]} guildOptions={guildOptions} onChange={onChange} onTextSelection={onTextSelection} selectedColorSlot={selectedColorSlot} colorSlotIds={colorSlotIds} onColorSlotSelect={onColorSlotSelect} />
        )}
        {imageUrlField && currentText && !validImageUrl ? (
          <small className="osk-message-url-hint" data-state="error">Cole um link HTTPS completo para carregar a imagem.</small>
        ) : null}
        {discordAttachment?.expired ? (
          <small className="osk-message-url-hint" data-state="error">Este link temporário do Discord expirou. Copie um link novo ou use uma hospedagem permanente.</small>
        ) : discordAttachment ? (
          <small className="osk-message-url-hint" data-state="warning">Links de anexos do Discord expiram. A prévia usa uma rota de compatibilidade, mas vale trocar por um link permanente.</small>
        ) : null}
      </section>;
    })}
  </div>;
}
