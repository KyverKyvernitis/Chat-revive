import {
  Bold,
  Braces,
  Check,
  ChevronLeft,
  Code2,
  Italic,
  Link2,
  Redo2,
  Strikethrough,
  Undo2,
  Variable,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type TransitionEvent } from "react";
import { createPortal } from "react-dom";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import {
  colorPanelFromEditorId,
  normalizeColorPanelLayout,
} from "../color-roles/colorRolesModel";
import { MessageJsonEditor } from "./MessageJsonEditor";
import { MessagePreview } from "./MessagePreview";
import { MessageVariablesPanel } from "./MessageVariablesPanel";
import { MessageVisualEditor } from "./MessageVisualEditor";
import type {
  JsonFieldChange,
  MessageEditorProps,
} from "./messageEditorTypes";
import {
  formatTemplateVariable,
  messageFieldsObject,
  parseMessageJson,
  pendingChangesReachedDraft,
  serializeMessageFields,
} from "./messageEditorUtils";

interface HistoryChange {
  field: DashboardFieldDefinition;
  before: unknown;
  after: unknown;
}

interface HistoryEntry {
  changes: HistoryChange[];
  mergeKey: string | null;
  at: number;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

function cloneValue<T>(value: T): T {
  if (value === undefined || value === null || typeof value !== "object") return value;
  try { return structuredClone(value); } catch {
    try { return JSON.parse(JSON.stringify(value)) as T; } catch { return value; }
  }
}

function editorVisualFieldVisible(editorId: string, fieldId: string, draft: Record<string, unknown>): boolean {
  if (fieldId.startsWith("welcome.webhook.")) {
    if (fieldId === "welcome.webhook.enabled") return true;
    if (!Boolean(draft["welcome.webhook.enabled"])) return false;
    if (fieldId === "welcome.webhook.name") return String(draft["welcome.webhook.name_mode"] || "fixed") === "fixed";
    if (fieldId === "welcome.webhook.avatar_url") return String(draft["welcome.webhook.avatar_mode"] || "server") === "custom";
    return true;
  }
  if (editorId === "welcome-public") {
    const renderMode = String(draft["welcome.render_mode"] || "components_v2");
    if (["welcome.style", "welcome.accent_color", "welcome.accent_color_mode", "welcome.media_mode", "welcome.media_url"].includes(fieldId)) {
      if (renderMode !== "components_v2") return false;
      if (fieldId === "welcome.accent_color") return String(draft["welcome.accent_color_mode"] || "fixed") === "fixed";
      if (fieldId === "welcome.media_url") return String(draft["welcome.media_mode"] || "custom") === "custom";
      return true;
    }
    if (fieldId.includes(".embed.")) {
      if (renderMode !== "embed") return false;
      if (fieldId === "welcome.embed.color") return String(draft["welcome.embed.color_mode"] || "fixed") === "fixed";
      if (fieldId === "welcome.embed.author_icon_url") return String(draft["welcome.embed.author_icon_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.thumbnail_url") return String(draft["welcome.embed.thumbnail_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.image_url") return String(draft["welcome.embed.image_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.footer_icon_url") return String(draft["welcome.embed.footer_icon_mode"] || "none") === "custom";
      return true;
    }
    if (fieldId.includes(".public.")) return renderMode !== "embed";
  }
  return true;
}

type MessageEditorView = "canvas" | "inspector" | "variables" | "json";

function relatedContextFields(
  selected: DashboardFieldDefinition | null,
  fields: DashboardFieldDefinition[],
): DashboardFieldDefinition[] {
  if (!selected) return [];
  const id = selected.id;
  if (id.startsWith("welcome.webhook.")) {
    return fields.filter((field) => field.id.startsWith("welcome.webhook."));
  }
  if (["welcome.style", "welcome.accent_color", "welcome.accent_color_mode"].includes(id)) {
    return fields.filter((field) => ["welcome.style", "welcome.accent_color", "welcome.accent_color_mode"].includes(field.id));
  }
  if (["welcome.media_mode", "welcome.media_url"].includes(id)) {
    return fields.filter((field) => ["welcome.media_mode", "welcome.media_url"].includes(field.id));
  }
  const groups: RegExp[] = [
    /^(.*\.author_)(?:name|icon_mode|icon_url|url)$/,
    /^(.*\.footer_)(?:text|icon_mode|icon_url)$/,
    /^(.*\.image_)(?:mode|url)$/,
    /^(.*\.thumbnail_)(?:mode|url)$/,
    /^(.*\.color)(?:_mode)?$/,
    /^(.*\.title)(?:_url)?$/,
  ];
  for (const pattern of groups) {
    const match = id.match(pattern);
    if (!match) continue;
    const prefix = match[1];
    const related = fields.filter((field) => field.id.startsWith(prefix));
    if (related.length) return related;
  }

  const actionMatch = id.match(/^(.*?)(approve|reject|button)(?:_|$)/);
  if (actionMatch) {
    const prefix = `${actionMatch[1]}${actionMatch[2]}`;
    const related = fields.filter((field) => field.id.startsWith(prefix));
    if (related.length) return related;
  }

  return [selected];
}

export function MessageEditor(props: MessageEditorProps) {
  const {
    editorId,
    sectionId,
    sectionLabel,
    groupLabel,
    description,
    fields,
    senderFieldIds = [],
    presentation = "generic",
    baseline,
    draft,
    guildOptions,
    botName,
    botAvatarUrl,
    guildName,
    guildAvatarUrl,
    variables,
    onChange,
    onApply,
    onDiscard,
  } = props;

  const editorKey = `${sectionId}:${editorId}`;
  const senderFieldIdSet = useMemo(() => new Set(senderFieldIds), [senderFieldIds]);
  const jsonFields = useMemo(() => fields.filter((field) => field.type !== "color_slots" && field.type !== "color_panel_layout"), [fields]);
  const serializedDraft = useMemo(() => serializeMessageFields(jsonFields, draft), [draft, jsonFields]);
  const visualFields = useMemo(
    () => fields.filter((field) => editorVisualFieldVisible(editorId, field.id, draft)),
    [draft, editorId, fields],
  );
  const senderFields = useMemo(
    () => visualFields.filter((field) => senderFieldIdSet.has(field.id)),
    [senderFieldIdSet, visualFields],
  );
  const messageFields = useMemo(
    () => visualFields.filter((field) => !senderFieldIdSet.has(field.id)),
    [senderFieldIdSet, visualFields],
  );
  const [visible, setVisible] = useState(false);
  const [view, setView] = useState<MessageEditorView>("canvas");
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(null);
  const [editingFieldId, setEditingFieldId] = useState<string | null>(null);
  const [selectedColorSlot, setSelectedColorSlot] = useState<number | null>(null);
  const [jsonText, setJsonText] = useState(serializedDraft);
  const [jsonBaseline, setJsonBaseline] = useState<Record<string, unknown>>(() => messageFieldsObject(jsonFields, draft));
  const [jsonDirty, setJsonDirty] = useState(false);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [pendingJsonChanges, setPendingJsonChanges] = useState<JsonFieldChange[] | null>(null);
  const [activeTextFieldId, setActiveTextFieldId] = useState<string | null>(null);
  const [historyStatus, setHistoryStatus] = useState({ index: 0, length: 0 });

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const closeIntent = useRef<"apply" | "discard" | null>(null);
  const finalIntent = useRef<"apply" | "discard" | null>(null);
  const closing = useRef(false);
  const closeTimerRef = useRef<number | null>(null);
  const scrollPositionRef = useRef(0);
  const textSelectionRef = useRef<{ fieldId: string; start: number; end: number } | null>(null);
  const historyMarker = useRef(`osk-editor-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const historyRef = useRef<HistoryEntry[]>([]);
  const historyIndexRef = useRef(0);
  const latestDraftRef = useRef<Record<string, unknown>>({ ...draft });
  const closeAfterJsonApplyRef = useRef(false);
  const viewRef = useRef<MessageEditorView>(view);
  const auxiliaryBackRef = useRef<() => void>(() => undefined);

  const localDirty = useMemo(
    () => fields.some((field) => !valuesEqual(baseline[field.id], draft[field.id])),
    [baseline, draft, fields],
  );
  const localDirtyRef = useRef(localDirty);
  const jsonDirtyRef = useRef(jsonDirty);
  const pendingJsonChangesRef = useRef(pendingJsonChanges);
  const editingFieldIdRef = useRef(editingFieldId);
  const onChangeRef = useRef(onChange);
  const onApplyRef = useRef(onApply);
  const onDiscardRef = useRef(onDiscard);
  const applyActionRef = useRef<() => void>(() => undefined);

  localDirtyRef.current = localDirty;
  jsonDirtyRef.current = jsonDirty;
  pendingJsonChangesRef.current = pendingJsonChanges;
  editingFieldIdRef.current = editingFieldId;
  viewRef.current = view;
  onChangeRef.current = onChange;
  onApplyRef.current = onApply;
  onDiscardRef.current = onDiscard;

  useEffect(() => {
    latestDraftRef.current = { ...draft };
  }, [draft]);

  const restoreHistoryMarker = useCallback(() => {
    if (window.history.state?.oskMessageEditor === historyMarker.current) return;
    window.history.pushState(
      { ...(window.history.state || {}), oskMessageEditor: historyMarker.current },
      "",
      window.location.href,
    );
  }, []);

  const finalizeClose = useCallback(() => {
    if (!closing.current) return;
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    const intent = finalIntent.current ?? "apply";
    finalIntent.current = null;
    if (intent === "apply") onApplyRef.current();
    else onDiscardRef.current();
  }, []);

  const beginClose = useCallback((intent: "apply" | "discard") => {
    if (closing.current) return;
    if (intent === "discard" && (localDirtyRef.current || jsonDirtyRef.current)
      && !window.confirm("Descartar as alterações feitas neste editor?")) {
      restoreHistoryMarker();
      return;
    }
    closing.current = true;
    finalIntent.current = intent;
    setVisible(false);
    closeTimerRef.current = window.setTimeout(finalizeClose, 300);
  }, [finalizeClose, restoreHistoryMarker]);

  const handleHistoryClose = useCallback(() => {
    const intent = closeIntent.current;
    closeIntent.current = null;
    if (intent) beginClose(intent);
    else applyActionRef.current();
  }, [beginClose]);

  const requestClose = useCallback((intent: "apply" | "discard") => {
    if (closing.current) return;
    closeIntent.current = intent;
    if (window.history.state?.oskMessageEditor === historyMarker.current) window.history.back();
    else handleHistoryClose();
  }, [handleHistoryClose]);

  function updateHistoryStatus() {
    setHistoryStatus({ index: historyIndexRef.current, length: historyRef.current.length });
  }

  function recordChanges(changes: Array<{ field: DashboardFieldDefinition; raw: unknown }>, merge = true) {
    const normalized = changes.map(({ field, raw }) => ({
      field,
      before: cloneValue(latestDraftRef.current[field.id]),
      after: cloneValue(raw),
    })).filter((change) => !valuesEqual(change.before, change.after));
    if (!normalized.length) return;

    const now = Date.now();
    const mergeKey = merge && normalized.length === 1 ? normalized[0].field.id : null;
    const entries = historyRef.current.slice(0, historyIndexRef.current);
    const last = entries[entries.length - 1];
    if (merge && mergeKey && last?.mergeKey === mergeKey && now - last.at < 750 && last.changes.length === 1) {
      last.changes[0].after = cloneValue(normalized[0].after);
      last.at = now;
    } else {
      entries.push({ changes: normalized, mergeKey, at: now });
    }
    historyRef.current = entries;
    historyIndexRef.current = entries.length;
    updateHistoryStatus();

    for (const change of normalized) {
      latestDraftRef.current[change.field.id] = cloneValue(change.after);
      onChangeRef.current(change.field, change.after);
    }
  }

  function handleFieldChange(field: DashboardFieldDefinition, raw: unknown) {
    recordChanges([{ field, raw }], true);
  }

  function undo() {
    if (historyIndexRef.current <= 0 || jsonDirtyRef.current || pendingJsonChangesRef.current) return;
    const entry = historyRef.current[historyIndexRef.current - 1];
    for (const change of [...entry.changes].reverse()) {
      latestDraftRef.current[change.field.id] = cloneValue(change.before);
      onChangeRef.current(change.field, cloneValue(change.before));
    }
    historyIndexRef.current -= 1;
    updateHistoryStatus();
    setEditingFieldId(null);
  }

  function redo() {
    if (historyIndexRef.current >= historyRef.current.length || jsonDirtyRef.current || pendingJsonChangesRef.current) return;
    const entry = historyRef.current[historyIndexRef.current];
    for (const change of entry.changes) {
      latestDraftRef.current[change.field.id] = cloneValue(change.after);
      onChangeRef.current(change.field, cloneValue(change.after));
    }
    historyIndexRef.current += 1;
    updateHistoryStatus();
    setEditingFieldId(null);
  }

  useEffect(() => {
    setSelectedFieldId(null);
    setEditingFieldId(null);
    setSelectedColorSlot(null);
    setView("canvas");
    setJsonText(serializedDraft);
    setJsonBaseline(messageFieldsObject(jsonFields, draft));
    setJsonDirty(false);
    setJsonError(null);
    setPendingJsonChanges(null);
    closeAfterJsonApplyRef.current = false;
    setActiveTextFieldId(null);
    textSelectionRef.current = null;
    latestDraftRef.current = { ...draft };
    historyRef.current = [];
    historyIndexRef.current = 0;
    setHistoryStatus({ index: 0, length: 0 });
    closing.current = false;
    closeIntent.current = null;
    finalIntent.current = null;
    setVisible(false);
    const firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => setVisible(true));
    });
    return () => window.cancelAnimationFrame(firstFrame);
  }, [editorKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selectedFieldId || visualFields.some((field) => field.id === selectedFieldId)) return;
    setSelectedFieldId(null);
    setEditingFieldId(null);
    setView("canvas");
    setActiveTextFieldId(null);
    textSelectionRef.current = null;
  }, [selectedFieldId, visualFields]);

  useEffect(() => {
    if (!editingFieldId) return;
    const frame = window.requestAnimationFrame(() => {
      const target = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("[data-message-inline-field-id]") ?? [])
        .find((element) => element.dataset.messageInlineFieldId === editingFieldId);
      target?.scrollIntoView({ block: "center", behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [editingFieldId]);

  useEffect(() => {
    if (!jsonDirty && pendingJsonChanges === null) {
      setJsonText(serializedDraft);
      setJsonBaseline(messageFieldsObject(jsonFields, draft));
    }
  }, [draft, jsonDirty, jsonFields, pendingJsonChanges, serializedDraft]);

  useEffect(() => {
    if (!pendingJsonChanges || !pendingChangesReachedDraft(pendingJsonChanges, draft)) return;
    setPendingJsonChanges(null);
    setJsonDirty(false);
    setJsonError(null);
    setJsonText(serializeMessageFields(jsonFields, draft));
    setJsonBaseline(messageFieldsObject(jsonFields, draft));
    if (closeAfterJsonApplyRef.current) {
      closeAfterJsonApplyRef.current = false;
      requestClose("apply");
    } else {
      setView("canvas");
    }
  }, [draft, jsonFields, pendingJsonChanges]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previousPosition = document.body.style.position;
    const previousTop = document.body.style.top;
    const previousWidth = document.body.style.width;
    scrollPositionRef.current = window.scrollY;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    document.body.style.position = "fixed";
    document.body.style.top = `-${scrollPositionRef.current}px`;
    document.body.style.width = "100%";

    const syncVisualViewport = () => {
      const viewport = window.visualViewport;
      const height = Math.max(1, Math.round(viewport?.height ?? window.innerHeight));
      dialogRef.current?.style.setProperty("--osk-message-editor-viewport-height", `${height}px`);
    };
    syncVisualViewport();
    window.visualViewport?.addEventListener("resize", syncVisualViewport);
    window.visualViewport?.addEventListener("scroll", syncVisualViewport);
    window.addEventListener("resize", syncVisualViewport);

    restoreHistoryMarker();
    window.setTimeout(() => dialogRef.current?.querySelector<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)")?.focus(), 0);
    const onBackRequest = () => {
      if (viewRef.current !== "canvas") {
        auxiliaryBackRef.current();
        restoreHistoryMarker();
        return;
      }
      handleHistoryClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) redo(); else undo();
        return;
      }
      if (event.key === "Escape") {
        if (editingFieldIdRef.current) {
          event.preventDefault();
          setEditingFieldId(null);
          return;
        }
        if (viewRef.current !== "canvas") {
          event.preventDefault();
          auxiliaryBackRef.current();
          return;
        }
        event.preventDefault();
        applyActionRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), [href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("osk:message-editor-back", onBackRequest as EventListener);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
      document.body.style.overflow = previousOverflow;
      document.body.style.position = previousPosition;
      document.body.style.top = previousTop;
      document.body.style.width = previousWidth;
      window.scrollTo({ top: scrollPositionRef.current, behavior: "auto" });
      window.removeEventListener("osk:message-editor-back", onBackRequest as EventListener);
      window.removeEventListener("keydown", onKeyDown);
      window.visualViewport?.removeEventListener("resize", syncVisualViewport);
      window.visualViewport?.removeEventListener("scroll", syncVisualViewport);
      window.removeEventListener("resize", syncVisualViewport);
      window.setTimeout(() => returnFocusRef.current?.focus(), 0);
    };
  }, [editorKey, handleHistoryClose, requestClose, restoreHistoryMarker]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleJsonChange(next: string) {
    setJsonText(next);
    setJsonDirty(true);
    setJsonError(null);
    setEditingFieldId(null);
    setView("json");
  }

  function discardJson() {
    setJsonText(serializedDraft);
    setJsonBaseline(messageFieldsObject(jsonFields, draft));
    setJsonDirty(false);
    setJsonError(null);
    closeAfterJsonApplyRef.current = false;
  }

  function applyJson(closeAfter = false) {
    if (pendingJsonChanges) return;
    try {
      const changes = parseMessageJson(jsonText, jsonFields, draft, jsonBaseline);
      setJsonError(null);
      if (changes.length === 0) {
        setJsonDirty(false);
        setJsonText(serializedDraft);
        setJsonBaseline(messageFieldsObject(jsonFields, draft));
        if (closeAfter) requestClose("apply");
        else setView("canvas");
        return;
      }
      closeAfterJsonApplyRef.current = closeAfter;
      setPendingJsonChanges(changes);
      recordChanges(changes.map((change) => ({ field: change.field, raw: change.raw })), false);
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : "JSON inválido.");
      setView("json");
      restoreHistoryMarker();
    }
  }

  function handleApply() {
    if (pendingJsonChanges) {
      restoreHistoryMarker();
      return;
    }
    if (jsonDirty) {
      applyJson(true);
      return;
    }
    requestClose("apply");
  }

  applyActionRef.current = handleApply;

  const colorPanelLayout = sectionId === "color_roles" ? normalizeColorPanelLayout(draft["color_roles.panel_layout"]) : [];
  const activeColorPanel = sectionId === "color_roles" ? colorPanelFromEditorId(colorPanelLayout, editorId) : null;
  const colorSlotIds = activeColorPanel?.slots ?? null;
  const selectedField = fields.find((field) => field.id === selectedFieldId) ?? null;
  const senderPrimaryField = senderFields.find((field) => field.id === "welcome.webhook.enabled") ?? senderFields[0] ?? null;
  const senderSelected = Boolean(selectedField && senderFieldIdSet.has(selectedField.id));
  const senderEnabled = Boolean(draft["welcome.webhook.enabled"]);
  const activeTextField = fields.find((field) => field.id === activeTextFieldId && (field.type === "text" || field.type === "textarea"))
    ?? null;

  function handleSelectSender() {
    if (!senderPrimaryField || jsonDirty || pendingJsonChanges) return;
    setSelectedFieldId(senderPrimaryField.id);
    setEditingFieldId(null);
    setActiveTextFieldId(null);
    setView("canvas");
  }

  function handleEditSender() {
    if (!senderPrimaryField || jsonDirty || pendingJsonChanges) return;
    setSelectedFieldId(senderPrimaryField.id);
    setEditingFieldId(null);
    setActiveTextFieldId(null);
    setView("inspector");
  }

  function handleSelectField(field: DashboardFieldDefinition) {
    if (jsonDirty || pendingJsonChanges) {
      setView("json");
      return;
    }
    setSelectedFieldId(field.id);
    setEditingFieldId((current) => current === field.id ? current : null);
    if (presentation === "color_panel" && field.id === "color_roles.slots") {
      setActiveTextFieldId(null);
      setView("canvas");
      return;
    }
    if (field.type === "text" || field.type === "textarea") {
      setActiveTextFieldId(field.id);
      setView("canvas");
      if (textSelectionRef.current?.fieldId !== field.id) textSelectionRef.current = null;
      return;
    }
    setView("inspector");
  }

  function handleEditField(field: DashboardFieldDefinition) {
    if (jsonDirty || pendingJsonChanges || (field.type !== "text" && field.type !== "textarea")) return;
    setSelectedFieldId(field.id);
    setActiveTextFieldId(field.id);
    setView("canvas");
    setEditingFieldId(field.id);
  }

  function handleTextSelection(field: DashboardFieldDefinition, start: number, end: number) {
    setActiveTextFieldId(field.id);
    textSelectionRef.current = { fieldId: field.id, start, end };
  }

  function focusTextControl(fieldId: string, start: number, end: number) {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const inline = Array.from(dialogRef.current?.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-message-inline-field-id]") ?? [])
          .find((element) => element.dataset.messageInlineFieldId === fieldId);
        const form = Array.from(dialogRef.current?.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-message-field-id]") ?? [])
          .find((element) => element.dataset.messageFieldId === fieldId);
        const control = inline ?? form;
        if (!control) return;
        control.focus({ preventScroll: true });
        control.setSelectionRange(start, end);
      });
    });
  }

  function replaceTextSelection(insertion: string, selectInserted = false) {
    if (!activeTextField) return;
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    const start = Math.max(0, Math.min(current.length, saved?.start ?? current.length));
    const end = Math.max(start, Math.min(current.length, saved?.end ?? start));
    const next = `${current.slice(0, start)}${insertion}${current.slice(end)}`;
    const nextStart = selectInserted ? start : start + insertion.length;
    const nextEnd = start + insertion.length;
    recordChanges([{ field: activeTextField, raw: next }], true);
    textSelectionRef.current = { fieldId: activeTextField.id, start: nextStart, end: nextEnd };
    setSelectedFieldId(activeTextField.id);
    focusTextControl(activeTextField.id, nextStart, nextEnd);
  }

  function wrapText(prefix: string, suffix: string, placeholder: string) {
    if (!activeTextField) return;
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    const start = Math.max(0, Math.min(current.length, saved?.start ?? current.length));
    const end = Math.max(start, Math.min(current.length, saved?.end ?? start));
    const selected = current.slice(start, end) || placeholder;
    const insertion = `${prefix}${selected}${suffix}`;
    const next = `${current.slice(0, start)}${insertion}${current.slice(end)}`;
    const selectionStart = start + prefix.length;
    const selectionEnd = selectionStart + selected.length;
    recordChanges([{ field: activeTextField, raw: next }], true);
    textSelectionRef.current = { fieldId: activeTextField.id, start: selectionStart, end: selectionEnd };
    setSelectedFieldId(activeTextField.id);
    focusTextControl(activeTextField.id, selectionStart, selectionEnd);
  }

  function handleInsertVariable(key: string) {
    if (!variables || !activeTextField) return;
    const targetId = activeTextField.id;
    const token = formatTemplateVariable(variables.syntax, key);
    const current = String(latestDraftRef.current[targetId] ?? "");
    const saved = textSelectionRef.current?.fieldId === targetId ? textSelectionRef.current : null;
    const separator = !saved && current && !/\s$/.test(current) ? " " : "";
    replaceTextSelection(`${separator}${token}`);
    setView("canvas");
    setEditingFieldId(targetId);
  }

  const inspectorFields = relatedContextFields(selectedField, visualFields);
  const activeEditingField = editingFieldId
    ? fields.find((field) => field.id === editingFieldId && (field.type === "text" || field.type === "textarea")) ?? null
    : null;
  const activeEditingValue = activeEditingField ? String(draft[activeEditingField.id] ?? "") : "";
  const applyDisabled = Boolean(pendingJsonChanges);
  const canvasInteractive = !jsonDirty && !pendingJsonChanges;

  function openVariables() {
    setEditingFieldId(null);
    setView("variables");
  }

  function openJson() {
    setEditingFieldId(null);
    setView("json");
  }

  function openInspector(field = selectedField) {
    if (!field || jsonDirty || pendingJsonChanges) return;
    setSelectedFieldId(field.id);
    setEditingFieldId(null);
    setView("inspector");
  }

  function leaveAuxiliaryView() {
    if (pendingJsonChanges) return;
    if (view === "json" && jsonDirty) {
      if (!window.confirm("Descartar as alterações não aplicadas do JSON?")) return;
      discardJson();
    }
    setView("canvas");
  }

  auxiliaryBackRef.current = leaveAuxiliaryView;

  function preventToolbarBlur(event: ReactMouseEvent<HTMLButtonElement>) {
    event.preventDefault();
  }

  function handleTransitionEnd(event: TransitionEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget || visible || !closing.current) return;
    if (event.propertyName === "opacity") finalizeClose();
  }

  const editor = <div
    ref={dialogRef}
    className="osk-root osk-message-editor osk-message-editor--unified"
    data-visible={visible || undefined}
    data-editor-view={view}
    data-text-editing={activeEditingField ? "true" : undefined}
    role="dialog"
    aria-modal="true"
    aria-label={`Editar ${groupLabel}`}
    onTransitionEnd={handleTransitionEnd}
  >
    <div className="osk-message-editor__shell">
      <header className="osk-message-editor__header">
        <button
          type="button"
          className="osk-message-editor__back"
          onClick={view === "canvas" ? handleApply : leaveAuxiliaryView}
          aria-label={view === "canvas" ? "Aplicar e voltar" : "Voltar à mensagem"}
        >
          <ChevronLeft size={19} />
        </button>
        <div className="osk-message-editor__title">
          <small>{sectionLabel}</small>
          <strong>{groupLabel}</strong>
        </div>
        <div className="osk-message-editor__header-actions">
          <button type="button" disabled={historyStatus.index <= 0 || jsonDirty || Boolean(pendingJsonChanges)} onClick={undo} aria-label="Desfazer" title="Desfazer"><Undo2 size={17} /></button>
          <button type="button" disabled={historyStatus.index >= historyStatus.length || jsonDirty || Boolean(pendingJsonChanges)} onClick={redo} aria-label="Refazer" title="Refazer"><Redo2 size={17} /></button>
          {variables?.items.length ? <button type="button" disabled={Boolean(pendingJsonChanges)} onClick={openVariables} aria-label="Abrir variáveis" title="Variáveis"><Variable size={17} /></button> : null}
          {presentation !== "color_panel" && <button type="button" disabled={Boolean(pendingJsonChanges)} onClick={openJson} aria-label="Abrir JSON avançado" title="JSON avançado"><Braces size={17} /></button>}
          <span className="osk-message-editor__dirty" data-visible={localDirty || jsonDirty || undefined} aria-live="polite" aria-hidden={!(localDirty || jsonDirty)}>Alterado</span>
        </div>
      </header>

      <div className="osk-message-editor__workspace">
        {view === "canvas" ? (
          <section className="osk-message-editor__canvas-pane">
            <MessagePreview
              sectionId={sectionId}
              editorId={editorId}
              groupLabel={groupLabel}
              presentation={presentation}
              fields={messageFields}
              senderFields={senderFields}
              draft={draft}
              guildOptions={guildOptions}
              botName={botName}
              botAvatarUrl={botAvatarUrl}
              guildName={guildName}
              guildAvatarUrl={guildAvatarUrl}
              interactive={canvasInteractive}
              senderSelected={senderSelected}
              onSelectSender={handleSelectSender}
              onEditSender={handleEditSender}
              selectedFieldId={selectedFieldId}
              editingFieldId={editingFieldId}
              selectedColorSlot={selectedColorSlot}
              textSelection={textSelectionRef.current}
              onSelectField={handleSelectField}
              onEditField={handleEditField}
              hasFieldOptions={(field) => relatedContextFields(field, visualFields).length > 1}
              onOpenFieldOptions={openInspector}
              onFinishEdit={() => setEditingFieldId(null)}
              onChange={handleFieldChange}
              onTextSelection={handleTextSelection}
              onSelectColorSlot={(slotNumber, openInspector = false) => {
                const repeated = selectedColorSlot === slotNumber && selectedFieldId === "color_roles.slots";
                setSelectedColorSlot(slotNumber);
                setSelectedFieldId("color_roles.slots");
                setEditingFieldId(null);
                setActiveTextFieldId(null);
                setView(openInspector || repeated ? "inspector" : "canvas");
              }}
            />
            {!canvasInteractive && <div className="osk-message-editor__canvas-lock"><Braces size={17} /><span>Aplique ou descarte o JSON pendente para voltar à edição visual.</span></div>}
          </section>
        ) : (
          <section className="osk-message-editor__editor-view">
            <div className="osk-message-editor__view-head">
              <button type="button" onClick={leaveAuxiliaryView} disabled={Boolean(pendingJsonChanges)} aria-label="Voltar à mensagem"><ChevronLeft size={18} /></button>
              <div>
                <strong>{view === "variables" ? "Variáveis" : view === "json" ? "JSON avançado" : senderSelected ? "Remetente da mensagem" : presentation === "color_panel" && selectedColorSlot ? `Opção ${Math.max(1, (activeColorPanel?.slots.indexOf(selectedColorSlot) ?? 0) + 1)}` : selectedField?.label ?? "Propriedades"}</strong>
                <small>{view === "variables" ? (activeTextField ? `Inserir em ${activeTextField.label}` : "Toque em uma variável para copiar") : view === "json" ? "Edição técnica da mensagem" : senderSelected ? "Nome e avatar usados no envio" : "Configurações do elemento selecionado"}</small>
              </div>
            </div>
            <div className="osk-message-editor__view-body">
              {view === "variables" ? (
                <MessageVariablesPanel variables={variables} insertTargetLabel={activeTextField?.label} onInsert={activeTextField ? handleInsertVariable : undefined} />
              ) : view === "json" ? (
                <MessageJsonEditor
                  value={jsonText}
                  error={jsonError}
                  dirty={jsonDirty}
                  applying={Boolean(pendingJsonChanges)}
                  onChange={handleJsonChange}
                  onApply={() => applyJson(false)}
                  onDiscard={() => { discardJson(); setView("canvas"); }}
                />
              ) : selectedField ? (
                <>
                  {senderSelected && <div className="osk-message-sender-note" data-enabled={senderEnabled || undefined}>
                    <strong>{senderEnabled ? "Webhook ativado" : "Enviado pela Osaka"}</strong>
                    <span>{senderEnabled ? "A identidade abaixo será usada apenas nesta mensagem." : "Ative o webhook para escolher outro nome e avatar."}</span>
                  </div>}
                  <MessageVisualEditor
                  fields={inspectorFields}
                  baseline={baseline}
                  draft={draft}
                  guildOptions={guildOptions}
                  onChange={handleFieldChange}
                  selectedFieldId={selectedFieldId}
                  selectedColorSlot={selectedColorSlot}
                  colorSlotIds={colorSlotIds}
                  onColorSlotSelect={(slotNumber) => setSelectedColorSlot(slotNumber)}
                  onFocusField={(field) => {
                    setSelectedFieldId(field.id);
                    if (field.type !== "text" && field.type !== "textarea") return;
                    setActiveTextFieldId(field.id);
                    if (textSelectionRef.current?.fieldId !== field.id) textSelectionRef.current = null;
                  }}
                  onTextSelection={handleTextSelection}
                />
                </>
              ) : (
                <div className="osk-message-empty">Selecione um elemento da mensagem para abrir suas propriedades.</div>
              )}
            </div>
          </section>
        )}
      </div>

      {activeEditingField ? (
        <div className="osk-message-editor__text-dock">
          <div className="osk-message-editor__text-dock-copy">
            <strong>{activeEditingField.label}</strong>
            {activeEditingField.maxLength ? <span>{activeEditingValue.length}/{activeEditingField.maxLength}</span> : null}
          </div>
          <div className="osk-message-editor__text-dock-tools" aria-label="Formatação de texto">
            <button type="button" onMouseDown={preventToolbarBlur} onClick={() => wrapText("**", "**", "texto")} title="Negrito"><Bold size={16} /></button>
            <button type="button" onMouseDown={preventToolbarBlur} onClick={() => wrapText("*", "*", "texto")} title="Itálico"><Italic size={16} /></button>
            <button type="button" onMouseDown={preventToolbarBlur} onClick={() => wrapText("~~", "~~", "texto")} title="Tachado"><Strikethrough size={16} /></button>
            <button type="button" onMouseDown={preventToolbarBlur} onClick={() => wrapText("`", "`", "código")} title="Código"><Code2 size={16} /></button>
            <button type="button" onMouseDown={preventToolbarBlur} onClick={() => wrapText("[", "](https://)", "texto do link")} title="Link"><Link2 size={16} /></button>
            {variables?.items.length ? <button type="button" onMouseDown={preventToolbarBlur} onClick={openVariables} title="Inserir variável"><Variable size={16} /></button> : null}
          </div>
          <button type="button" className="osk-message-editor__text-done" onClick={() => setEditingFieldId(null)}><Check size={16} />Concluir</button>
        </div>
      ) : (
        <footer className="osk-message-editor__footer">
          <button type="button" className="osk-secondary-button" onClick={() => requestClose("discard")}>Descartar</button>
          <button type="button" className="osk-primary-button" disabled={applyDisabled} onClick={handleApply}>{pendingJsonChanges ? "Aplicando..." : localDirty || jsonDirty ? "Aplicar ao painel" : "Concluir"}</button>
        </footer>
      )}
    </div>
  </div>;

  return createPortal(editor, document.body);
}
