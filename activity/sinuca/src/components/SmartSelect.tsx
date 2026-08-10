import { Check, ChevronDown, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type TransitionEvent } from "react";
import { createPortal } from "react-dom";

export interface SmartSelectOption {
  value: string;
  label: string;
  hint?: string;
  disabled?: boolean;
}

interface SmartSelectProps {
  value: string;
  options: SmartSelectOption[];
  onChange(value: string): void;
  placeholder?: string;
  emptyLabel?: string;
  disabled?: boolean;
  id?: string;
  ariaLabel?: string;
}

const SELECT_TRANSITION_MS = 220;

export function SmartSelect({ value, options, onChange, placeholder, emptyLabel, disabled, id, ariaLabel }: SmartSelectProps) {
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const [position, setPosition] = useState<{ left: number; top: number; width: number; placement: "above" | "below" }>({ left: 0, top: 0, width: 220, placement: "below" });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const selected = options.find((option) => option.value === value) ?? null;
  const searchable = options.length > 8;
  const filteredOptions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("pt-BR");
    if (!normalized) return options;
    return options.filter((option) => `${option.label} ${option.hint || ""}`.toLocaleLowerCase("pt-BR").includes(normalized));
  }, [options, query]);

  const firstEnabledIndex = useCallback((items: SmartSelectOption[]) => items.findIndex((option) => !option.disabled), []);

  const adjacentEnabledIndex = useCallback((items: SmartSelectOption[], current: number, direction: -1 | 1) => {
    if (!items.length) return -1;
    let index = current;
    for (let attempts = 0; attempts < items.length; attempts += 1) {
      index += direction;
      if (index < 0 || index >= items.length) return current;
      if (!items[index]?.disabled) return index;
    }
    return current;
  }, []);

  const updatePosition = useCallback(() => {
    const rect = rootRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.max(220, rect.width);
    const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8));
    const measuredHeight = popoverRef.current?.getBoundingClientRect().height || Math.min(420, Math.max(220, options.length * 52 + (options.length > 8 ? 64 : 0)));
    const roomBelow = window.innerHeight - rect.bottom - 8;
    const roomAbove = rect.top - 8;
    const placement = roomBelow < Math.min(measuredHeight, 260) && roomAbove > roomBelow ? "above" : "below";
    const top = placement === "above"
      ? Math.max(8, rect.top - measuredHeight - 7)
      : Math.min(window.innerHeight - 8, rect.bottom + 7);
    setPosition({ left, top, width, placement });
  }, [options.length]);

  const finishClose = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    setMounted(false);
    setQuery("");
  }, []);

  const close = useCallback((restoreFocus = true) => {
    setVisible(false);
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = window.setTimeout(finishClose, SELECT_TRANSITION_MS + 70);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
  }, [finishClose]);

  const open = useCallback(() => {
    if (disabled) return;
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    updatePosition();
    setMounted(true);
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => setVisible(true)));
  }, [disabled, updatePosition]);

  useEffect(() => {
    if (!mounted) return;
    function handlePointerDown(event: MouseEvent | TouchEvent) {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !popoverRef.current?.contains(target)) close(false);
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown, { passive: true });
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [close, mounted, updatePosition]);

  useEffect(() => {
    if (!mounted || !window.matchMedia("(max-width: 720px)").matches) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previousOverflow; };
  }, [mounted]);

  useEffect(() => {
    if (!visible) return;
    const idx = filteredOptions.findIndex((option) => option.value === value);
    setActiveIndex(idx >= 0 && !filteredOptions[idx]?.disabled ? idx : firstEnabledIndex(filteredOptions));
    const focusTimer = window.setTimeout(() => {
      updatePosition();
      if (searchable) searchRef.current?.focus();
      else listRef.current?.focus();
      listRef.current?.querySelector<HTMLElement>('[data-selected="true"]')?.scrollIntoView({ block: "nearest" });
    }, 40);
    return () => window.clearTimeout(focusTimer);
  }, [filteredOptions, firstEnabledIndex, searchable, updatePosition, value, visible]);

  useEffect(() => () => {
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
  }, []);

  function commit(optionValue: string) {
    if (options.find((option) => option.value === optionValue)?.disabled) return;
    onChange(optionValue);
    close();
  }

  function handleOptionsKeyDown(event: KeyboardEvent<HTMLInputElement | HTMLUListElement>) {
    if (event.key === "Tab") {
      close(false);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((idx) => adjacentEnabledIndex(filteredOptions, idx < 0 ? -1 : idx, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((idx) => adjacentEnabledIndex(filteredOptions, idx < 0 ? filteredOptions.length : idx, -1));
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveIndex(firstEnabledIndex(filteredOptions));
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveIndex(adjacentEnabledIndex(filteredOptions, filteredOptions.length, -1));
    } else if (event.key === "Enter" || (event.key === " " && event.currentTarget.tagName !== "INPUT")) {
      event.preventDefault();
      const option = filteredOptions[activeIndex];
      if (option && !option.disabled) commit(option.value);
    }
  }

  function handleTransitionEnd(event: TransitionEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget || visible) return;
    if (event.propertyName === "opacity" || event.propertyName === "transform") finishClose();
  }

  const listboxId = id ? `${id}-listbox` : undefined;
  const layerStyle = {
    "--osk-select-left": `${position.left}px`,
    "--osk-select-top": `${position.top}px`,
    "--osk-select-width": `${position.width}px`,
  } as CSSProperties;

  const activeDescendant = id && activeIndex >= 0 && filteredOptions[activeIndex] ? `${id}-opt-${activeIndex}` : undefined;
  const layer = mounted ? <div className="osk-select-layer" data-visible={visible || undefined} data-placement={position.placement} style={layerStyle} onTransitionEnd={handleTransitionEnd}>
    <button type="button" className="osk-select-backdrop" onClick={() => close()} aria-label="Fechar opções" tabIndex={visible ? 0 : -1} />
    <div className="osk-select-popover" ref={popoverRef} role="presentation" data-placement={position.placement}>
      <span className="osk-select-sheet-handle" aria-hidden="true" />
      <header className="osk-select-mobile-header">
        <strong>Escolha uma opção</strong>
        <button type="button" className="osk-select-close" onClick={() => close()} aria-label="Fechar"><X size={18} /></button>
      </header>
      {searchable && <label className="osk-select-search"><Search size={15} /><input ref={searchRef} role="combobox" aria-label={`Buscar em ${ariaLabel || "opções"}`} aria-autocomplete="list" aria-controls={listboxId} aria-expanded={visible} aria-activedescendant={activeDescendant} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={handleOptionsKeyDown} placeholder="Buscar..." /></label>}
      <ul className="osk-select-menu" role="listbox" aria-label={ariaLabel || "Opções"} id={listboxId} ref={listRef} tabIndex={searchable ? -1 : 0} aria-activedescendant={activeDescendant} onKeyDown={handleOptionsKeyDown}>
        {filteredOptions.length === 0 && <li className="osk-select-empty" role="presentation">{emptyLabel ?? "Nenhuma opção disponível"}</li>}
        {filteredOptions.map((option, index) => <li key={option.value || "__empty"} id={id ? `${id}-opt-${index}` : undefined} role="option" aria-selected={option.value === value} aria-disabled={option.disabled || undefined} className="osk-select-option" data-active={!option.disabled && index === activeIndex || undefined} data-selected={option.value === value || undefined} data-disabled={option.disabled || undefined} onMouseEnter={() => { if (!option.disabled) setActiveIndex(index); }} onClick={() => { if (!option.disabled) commit(option.value); }}>
          <span className="osk-select-option-text"><span>{option.label}</span>{option.hint && <small>{option.hint}</small>}</span>
          {option.value === value && <Check size={14} aria-hidden="true" />}
        </li>)}
      </ul>
    </div>
  </div> : null;

  return <div className="osk-select" data-open={visible || undefined} data-disabled={disabled || undefined} ref={rootRef}>
    <button ref={triggerRef} type="button" id={id} className="osk-select-trigger" aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={visible} aria-controls={listboxId} disabled={disabled} onClick={() => visible ? close() : open()}>
      <span className="osk-select-value">{selected ? selected.label : <span className="osk-select-placeholder">{placeholder ?? "Selecione"}</span>}</span>
      <ChevronDown size={16} className="osk-select-chev" aria-hidden="true" />
    </button>
    {layer && createPortal(layer, document.body)}
  </div>;
}
