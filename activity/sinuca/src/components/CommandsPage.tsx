import { Check, ChevronDown, Clipboard, Command, RefreshCw, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchDashboardCommands } from "../transport/dashboardApi";
import type { DashboardCommandCategory, DashboardCommandEntry, DashboardCommandsPayload } from "../types/dashboard";
import { LoadingVisual } from "./VisualTemplates";

interface CommandsPageProps {
  guildId: string;
  refreshToken?: number;
}

function normalizeSearch(value: unknown): string {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("pt-BR")
    .replace(/\s+/g, " ")
    .trim();
}

function commandSearchText(command: DashboardCommandEntry, category: DashboardCommandCategory | undefined): string {
  return normalizeSearch([
    command.key,
    command.usage,
    command.description,
    command.group,
    command.aliases.join(" "),
    command.keywords.join(" "),
    category?.label,
    category?.description,
  ].join(" "));
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  document.execCommand("copy");
  field.remove();
}

export function CommandsPage({ guildId, refreshToken = 0 }: CommandsPageProps) {
  const [payload, setPayload] = useState<DashboardCommandsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const generationRef = useRef(0);
  const copyTimerRef = useRef<number | null>(null);

  const load = useCallback(async (force = false) => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setLoading(true);
    setError("");
    try {
      const result = await fetchDashboardCommands(guildId, force);
      if (generationRef.current !== generation) return;
      setPayload(result);
    } catch (reason) {
      if (generationRef.current !== generation) return;
      setError(reason instanceof Error ? reason.message : "Não foi possível carregar os comandos.");
    } finally {
      if (generationRef.current === generation) setLoading(false);
    }
  }, [guildId]);

  useEffect(() => {
    setPayload(null);
    setQuery("");
    setCategory("all");
    setExpanded(null);
    void load(refreshToken > 0);
    return () => { generationRef.current += 1; };
  }, [load, refreshToken]);

  useEffect(() => () => {
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
  }, []);

  const categoryByKey = useMemo(() => new Map((payload?.categories ?? []).map((item) => [item.key, item])), [payload?.categories]);
  const normalizedQuery = normalizeSearch(query);
  const filtered = useMemo(() => (payload?.commands ?? []).filter((command) => {
    if (category !== "all" && command.category !== category) return false;
    if (!normalizedQuery) return true;
    return commandSearchText(command, categoryByKey.get(command.category)).includes(normalizedQuery);
  }), [category, categoryByKey, normalizedQuery, payload?.commands]);
  const grouped = useMemo(() => (payload?.categories ?? []).map((item) => ({
    category: item,
    commands: filtered.filter((command) => command.category === item.key),
  })).filter((item) => item.commands.length > 0), [filtered, payload?.categories]);

  const handleCopy = useCallback(async (command: DashboardCommandEntry) => {
    try {
      await copyText(command.usage);
      setCopied(command.key);
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => setCopied(null), 1_600);
    } catch {
      setCopied(null);
    }
  }, []);

  return <section className="osk-dashboard-page osk-commands-page">
    <header className="osk-commands-intro">
      <span className="osk-kicker">Referência do usuário</span>
      <span className="osk-commands-title"><span aria-hidden="true"><Command size={25} /></span><span><h1>Comandos</h1><p>Encontre tudo o que usuários comuns podem usar neste servidor.</p></span></span>
    </header>

    <div className="osk-command-discovery">
      <label className="osk-command-search">
        <Search size={20} aria-hidden="true" />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar comando, uso, alias ou categoria" aria-label="Buscar comandos" autoComplete="off" />
        {query && <button type="button" onClick={() => setQuery("")} aria-label="Limpar pesquisa"><X size={17} /></button>}
      </label>
      <nav className="osk-command-categories" aria-label="Categorias de comandos">
        <button type="button" data-active={category === "all" || undefined} onClick={() => setCategory("all")}>Todos</button>
        {(payload?.categories ?? []).map((item) => <button key={item.key} type="button" data-active={category === item.key || undefined} onClick={() => setCategory(item.key)}><span aria-hidden="true">{item.emoji}</span>{item.label}</button>)}
      </nav>
    </div>

    {loading && !payload && <div className="osk-commands-loading" aria-busy="true" aria-label="Carregando comandos"><LoadingVisual size={26} /><span /><span /><span /></div>}
    {error && !payload && <div className="osk-commands-error" role="alert"><p>Não foi possível abrir o catálogo agora.</p><button type="button" onClick={() => void load(true)}><RefreshCw size={16} />Tentar novamente</button></div>}

    {payload && <div className="osk-command-results">
      <span className="osk-sr-only" role="status">{filtered.length} comandos encontrados</span>
      {grouped.map(({ category: item, commands }) => <section key={item.key} className="osk-command-category-section">
        <header><span className="osk-command-category-icon" aria-hidden="true">{item.emoji}</span><div><h2>{item.label}</h2><p>{item.description}</p></div></header>
        <div className="osk-command-list">
          {commands.map((command) => {
            const open = expanded === command.key;
            return <article key={command.key} className="osk-command-card" data-open={open || undefined}>
              <div className="osk-command-card-main">
                <div className="osk-command-syntax"><code>{command.usage}</code><span>{command.group}</span></div>
                <p>{command.description}</p>
              </div>
              <button type="button" className="osk-command-copy" data-copied={copied === command.key || undefined} onClick={() => void handleCopy(command)} aria-label={`Copiar ${command.usage}`}>{copied === command.key ? <Check size={17} /> : <Clipboard size={17} />}<span>{copied === command.key ? "Copiado" : "Copiar"}</span></button>
              {command.aliases.length > 0 && <button type="button" className="osk-command-details" onClick={() => setExpanded(open ? null : command.key)} aria-expanded={open}><span>{open ? "Ocultar aliases" : "Ver aliases"}</span><ChevronDown size={16} /></button>}
              {open && <div className="osk-command-aliases"><small>Também funciona com</small><div>{command.aliases.map((alias) => <code key={alias}>{alias}</code>)}</div></div>}
            </article>;
          })}
        </div>
      </section>)}
      {filtered.length === 0 && <div className="osk-command-empty"><Search size={22} /><h2>Nenhum comando encontrado</h2><p>Tente outro nome, alias ou escolha a categoria Todos.</p><button type="button" onClick={() => { setQuery(""); setCategory("all"); }}>Limpar filtros</button></div>}
    </div>}
  </section>;
}
