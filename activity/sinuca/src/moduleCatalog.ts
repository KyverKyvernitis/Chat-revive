import type { LucideIcon } from "lucide-react";
import { Cake, ClipboardList, DoorOpen, Palette, Settings, Ticket, Volume2 } from "lucide-react";
import type { DashboardSectionSummary } from "./types/dashboard";

export type ModuleGroup = "main" | "system";

export interface ModuleVisualMeta {
  id: string;
  label: string;
  description: string;
  icon: LucideIcon;
  group: ModuleGroup;
  aliases?: string[];
}

export const MODULE_CATALOG: ModuleVisualMeta[] = [
  { id: "welcome", label: "Boas-vindas", description: "Mensagens, cargos automáticos e aparência", icon: DoorOpen, group: "main" },
  { id: "forms", label: "Formulários", description: "Perguntas, respostas e fluxo de aprovação", icon: ClipboardList, group: "main", aliases: ["form", "formularios"] },
  { id: "tickets", label: "Tickets", description: "Painéis, atendimento e permissões da equipe", icon: Ticket, group: "main", aliases: ["ticket"] },
  { id: "color_roles", label: "Cargos de cor", description: "Painéis para escolher e organizar cores", icon: Palette, group: "main", aliases: ["colors", "colorroles", "color-roles"] },
  { id: "birthday", label: "Aniversários", description: "Cadastro, calendário e avisos automáticos", icon: Cake, group: "main", aliases: ["birthdays"] },
  { id: "tts", label: "Texto pra Voz", description: "Vozes, idiomas e regras de leitura", icon: Volume2, group: "main" },
  { id: "general", label: "Geral", description: "Preferências básicas do bot neste servidor.", icon: Settings, group: "system", aliases: ["guild"] },
];

export type DashboardVisualModule = DashboardSectionSummary & {
  icon: LucideIcon;
  group: ModuleGroup;
  available: boolean;
};

export function normalizeModuleId(id: string): string {
  return id.replace(/-/g, "_").toLowerCase();
}

export function findModuleMeta(sectionId: string | null | undefined): ModuleVisualMeta | undefined {
  const normalized = normalizeModuleId(sectionId || "");
  return MODULE_CATALOG.find((item) => item.id === normalized || item.aliases?.some((alias) => normalizeModuleId(alias) === normalized));
}

export function mergeDashboardModules(summary: DashboardSectionSummary[]): DashboardVisualModule[] {
  const summaryById = new Map(summary.map((item) => [normalizeModuleId(item.id), item]));
  const modules = MODULE_CATALOG.flatMap((meta) => {
    const found = summaryById.get(meta.id) ?? meta.aliases?.map((alias) => summaryById.get(normalizeModuleId(alias))).find(Boolean);
    if (!found) return [];
    return [{ ...found, icon: meta.icon, group: meta.group, available: true } satisfies DashboardVisualModule];
  });
  const known = new Set(modules.map((item) => normalizeModuleId(item.id)));
  const extras = summary
    .filter((item) => !known.has(normalizeModuleId(item.id)))
    .map((item) => ({ ...item, icon: Settings, group: "system" as const, available: true }));
  return [...modules, ...extras];
}

export function guildInitials(name: string): string {
  return name.split(/\s+/).map((part) => part[0]).filter(Boolean).slice(0, 2).join("").toUpperCase() || "S";
}
