import { Check, Copy, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import type { DashboardTemplateVariables } from "../../types/dashboard";
import { formatTemplateVariable } from "./messageEditorUtils";

interface MessageVariablesPanelProps {
  variables?: DashboardTemplateVariables;
  insertTargetLabel?: string;
  onInsert?(key: string): void;
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

export function MessageVariablesPanel({ variables, insertTargetLabel, onInsert }: MessageVariablesPanelProps) {
  const [query, setQuery] = useState("");
  const [copied, setCopied] = useState<string | null>(null);

  const filtered = useMemo(() => {
    if (!variables) return [];
    const needle = query.trim().toLocaleLowerCase("pt-BR");
    if (!needle) return variables.items;
    return variables.items.filter((item) => {
      const formatted = formatTemplateVariable(variables.syntax, item.key);
      return `${item.key} ${item.label} ${formatted}`.toLocaleLowerCase("pt-BR").includes(needle);
    });
  }, [query, variables]);

  if (!variables || variables.items.length === 0) {
    return (
      <div className="osk-message-empty">
        Nenhuma variável foi fornecida para este grupo.
      </div>
    );
  }

  async function handleCopy(key: string) {
    const formatted = formatTemplateVariable(variables!.syntax, key);
    try {
      await copyText(formatted);
      setCopied(key);
      window.setTimeout(() => setCopied((current) => current === key ? null : current), 1400);
    } catch {
      setCopied(null);
    }
  }

  return (
    <div className="osk-message-variables">
      <label className="osk-message-variables__search">
        <Search size={16} />
        <input
          type="text"
          aria-label="Buscar variável"
          value={query}
          placeholder="Buscar variável"
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>

      {insertTargetLabel && <div className="osk-message-variables__target">A variável será inserida na posição do cursor em <strong>{insertTargetLabel}</strong>.</div>}

      <div className="osk-message-variables__list">
        {filtered.map((item) => {
          const formatted = formatTemplateVariable(variables.syntax, item.key);
          const wasCopied = copied === item.key;
          return (
            <button
              type="button"
              key={item.key}
              className="osk-message-variable"
              onClick={() => {
                if (onInsert) {
                  onInsert(item.key);
                  setCopied(item.key);
                  window.setTimeout(() => setCopied((current) => current === item.key ? null : current), 1400);
                } else {
                  void handleCopy(item.key);
                }
              }}
            >
              <span>
                <code>{formatted}</code>
                <small>{item.label}</small>
              </span>
              {wasCopied ? <Check size={16} /> : onInsert ? <Plus size={16} /> : <Copy size={16} />}
            </button>
          );
        })}
        {filtered.length === 0 && (
          <div className="osk-message-empty">Nenhuma variável encontrada.</div>
        )}
      </div>
    </div>
  );
}
