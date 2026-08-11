import { Clock3, Command, Globe2, LocateFixed, Settings2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { DashboardFieldDefinition, DashboardOptionsPayload, DashboardSectionDefinition } from "../types/dashboard";
import { DashboardFieldControl } from "./DashboardFieldControl";
import { SmartAvatar } from "./SmartAvatar";
import { SmartSelect, type SmartSelectOption } from "./SmartSelect";

interface GeneralPageProps {
  section: DashboardSectionDefinition;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  guildName: string;
  guildIcon?: string | null;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
}

const FALLBACK_TIMEZONES = [
  "America/Sao_Paulo", "America/Manaus", "America/Belem", "America/Fortaleza", "America/Recife",
  "America/Bahia", "America/Cuiaba", "America/Campo_Grande", "America/Porto_Velho", "America/Rio_Branco",
  "America/Noronha", "America/Argentina/Buenos_Aires", "America/Santiago", "America/Bogota", "America/Lima",
  "America/Mexico_City", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "Europe/Lisbon", "Europe/London", "Europe/Madrid", "Europe/Paris", "Europe/Berlin", "Europe/Rome",
  "Africa/Luanda", "Africa/Maputo", "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai", "Asia/Kolkata",
  "Australia/Sydney", "Pacific/Auckland", "UTC",
];

function browserTimeZones(): string[] {
  const intl = Intl as typeof Intl & { supportedValuesOf?: (key: "timeZone") => string[] };
  try {
    const supported = intl.supportedValuesOf?.("timeZone") ?? [];
    return supported.length ? supported : FALLBACK_TIMEZONES;
  } catch {
    return FALLBACK_TIMEZONES;
  }
}

function zoneLabel(timeZone: string): string {
  const parts = timeZone.split("/").map((part) => part.replace(/_/g, " "));
  return parts.length > 1 ? `${parts[parts.length - 1]} — ${parts.slice(0, -1).join(" / ")}` : parts[0];
}

function timeZoneOptions(current: string, detected: string): SmartSelectOption[] {
  const priority = [current, detected, "America/Sao_Paulo"];
  const unique = Array.from(new Set([...priority, ...FALLBACK_TIMEZONES, ...browserTimeZones()].filter(Boolean)));
  return unique.map((timeZone) => ({ value: timeZone, label: zoneLabel(timeZone), hint: timeZone }));
}

function formatServerTime(timeZone: string, date: Date): string {
  try {
    return new Intl.DateTimeFormat("pt-BR", {
      timeZone,
      weekday: "long",
      day: "2-digit",
      month: "long",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(date);
  } catch {
    return "Fuso horário inválido";
  }
}

function valuesEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function GeneralPage({ section, values, draft, guildOptions, guildName, guildIcon, onChange }: GeneralPageProps) {
  const prefixField = section.fields.find((field) => field.id === "general.bot_prefix") ?? null;
  const timezoneField = section.fields.find((field) => field.id === "general.timezone") ?? null;
  const otherFields = section.fields.filter((field) => !["general.bot_prefix", "general.timezone"].includes(field.id));
  const timezone = String(draft["general.timezone"] || "America/Sao_Paulo");
  const detectedTimezone = useMemo(() => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Sao_Paulo"; }
    catch { return "America/Sao_Paulo"; }
  }, []);
  const timezoneChoices = useMemo(() => timeZoneOptions(timezone, detectedTimezone), [detectedTimezone, timezone]);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  return <section className="osk-dashboard-page osk-general-page">
    <header className="osk-general-hero">
      <div className="osk-general-identity">
        <SmartAvatar className="osk-general-guild-avatar" src={guildIcon} name={guildName} type="server" alt={`Ícone de ${guildName}`} size={56} loading="eager" />
        <div><span className="osk-kicker">Configuração do servidor</span><h1>Geral</h1><p>Preferências que orientam todo o painel da {guildName}.</p></div>
      </div>
      <span className="osk-general-mark" aria-hidden="true"><Settings2 size={24} /></span>
    </header>

    <div className="osk-general-grid">
      <article className="osk-general-card" data-changed={prefixField && !valuesEqual(values[prefixField.id], draft[prefixField.id]) || undefined}>
        <header><span><Command size={20} /></span><div><h2>Comandos e interação</h2><p>Defina como os comandos por mensagem começam neste servidor.</p></div></header>
        {prefixField && <div className="osk-general-control"><span className="osk-general-control-label">{prefixField.label}</span><DashboardFieldControl field={prefixField} value={draft[prefixField.id]} guildOptions={guildOptions} onChange={onChange} /></div>}
        <small className="osk-general-footnote">A página Comandos usa esse prefixo automaticamente.</small>
      </article>

      <article className="osk-general-card osk-general-time-card" data-changed={timezoneField && !valuesEqual(values[timezoneField.id], draft[timezoneField.id]) || undefined}>
        <header><span><Globe2 size={20} /></span><div><h2>Horário do servidor</h2><p>Uma referência única para calendários e ações programadas.</p></div></header>
        {timezoneField && <div className="osk-general-control">
          <label htmlFor="general-timezone">Fuso horário</label>
          <SmartSelect id="general-timezone" ariaLabel="Fuso horário do servidor" value={timezone} options={timezoneChoices} onChange={(next) => onChange(timezoneField, next)} placeholder="Escolha um fuso horário" emptyLabel="Nenhum fuso encontrado" />
        </div>}
        <div className="osk-time-preview"><Clock3 size={18} /><span><small>Agora no servidor</small><strong>{formatServerTime(timezone, now)}</strong></span></div>
        {timezoneField && detectedTimezone !== timezone && <button type="button" className="osk-detect-timezone" onClick={() => onChange(timezoneField, detectedTimezone)}><LocateFixed size={16} />Usar meu fuso</button>}
      </article>

      {otherFields.length > 0 && <article className="osk-general-card osk-general-other-card">
        <header><span><Settings2 size={20} /></span><div><h2>Outras preferências</h2><p>Ajustes gerais adicionais deste servidor.</p></div></header>
        <div className="osk-compact-fields">{otherFields.map((field) => <div key={field.id} className="osk-compact-field" data-changed={!valuesEqual(values[field.id], draft[field.id]) || undefined}><div className="osk-compact-field-copy"><strong>{field.label}</strong>{field.description && <small>{field.description}</small>}</div><div className="osk-compact-field-control"><DashboardFieldControl field={field} value={draft[field.id]} guildOptions={guildOptions} onChange={onChange} /></div></div>)}</div>
      </article>}
    </div>
  </section>;
}
