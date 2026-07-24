import type {
  DashboardFieldDefinition,
  DashboardMessageEditorPresentation,
  DashboardOptionsPayload,
  DashboardTemplateVariables,
} from "../../types/dashboard";

export type MessageEditorMode = "content" | "appearance" | "components" | "variables" | "json";

export interface MessageEditorProps {
  editorId: string;
  sectionId: string;
  sectionLabel: string;
  groupLabel: string;
  description?: string;
  fields: DashboardFieldDefinition[];
  senderFieldIds?: string[];
  presentation?: DashboardMessageEditorPresentation;
  baseline: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  botName?: string;
  botAvatarUrl?: string | null;
  guildName?: string;
  guildAvatarUrl?: string | null;
  variables?: DashboardTemplateVariables;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onApply(): void;
  onDiscard(): void;
}

export interface JsonFieldChange {
  field: DashboardFieldDefinition;
  raw: unknown;
  expected: unknown;
}
