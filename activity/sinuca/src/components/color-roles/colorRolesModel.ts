import type {
  DashboardColorPanel,
  DashboardColorSlot,
  DashboardOptionsPayload,
} from "../../types/dashboard";

export const COLOR_PANEL_MAX = 3;
export const COLOR_PANEL_OPTION_MAX = 10;
export const COLOR_SLOT_MAX = 30;

const COLOR_PRESET_ROLE_HEX: Record<number, string> = {
  1: "#8B0000", 2: "#B8860B", 3: "#006400", 4: "#00008B", 5: "#C71585",
  6: "#800080", 7: "#FF8C00", 8: "#A0522D", 9: "#008B8B", 10: "#1F1F1F",
  11: "#FF0000", 12: "#FFD700", 13: "#00FF00", 14: "#1E90FF", 15: "#FF69B4",
  16: "#9370DB", 17: "#FFA500", 18: "#F5DEB3", 19: "#00FFFF", 20: "#808080",
  21: "#FF7F7F", 22: "#FFF68F", 23: "#90EE90", 24: "#87CEFA", 25: "#FFB6C1",
  26: "#D8BFD8", 27: "#FFCC99", 28: "#F5F5DC", 29: "#E0FFFF", 30: "#FFFFFF",
};

function normalizedHex(value: unknown): string | null {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  const normalized = raw.startsWith("#") ? raw : `#${raw}`;
  return /^#[0-9a-f]{6}$/i.test(normalized) ? normalized.toUpperCase() : null;
}

export function presetColorRoleHex(slotNumber: number): string {
  return COLOR_PRESET_ROLE_HEX[Math.max(1, Math.min(COLOR_SLOT_MAX, Math.trunc(slotNumber || 1)))] ?? "#5865F2";
}

function uniqueId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createColorPanelId(): string {
  return uniqueId("panel");
}

export function defaultColorPanelLayout(): DashboardColorPanel[] {
  return [0, 1, 2].map((index) => ({
    id: `panel-${index + 1}`,
    slots: Array.from({ length: 10 }, (_, offset) => index * 10 + offset + 1),
  }));
}

export function normalizeColorPanelLayout(raw: unknown): DashboardColorPanel[] {
  const source = Array.isArray(raw) ? raw : defaultColorPanelLayout();
  const used = new Set<number>();
  const result: DashboardColorPanel[] = [];

  source.slice(0, COLOR_PANEL_MAX).forEach((item, panelIndex) => {
    const object = item && typeof item === "object" && !Array.isArray(item)
      ? item as Record<string, unknown>
      : {};
    const slots = Array.isArray(object.slots) ? object.slots : [];
    const normalizedSlots: number[] = [];
    for (const candidate of slots) {
      const number = Math.trunc(Number(candidate));
      if (number < 1 || number > COLOR_SLOT_MAX || used.has(number)) continue;
      used.add(number);
      normalizedSlots.push(number);
      if (normalizedSlots.length >= COLOR_PANEL_OPTION_MAX) break;
    }
    if (!normalizedSlots.length) return;
    const requestedId = String(object.id || "").trim().slice(0, 80);
    let id = requestedId && !result.some((panel) => panel.id === requestedId)
      ? requestedId
      : `panel-${panelIndex + 1}`;
    let suffix = 2;
    while (result.some((panel) => panel.id === id)) {
      id = `panel-${panelIndex + 1}-${suffix}`;
      suffix += 1;
    }
    result.push({ id, slots: normalizedSlots });
  });

  if (result.length) return result;
  return [{ id: "panel-1", slots: [1] }];
}

export function allUsedColorSlots(layout: DashboardColorPanel[]): number[] {
  return layout.flatMap((panel) => panel.slots);
}

export function nextUnusedColorSlot(layout: DashboardColorPanel[]): number | null {
  const used = new Set(allUsedColorSlots(layout));
  for (let number = 1; number <= COLOR_SLOT_MAX; number += 1) {
    if (!used.has(number)) return number;
  }
  return null;
}

export function colorPanelFromEditorId(layout: DashboardColorPanel[], editorId: string): DashboardColorPanel | null {
  const prefix = "color-panel:";
  if (editorId.startsWith(prefix)) {
    const id = decodeURIComponent(editorId.slice(prefix.length));
    return layout.find((panel) => panel.id === id) ?? null;
  }
  const legacyMatch = editorId.match(/^color-panel-(\d+)$/);
  if (legacyMatch) return layout[Number(legacyMatch[1]) - 1] ?? null;
  return null;
}

export function colorPanelEditorId(panel: DashboardColorPanel): string {
  return `color-panel:${encodeURIComponent(panel.id)}`;
}

export function colorRoleHex(
  slot: DashboardColorSlot | undefined,
  guildOptions?: DashboardOptionsPayload | null,
): string {
  const roleId = String(slot?.role_id || "").trim();
  const hasRoleId = Boolean(roleId && roleId !== "0");
  const role = hasRoleId ? guildOptions?.roles.find((item) => item.id === roleId) : undefined;
  const liveColor = Number(role?.color || 0);
  if (role && Number.isFinite(liveColor)) {
    if (liveColor > 0) return `#${liveColor.toString(16).padStart(6, "0").slice(-6)}`.toUpperCase();
    return "#99AAB5";
  }

  const slotNumber = Math.max(1, Math.min(COLOR_SLOT_MAX, Math.trunc(Number(slot?.number || 1))));
  const preset = presetColorRoleHex(slotNumber);
  const roleHex = normalizedHex(slot?.role_hex);
  const textHex = normalizedHex(slot?.text_hex);

  // Versões antigas do editor criavam novas opções com ambos os campos em branco.
  // Sem um cargo real, esse branco era apenas placeholder e deve voltar ao preset.
  const placeholderWhite = !hasRoleId
    && slotNumber !== 30
    && roleHex === "#FFFFFF"
    && textHex === "#FFFFFF";
  if (placeholderWhite) return preset;

  return roleHex ?? textHex ?? preset;
}

export function colorRoleLabel(slot: DashboardColorSlot | undefined, guildOptions?: DashboardOptionsPayload | null): string {
  const roleId = String(slot?.role_id || "");
  if (!roleId || roleId === "0") return slot?.managed ? "Cargo do preset" : "Cargo não selecionado";
  const role = guildOptions?.roles.find((item) => item.id === roleId);
  return `@${role?.name || slot?.role_name || roleId}`;
}

export function updatePanelSlots(
  layout: DashboardColorPanel[],
  panelId: string,
  updater: (slots: number[]) => number[],
): DashboardColorPanel[] {
  return layout.map((panel) => panel.id === panelId
    ? { ...panel, slots: updater([...panel.slots]).slice(0, COLOR_PANEL_OPTION_MAX) }
    : panel);
}
