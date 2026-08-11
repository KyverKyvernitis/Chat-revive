export type DashboardFieldType =
  | "boolean"
  | "text"
  | "textarea"
  | "number"
  | "channel"
  | "role"
  | "role_multi"
  | "select"
  | "color"
  | "url"
  | "string_list"
  | "form_fields"
  | "color_slots"
  | "color_panel_layout";

export interface DashboardFieldOption {
  value: string;
  label: string;
}

export type DashboardTemplateSyntax = "curly" | "dollar_curly";

export interface DashboardTemplateVariable {
  key: string;
  label: string;
}

export interface DashboardTemplateVariables {
  syntax: DashboardTemplateSyntax;
  items: DashboardTemplateVariable[];
}

export type DashboardMessageEditorPresentation = "adaptive" | "components_v2" | "generic" | "color_panel";

export interface DashboardMessageEditorDefinition {
  id: string;
  label: string;
  description?: string;
  fieldIds: string[];
  senderFieldIds?: string[];
  presentation?: DashboardMessageEditorPresentation;
  variables?: DashboardTemplateVariables;
}

export interface DashboardGroupMetadata {
  kind?: "message";
  variables?: DashboardTemplateVariables;
  settingsFieldIds?: string[];
  editors?: DashboardMessageEditorDefinition[];
}

export interface DashboardFieldDefinition {
  id: string;
  label: string;
  description?: string;
  type: DashboardFieldType;
  scope: "guild" | "welcome" | "birthday";
  path: string;
  placeholder?: string;
  min?: number;
  max?: number;
  maxLength?: number;
  options?: DashboardFieldOption[];
  group?: string;
}

export interface DashboardSectionDefinition {
  id: string;
  label: string;
  emoji: string;
  description: string;
  groups?: string[];
  groupMetadata?: Record<string, DashboardGroupMetadata>;
  fields: DashboardFieldDefinition[];
  actions?: Array<{ id: string; label: string; description?: string; group?: string }>;
}

export interface DashboardSectionSummary {
  id: string;
  label: string;
  emoji: string;
  description: string;
  enabled: boolean | null;
  state?: "active" | "inactive";
  configured: number;
  total: number;
  status: string;
  issues?: string[];
}

export interface DashboardSettingsPayload {
  ok: boolean;
  guildId: string;
  sections: DashboardSectionDefinition[];
  values: Record<string, unknown>;
  error?: string;
}

export interface DashboardSummaryPayload {
  ok: boolean;
  guildId: string;
  sections: DashboardSectionSummary[];
  error?: string;
}

export interface DashboardBootstrapPayload {
  ok: boolean;
  guild_id?: string;
  user?: DashboardUserPayload;
  bot?: DashboardUserPayload | null;
  sections?: Array<{ id: string; label: string; emoji: string; description: string }>;
  error?: string;
}

export interface DashboardUserPayload {
  id: string;
  username?: string | null;
  global_name?: string | null;
  avatar?: string | null;
  avatarUrl?: string | null;
}


export interface DashboardSupportServerPayload {
  id: string;
  name: string;
  icon: string | null;
  inviteUrl: string;
}

export interface DashboardSessionPayload {
  ok: boolean;
  authenticated?: boolean;
  user?: DashboardUserPayload | null;
  error?: string;
}

export interface DashboardServerCard {
  id: string;
  name: string;
  icon: string | null;
  owner: boolean;
  permissions: string;
  botPresent: boolean;
  canManage: boolean;
  canInvite: boolean;
  reason: string;
  inviteUrl?: string | null;
}

export interface DashboardServersPayload {
  ok: boolean;
  user?: DashboardUserPayload | null;
  manageable: DashboardServerCard[];
  needsInvite: DashboardServerCard[];
  error?: string;
}

export interface DashboardInvitePayload {
  ok: boolean;
  guild_id?: string;
  invite_url?: string;
  error?: string;
}

export interface DashboardChannelOption {
  id: string;
  name: string;
  type: number;
  parentId?: string | null;
  permissionsKnown?: boolean;
  viewable?: boolean;
  sendable?: boolean;
  connectable?: boolean;
  manageable?: boolean;
  webhookManageable?: boolean;
}

export interface DashboardRoleOption {
  id: string;
  name: string;
  color?: number;
  managed?: boolean;
  position?: number;
  assignable?: boolean;
}

export interface DashboardOptionsPayload {
  ok: boolean;
  guildId?: string;
  channels: DashboardChannelOption[];
  roles: DashboardRoleOption[];
  error?: string;
}

export interface DashboardFullPayload {
  ok: boolean;
  guildId: string;
  user?: DashboardUserPayload;
  bot?: DashboardUserPayload | null;
  sections: DashboardSectionDefinition[];
  values: Record<string, unknown>;
  summary: DashboardSectionSummary[];
  options: DashboardOptionsPayload;
  error?: string;
}

export interface DashboardCommandCategory {
  key: string;
  label: string;
  emoji: string;
  description: string;
}

export interface DashboardCommandEntry {
  key: string;
  category: string;
  group: string;
  description: string;
  usage: string;
  aliases: string[];
  keywords: string[];
}

export interface DashboardCommandsPayload {
  ok: boolean;
  guildId: string;
  catalogVersion: number;
  musicAvailable: boolean;
  categories: DashboardCommandCategory[];
  commands: DashboardCommandEntry[];
  error?: string;
}

export interface DashboardFormField {
  id: string;
  label: string;
  placeholder: string;
  response_label: string;
  required: boolean;
  long: boolean;
  show_in_response: boolean;
  enabled: boolean;
  min_length: number;
  max_length: number;
}

export interface DashboardColorSlot {
  number: number;
  name: string;
  text_hex: string;
  role_hex: string;
  role_id: string | number;
  role_name: string;
  managed: boolean;
}

export interface DashboardColorPanel {
  id: string;
  slots: number[];
}
