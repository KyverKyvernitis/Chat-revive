import { useLayoutEffect, useRef, type ChangeEvent, type FocusEvent, type MouseEvent, type SyntheticEvent } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";

interface MessageInlineTextEditorProps {
  field: DashboardFieldDefinition;
  value: string;
  selection?: { start: number; end: number } | null;
  onChange(value: string): void;
  onSelection(start: number, end: number): void;
  onFinish(): void;
}

export function MessageInlineTextEditor({
  field,
  value,
  selection,
  onChange,
  onSelection,
  onFinish,
}: MessageInlineTextEditorProps) {
  const controlRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);

  useLayoutEffect(() => {
    const control = controlRef.current;
    if (!control) return;
    control.focus({ preventScroll: true });
    const start = Math.max(0, Math.min(value.length, selection?.start ?? value.length));
    const end = Math.max(start, Math.min(value.length, selection?.end ?? start));
    control.setSelectionRange(start, end);
    onSelection(start, end);
    resizeTextarea(control);
  }, []); // O foco inicial deve acontecer somente quando o editor é montado.

  function resizeTextarea(control: HTMLInputElement | HTMLTextAreaElement) {
    if (!(control instanceof HTMLTextAreaElement)) return;
    const viewportHeight = window.visualViewport?.height ?? window.innerHeight;
    const maxHeight = Math.max(112, Math.min(240, Math.round(viewportHeight * 0.34)));
    control.style.height = "auto";
    const nextHeight = Math.max(58, Math.min(control.scrollHeight, maxHeight));
    control.style.height = `${nextHeight}px`;
    control.style.overflowY = control.scrollHeight > maxHeight ? "auto" : "hidden";
  }

  function updateSelection(control: HTMLInputElement | HTMLTextAreaElement) {
    onSelection(control.selectionStart ?? value.length, control.selectionEnd ?? value.length);
  }

  function handleClick(event: MouseEvent<HTMLInputElement | HTMLTextAreaElement>) {
    event.stopPropagation();
  }

  function handleFocus(event: FocusEvent<HTMLInputElement | HTMLTextAreaElement>) {
    updateSelection(event.currentTarget);
  }

  function handleSelect(event: SyntheticEvent<HTMLInputElement | HTMLTextAreaElement>) {
    updateSelection(event.currentTarget);
  }

  function handleChange(event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) {
    const control = event.currentTarget;
    onChange(control.value);
    updateSelection(control);
    resizeTextarea(control);
  }

  if (field.type === "textarea") {
    return (
      <textarea
        ref={(node) => { controlRef.current = node; }}
        className="osk-message-inline-editor osk-message-inline-editor--multiline"
        value={value}
        maxLength={field.maxLength}
        aria-label={field.label}
        placeholder={field.placeholder || field.label}
        data-message-inline-field-id={field.id}
        rows={Math.min(5, Math.max(2, value.split("\n").length))}
        onClick={handleClick}
        onFocus={handleFocus}
        onSelect={handleSelect}
        onChange={handleChange}
        onBlur={onFinish}
        onKeyDown={(event) => {
          if (event.key === "Escape" || (event.key === "Enter" && (event.ctrlKey || event.metaKey))) {
            event.preventDefault();
            onFinish();
          }
        }}
      />
    );
  }

  return (
    <input
      ref={(node) => { controlRef.current = node; }}
      className="osk-message-inline-editor"
      type="text"
      value={value}
      maxLength={field.maxLength}
      aria-label={field.label}
      placeholder={field.placeholder || field.label}
      data-message-inline-field-id={field.id}
      onClick={handleClick}
      onFocus={handleFocus}
      onSelect={handleSelect}
      onChange={handleChange}
      onBlur={onFinish}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === "Escape") {
          event.preventDefault();
          onFinish();
        }
      }}
    />
  );
}
