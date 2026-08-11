import { Long, MongoClient, type Collection, type Db, type Document } from "mongodb";

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
export type DashboardFieldScope = "guild" | "welcome" | "birthday";

export interface DashboardFieldOption { value: string; label: string }
export type DashboardTemplateSyntax = "curly" | "dollar_curly";
export interface DashboardTemplateVariable { key: string; label: string }
export interface DashboardTemplateVariables { syntax: DashboardTemplateSyntax; items: DashboardTemplateVariable[] }
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
  scope: DashboardFieldScope;
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
}

export interface DashboardGuildSummary {
  guildId: string;
  sections: Array<{
    id: string;
    label: string;
    emoji: string;
    description: string;
    enabled: boolean | null;
    state: "active" | "inactive" | "partial" | "configured" | "pending";
    configured: number;
    total: number;
    status: string;
  }>;
}

export interface DashboardConfigService {
  listSections(): DashboardSectionDefinition[];
  getSummary(guildId: string): Promise<DashboardGuildSummary>;
  getSettings(guildId: string): Promise<{ guildId: string; sections: DashboardSectionDefinition[]; values: Record<string, unknown> }>;
  updateSettings(guildId: string, updates: Record<string, unknown>): Promise<{ ok: true; values: Record<string, unknown>; saved: string[]; revision?: number; changed_sections?: string[] }>;
}

interface CreateDashboardConfigServiceOptions {
  mongoUri: string;
  mongoDbName: string;
  mongoCollectionName: string;
}

const WELCOME_DOC_CONFIG = "welcome_config";
const BIRTHDAY_DOC_CONFIG = "birthday_config";

const TTS_ENGINE_OPTIONS = [
  { value: "gtts", label: "gTTS" },
  { value: "edge", label: "Microsoft Edge" },
];
const TTS_LANGUAGE_OPTIONS = [
  { value: "pt-BR", label: "Português — Brasil" },
  { value: "pt-PT", label: "Português — Portugal" },
  { value: "en-US", label: "Inglês — Estados Unidos" },
  { value: "en-GB", label: "Inglês — Reino Unido" },
  { value: "es-ES", label: "Espanhol — Espanha" },
  { value: "es-MX", label: "Espanhol — México" },
  { value: "fr-FR", label: "Francês — França" },
  { value: "de-DE", label: "Alemão — Alemanha" },
  { value: "it-IT", label: "Italiano — Itália" },
  { value: "ja-JP", label: "Japonês" },
  { value: "ko-KR", label: "Coreano" },
];
const TTS_VOICE_OPTIONS = [
  { value: "pt-BR-FranciscaNeural", label: "Francisca — feminina" },
  { value: "pt-BR-AntonioNeural", label: "Antônio — masculina" },
  { value: "pt-BR-BrendaNeural", label: "Brenda — feminina" },
  { value: "pt-BR-DonatoNeural", label: "Donato — masculina" },
  { value: "pt-BR-ElzaNeural", label: "Elza — feminina" },
  { value: "pt-BR-FabioNeural", label: "Fábio — masculina" },
  { value: "pt-BR-GiovannaNeural", label: "Giovanna — feminina" },
  { value: "pt-BR-HumbertoNeural", label: "Humberto — masculina" },
  { value: "pt-BR-LeilaNeural", label: "Leila — feminina" },
  { value: "pt-BR-LeticiaNeural", label: "Letícia — feminina" },
  { value: "pt-BR-ManuelaNeural", label: "Manuela — feminina" },
  { value: "pt-BR-NicolauNeural", label: "Nicolau — masculina" },
  { value: "pt-BR-YaraNeural", label: "Yara — feminina" },
];
const BUTTON_STYLE_OPTIONS = [
  { value: "primary", label: "Azul" },
  { value: "secondary", label: "Cinza" },
  { value: "success", label: "Verde" },
  { value: "danger", label: "Vermelho" },
];
const TICKET_FLOW_OPTIONS = [
  { value: "confirm_ticket", label: "Confirmar e abrir ticket" },
  { value: "modal_ticket", label: "Formulário e ticket" },
  { value: "modal_channel", label: "Formulário para canal" },
  { value: "direct_ticket", label: "Abrir ticket direto" },
];
const WELCOME_MODE_OPTIONS = [
  { value: "components_v2", label: "Components V2" },
  { value: "embed", label: "Embed" },
  { value: "normal", label: "Mensagem normal" },
];
const WELCOME_STYLE_OPTIONS = [
  { value: "complete", label: "Completo" },
  { value: "simple", label: "Simples" },
  { value: "compact", label: "Compacto" },
];
const WELCOME_COLOR_MODE_OPTIONS = [
  { value: "fixed", label: "Cor fixa" },
  { value: "member_avatar", label: "Combinar com avatar do membro" },
];
const WELCOME_MEDIA_MODE_OPTIONS = [
  { value: "custom", label: "Link personalizado" },
  { value: "avatar_stars", label: "Estrelas pelo avatar" },
];
const WELCOME_WEBHOOK_NAME_OPTIONS = [
  { value: "fixed", label: "Nome personalizado" },
  { value: "server", label: "Nome do servidor" },
  { value: "member", label: "Nome do membro" },
  { value: "inviter", label: "Nome de quem convidou" },
];
const WELCOME_WEBHOOK_AVATAR_OPTIONS = [
  { value: "server", label: "Avatar do servidor" },
  { value: "member", label: "Avatar do membro" },
  { value: "inviter", label: "Avatar de quem convidou" },
  { value: "custom", label: "Avatar por link" },
];
const WELCOME_EMBED_IMAGE_MODE_OPTIONS = [
  { value: "none", label: "Sem imagem" },
  { value: "member", label: "Avatar do membro" },
  { value: "inviter", label: "Avatar de quem convidou" },
  { value: "server", label: "Ícone do servidor" },
  { value: "bot", label: "Avatar do bot" },
  { value: "custom", label: "Link personalizado" },
];
const WELCOME_EMBED_MAIN_IMAGE_MODE_OPTIONS = [
  ...WELCOME_EMBED_IMAGE_MODE_OPTIONS,
  { value: "avatar_stars", label: "Estrelas pelo avatar" },
];
const BIRTHDAY_LEAP_MODE_OPTIONS = [
  { value: "feb28", label: "Avisar em 28/02" },
  { value: "mar01", label: "Avisar em 01/03" },
];

const WELCOME_VARIABLE_HELP: Record<string, string> = {
  membro: "nome exibido do membro",
  membro_mencao: "menção do membro",
  usuario: "nome de usuário",
  usuario_id: "ID do membro",
  membro_id: "ID do membro",
  membro_avatar: "avatar do membro",
  servidor: "nome do servidor",
  servidor_id: "ID do servidor",
  servidor_icone: "ícone do servidor",
  contador: "quantidade atual de membros",
  criado_em: "data de criação da conta",
  criado_relativo: "há quanto tempo a conta foi criada",
  entrou_em: "horário da entrada no servidor",
  convite_codigo: "código do convite usado",
  convite_canal: "nome do canal do convite",
  convite_canal_mencao: "menção do canal do convite",
  convite_usos: "quantidade de usos do convite",
  convidador: "nome de quem convidou",
  convidador_mencao: "menção de quem convidou",
  convidador_avatar: "avatar de quem convidou",
  bot_avatar: "avatar do bot",
};
const BIRTHDAY_VARIABLE_HELP: Record<string, string> = {
  usermention: "menciona o membro",
  userid: "ID do membro",
  username: "nome de usuário",
  userdisplayname: "nome exibido no servidor",
  usermessage: "mensagem enviada na thread",
  birthdayday: "dia do aniversário",
  birthdaymonth: "mês do aniversário",
  birthdayyear: "ano informado",
  birthdaydate: "data no formato dia/mês",
  birthdayage: "idade calculada",
  birthdaycount: "quantidade de aniversariantes",
  birthdaycalendarblock: "bloco do calendário",
  birthdaycalendar: "calendário completo",
  birthdaymentions: "menções dos aniversariantes",
  guildname: "nome do servidor",
  nowtimestamp: "timestamp atual",
  validexample: "exemplo de data válida",
};

function templateVariables(syntax: DashboardTemplateSyntax, keys: string[], labels: Record<string, string>): DashboardTemplateVariables {
  return { syntax, items: keys.map((key) => ({ key, label: labels[key] ?? key })) };
}
const WELCOME_TEMPLATE_VARIABLES = templateVariables("curly", Object.keys(WELCOME_VARIABLE_HELP), WELCOME_VARIABLE_HELP);
const FORM_VARIABLE_HELP: Record<string, string> = {
  user: "menção do membro",
  user_name: "nome exibido do membro",
  user_id: "ID do membro",
  guild: "nome do servidor",
  field1: "resposta da primeira pergunta",
  field2: "resposta da segunda pergunta",
  field3: "resposta da terceira pergunta",
  field4: "resposta da quarta pergunta",
  field5: "resposta da quinta pergunta",
};
const FORM_TEMPLATE_VARIABLES = templateVariables("curly", Object.keys(FORM_VARIABLE_HELP), FORM_VARIABLE_HELP);
const BIRTHDAY_REGISTER_VARIABLES = templateVariables("dollar_curly", [
  "usermention", "userid", "username", "userdisplayname", "birthdayday", "birthdaymonth", "birthdayyear",
  "birthdaydate", "birthdayage", "nowtimestamp", "usermessage", "validexample",
], BIRTHDAY_VARIABLE_HELP);
const BIRTHDAY_ANNOUNCE_VARIABLES = templateVariables("dollar_curly", [
  "usermention", "userid", "username", "userdisplayname", "birthdayday", "birthdaymonth", "birthdayyear",
  "birthdaydate", "birthdayage", "birthdaycount", "birthdaymentions", "nowtimestamp", "guildname",
], BIRTHDAY_VARIABLE_HELP);
const BIRTHDAY_CALENDAR_VARIABLES = templateVariables("dollar_curly", [
  "guildname", "birthdaycount", "birthdaycalendarblock", "birthdaycalendar", "nowtimestamp",
], BIRTHDAY_VARIABLE_HELP);

const DEFAULT_COLOR_SLOT_DATA: Array<[number, string, string, string]> = [
  [1, "Vermelho escuro", "#B11212", "#8B0000"], [2, "Amarelo escuro", "#C9A31A", "#B8860B"],
  [3, "Verde escuro", "#0B5D30", "#006400"], [4, "Azul escuro", "#1737D8", "#00008B"],
  [5, "Rosa escuro", "#D61EA6", "#C71585"], [6, "Roxo escuro", "#9A0EC7", "#800080"],
  [7, "Laranja escuro", "#D98900", "#FF8C00"], [8, "Bege escuro", "#B96D43", "#A0522D"],
  [9, "Ciano escuro", "#008F98", "#008B8B"], [10, "Preto", "#000000", "#1F1F1F"],
  [11, "Vermelho", "#FF1B1B", "#FF0000"], [12, "Amarelo", "#FFEC1A", "#FFD700"],
  [13, "Verde", "#11B611", "#00FF00"], [14, "Azul", "#0E2FFF", "#1E90FF"],
  [15, "Rosa", "#FF62C3", "#FF69B4"], [16, "Roxo", "#C020FF", "#9370DB"],
  [17, "Laranja", "#FFAD13", "#FFA500"], [18, "Bege", "#D6B694", "#F5DEB3"],
  [19, "Ciano", "#00ECFF", "#00FFFF"], [20, "Cinza", "#8F8F8F", "#808080"],
  [21, "Vermelho claro", "#FF8B8B", "#FF7F7F"], [22, "Amarelo claro", "#FFF38F", "#FFF68F"],
  [23, "Verde claro", "#9CFF9C", "#90EE90"], [24, "Azul claro", "#A6C7FF", "#87CEFA"],
  [25, "Rosa claro", "#FFB6D9", "#FFB6C1"], [26, "Roxo claro", "#D6A5FF", "#D8BFD8"],
  [27, "Laranja claro", "#FFD199", "#FFCC99"], [28, "Bege claro", "#FFE8D0", "#F5F5DC"],
  [29, "Ciano claro", "#D6FFFF", "#E0FFFF"], [30, "Branco", "#FFFFFF", "#FFFFFF"],
];

function defaultColorSlots(): Record<string, unknown> {
  return Object.fromEntries(DEFAULT_COLOR_SLOT_DATA.map(([number, name, textHex, roleHex]) => [String(number), {
    number, name, text_hex: textHex.toLowerCase(), role_hex: roleHex.toLowerCase(), role_id: 0, role_name: name, managed: false,
  }]));
}

function defaultColorPanelLayout(): Array<{ id: string; slots: number[] }> {
  return [0, 1, 2].map((index) => ({
    id: `panel-${index + 1}`,
    slots: Array.from({ length: 10 }, (_, offset) => index * 10 + offset + 1),
  }));
}

function defaultTicketOption(id: string, label: string, emoji: string, description: string, flow: string, openingText: string) {
  return {
    id, builtin: true, enabled: true, label, emoji, description, flow,
    confirmation_text: id === "partnership" ? "Ao confirmar, criaremos um ticket privado para conversar com a equipe responsável." : "",
    opening_text: openingText,
    modal_title: id === "report" ? "Enviar denúncia" : id === "suggestion" ? "Enviar sugestão" : "Abrir ticket",
    modal_notice: id === "report" ? "Use esse atendimento apenas para denúncias reais." : "",
    subject_label: id === "report" ? "Usuário denunciado, se houver" : id === "suggestion" ? "Título da sugestão" : "Assunto",
    body_label: id === "suggestion" ? "Descrição da sugestão" : "Explique o atendimento",
    target_channel_id: 0,
    use_report_types: id === "report",
  };
}

function defaultFormsConfig() {
  return {
    form_channel_id: 0,
    responses_channel_id: 0,
    active_message_id: 0,
    active_c_trigger: { channel_id: 0, message_id: 0 },
    active_c_panel: { channel_id: 0, message_id: 0 },
    pending_reviews: [],
    panel: {
      title: "📝 Formulário de verificação",
      description: "Clique no botão abaixo pra preencher sua verificação.",
      button_label: "Preencher formulário",
      button_emoji: "📝",
      button_style: "primary",
      media_url: "",
      accent_color: "#5865F2",
    },
    modal: {
      title: "Nova verificação",
      fields: [
        { id: "field1", label: "Nome", placeholder: "Leonardo", response_label: "Nome", required: true, long: false, show_in_response: true, enabled: true, min_length: 0, max_length: 120 },
        { id: "field2", label: "Idade e pronome", placeholder: "17, ele/dele", response_label: "Idade e pronome", required: true, long: false, show_in_response: true, enabled: true, min_length: 0, max_length: 120 },
        { id: "field3", label: "Descrição", placeholder: "Conta um pouco sobre você...", response_label: "Descrição", required: true, long: true, show_in_response: true, enabled: true, min_length: 0, max_length: 1000 },
      ],
    },
    response: { title: "Nova Verificação", intro: "", footer: "Enviado por {user} • ID `{user_id}`", media_url: "", accent_color: "#5865F2" },
    approval: {
      enabled: false, role_id: 0, approve_label: "Aprovar", approve_emoji: "✅", approve_style: "success",
      reject_label: "Rejeitar", reject_emoji: "❌", reject_style: "danger",
      approve_dm: "✅ **Você foi aprovado em {guild}!**\nO cargo de aprovado foi aplicado, quando configurado pela staff.",
      reject_dm: "❌ **Você foi rejeitado em {guild}.**\nConfira as regras e tente novamente se a staff permitir.",
    },
  };
}

function defaultTicketsConfig() {
  const optionItems = {
    partnership: defaultTicketOption("partnership", "Parceria", "🤝", "Criar um ticket privado de parceria.", "confirm_ticket", "Envie aqui as informações da parceria."),
    report: defaultTicketOption("report", "Denúncia", "👾", "Enviar uma denúncia e abrir um ticket privado.", "modal_ticket", "Envie provas adicionais aqui, se necessário."),
    suggestion: defaultTicketOption("suggestion", "Sugestão", "⚡", "Enviar uma sugestão para o canal configurado.", "modal_channel", "Nova sugestão enviada para análise."),
    other: defaultTicketOption("other", "Outros", "⚙️", "Abrir um ticket para outros assuntos.", "modal_ticket", "Explique aqui o que você precisa e aguarde a equipe."),
  };
  return {
    panel: { channel_id: 0, message_id: 0, title: "🎫 Atendimento", description: "Escolha abaixo o tipo de atendimento.", placeholder: "Escolha uma opção", accent_color: "#5865F2", image_url: "", side_image_url: "" },
    channels: { category_id: 0, logs_channel_id: 0, suggestions_channel_id: 0 },
    roles: { staff_role_id: 0, partnership_staff_role_id: 0, report_staff_role_id: 0, other_staff_role_id: 0 },
    enabled: { partnership: true, report: true, suggestion: true, other: true },
    options: { allow_multiple_open_tickets: false, transcript_on_close: true, use_server_webhook: false },
    permissions: {
      everyone: { view_channel: false, send_messages: false, read_message_history: false, attach_files: false, embed_links: false, add_reactions: false },
      staff: { view_channel: true, send_messages: true, read_message_history: true, attach_files: true, embed_links: true, add_reactions: true, manage_messages: true, manage_channels: false },
      creator: { view_channel: true, send_messages: true, read_message_history: true, attach_files: true, embed_links: true, add_reactions: true, mention_everyone: false },
    },
    texts: {
      partnership_confirm: "Ao confirmar, criaremos um ticket privado para você conversar com a equipe responsável por parcerias.",
      partnership_opening: "A equipe irá analisar sua solicitação. Envie aqui as informações da parceria.",
      report_modal_notice: "Ao enviar este formulário, criaremos um ticket privado para você conversar com a equipe.",
      report_opening: "A equipe irá analisar a denúncia. Envie provas adicionais aqui, se necessário.",
      other_opening: "Explique aqui o que você precisa e aguarde a equipe.",
      suggestion_published: "Nova sugestão enviada para análise.",
      close_notice: "Este ticket será fechado em alguns segundos.",
    },
    option_items: optionItems,
    report_types: ["Spam", "Flood", "Ofensa", "Assédio", "Golpe", "Divulgação indevida", "Conteúdo impróprio", "Raid", "Fake account", "Outro"],
    next_ticket_number: 1,
    next_custom_option_number: 1,
    active_tickets: [],
  };
}

function defaultGuildDoc(guildId: string): Record<string, unknown> {
  return {
    type: "guild",
    guild_id: snowflakeToLong(guildId),
    bot_prefix: "_",
    tts_prefix: ".",
    atts_prefix: "%",
    teto_prefix: "'",
    gtts_prefix: ".",
    edge_prefix: ",",
    speech_limit_seconds: 30,
    announce_author_enabled: false,
    auto_leave_enabled: true,
    ignored_tts_role_id: 0,
    ignored_tts_role_enabled: false,
    tts_voice_channel_id: 0,
    tts_defaults: { engine: "edge", voice: "", language: "pt-BR", rate: "+0%", pitch: "+0Hz" },
    forms: defaultFormsConfig(),
    tickets: defaultTicketsConfig(),
    color_roles: {
      channel_id: 0,
      message_ids: [],
      panel_count: 3,
      panel_layout: defaultColorPanelLayout(),
      messages: Object.fromEntries([1, 2, 3, 4, 5].map((number) => [String(number), { title: "", subtitle: "", footer: "" }])),
      templates: {
        apply: "cor {cor_adicionada} aplicada.", remove: "cor {cor_removida} removida.", switch: "cor alterada: {cor_removida} → {cor_adicionada}.",
        no_role: "Essa cor ainda não está configurada.", hierarchy: "não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",
        missing_panel: "Esse painel de cores não é mais o oficial deste servidor.",
      },
      slots: defaultColorSlots(),
    },
  };
}

function defaultWelcomeDoc(guildId: string): Record<string, unknown> {
  return {
    type: WELCOME_DOC_CONFIG,
    guild_id: snowflakeToLong(guildId),
    enabled: false,
    channel_id: 0,
    render_mode: "components_v2",
    public: { title: "Bem-vindo(a)!", body: "Olá, {membro_mencao}. Seja bem-vindo(a) ao **{servidor}**.", footer: "Você é o membro #{contador}." },
    dm_enabled: false,
    delete_on_leave_enabled: false,
    decorative_emoji_enabled: false,
    auto_role_ids: [],
    style: "complete",
    dm_render_mode: "components_v2",
    accent_color: "#5865F2",
    accent_color_mode: "fixed",
    media_url: "",
    media_mode: "custom",
    embed: {
      content: "", author_name: "", author_icon_mode: "none", author_icon_url: "", author_url: "", title: "", title_url: "",
      description: "", color: "", color_mode: "fixed", thumbnail_mode: "none", thumbnail_url: "", image_mode: "custom", image_url: "",
      footer_text: "", footer_icon_mode: "none", footer_icon_url: "",
    },
    dm: { title: "Bem-vindo(a) ao {servidor}!", body: "Que bom ter você por aqui, {membro}. Aproveite o servidor.", footer: "" },
    webhook: { enabled: false, channel_id: 0, webhook_id: 0, webhook_token: "", name: "Boas-vindas", name_mode: "fixed", avatar_mode: "server", avatar_url: "" },
    variants: [], mode_configs: {}, invite_cache: {}, special_rules: [],
  };
}

function defaultBirthdayDoc(guildId: string): Record<string, unknown> {
  return {
    type: BIRTHDAY_DOC_CONFIG,
    guild_id: snowflakeToLong(guildId),
    register_channel_id: 0,
    announce_channel_id: 0,
    timezone: "America/Sao_Paulo",
    announce_hour: 9,
    announce_minute: 0,
    options: { allow_update: true, show_age: true, group_announcements: true, delete_on_leave: true, leap_day_mode: "feb28", valid_reaction: "✅" },
    templates: {
      calendar: "# 🎂 Aniversários${birthdaycalendarblock}",
      saved: "Prontinho, ${usermention}. Seu aniversário foi salvo como **${birthdaydate}** 🎂",
      updated: "Prontinho, ${usermention}. Atualizei seu aniversário para **${birthdaydate}** 🎂",
      invalid: "Data inválida. Mande uma data no estilo **dia/mês**.",
      announce_single: "🎂 Feliz aniversário, ${usermention}! Hoje é seu dia.",
      announce_group: "🎂 Hoje temos ${birthdaycount} aniversariante(s)!\n\n${birthdaymentions}",
      empty_calendar: "Nenhum aniversário cadastrado ainda.",
    },
  };
}

function messageField(id: string, label: string, scope: DashboardFieldScope, path: string, group: string, maxLength = 1800): DashboardFieldDefinition {
  return { id, label, type: maxLength > 400 ? "textarea" : "text", scope, path, group, maxLength };
}

function ticketOptionFields(id: string, label: string): DashboardFieldDefinition[] {
  const prefix = `tickets.option_items.${id}`;
  const fieldId = `tickets.option_items.${id}`;
  return [
    { id: `${fieldId}.label`, label: `${label}: nome`, type: "text", scope: "guild", path: `${prefix}.label`, maxLength: 80, group: "Fluxos" },
    { id: `${fieldId}.emoji`, label: `${label}: emoji`, type: "text", scope: "guild", path: `${prefix}.emoji`, maxLength: 32, group: "Fluxos" },
    { id: `${fieldId}.description`, label: `${label}: descrição`, type: "text", scope: "guild", path: `${prefix}.description`, maxLength: 100, group: "Fluxos" },
    { id: `${fieldId}.flow`, label: `${label}: comportamento`, type: "select", scope: "guild", path: `${prefix}.flow`, options: TICKET_FLOW_OPTIONS, group: "Fluxos" },
    { id: `${fieldId}.confirmation_text`, label: `${label}: confirmação`, type: "textarea", scope: "guild", path: `${prefix}.confirmation_text`, maxLength: 1200, group: "Fluxos" },
    { id: `${fieldId}.opening_text`, label: `${label}: mensagem de abertura`, type: "textarea", scope: "guild", path: `${prefix}.opening_text`, maxLength: 1800, group: "Fluxos" },
    { id: `${fieldId}.modal_title`, label: `${label}: título do formulário`, type: "text", scope: "guild", path: `${prefix}.modal_title`, maxLength: 45, group: "Fluxos" },
    { id: `${fieldId}.modal_notice`, label: `${label}: aviso do formulário`, type: "textarea", scope: "guild", path: `${prefix}.modal_notice`, maxLength: 1000, group: "Fluxos" },
    { id: `${fieldId}.subject_label`, label: `${label}: campo de assunto`, type: "text", scope: "guild", path: `${prefix}.subject_label`, maxLength: 45, group: "Fluxos" },
    { id: `${fieldId}.body_label`, label: `${label}: campo de descrição`, type: "text", scope: "guild", path: `${prefix}.body_label`, maxLength: 45, group: "Fluxos" },
    { id: `${fieldId}.target_channel_id`, label: `${label}: canal de destino`, type: "channel", scope: "guild", path: `${prefix}.target_channel_id`, group: "Fluxos" },
    { id: `${fieldId}.use_report_types`, label: `${label}: usar tipos de denúncia`, type: "boolean", scope: "guild", path: `${prefix}.use_report_types`, group: "Fluxos" },
  ];
}

const sections: DashboardSectionDefinition[] = [
  {
    id: "general", label: "Geral", emoji: "⚙️", description: "Preferências básicas usadas pelos comandos do bot.",
    fields: [
      { id: "general.bot_prefix", label: "Prefixo do bot", description: "Prefixo usado pelos comandos por mensagem.", type: "text", scope: "guild", path: "bot_prefix", maxLength: 8, placeholder: "_" },
    ],
  },
  {
    id: "welcome", label: "Boas-vindas", emoji: "👋", description: "Mensagem pública, DM, cargos automáticos e identidade de envio.",
    groups: ["Mensagem de entrada", "Mensagem privada", "Aparência", "Cargos"],
    groupMetadata: {
      "Mensagem de entrada": {
        kind: "message",
        variables: WELCOME_TEMPLATE_VARIABLES,
        settingsFieldIds: [
          "welcome.enabled", "welcome.channel_id", "welcome.render_mode", "welcome.delete_on_leave_enabled",
        ],
        editors: [{
          id: "welcome-public",
          label: "Mensagem pública",
          description: "Edite a mensagem e a identidade usada no envio.",
          presentation: "adaptive",
          fieldIds: [
            "welcome.public.title", "welcome.public.body", "welcome.public.footer",
            "welcome.style", "welcome.accent_color", "welcome.accent_color_mode", "welcome.media_mode", "welcome.media_url",
            "welcome.embed.content", "welcome.embed.author_name", "welcome.embed.author_icon_mode", "welcome.embed.author_icon_url",
            "welcome.embed.author_url", "welcome.embed.title", "welcome.embed.title_url", "welcome.embed.description",
            "welcome.embed.color", "welcome.embed.color_mode", "welcome.embed.thumbnail_mode", "welcome.embed.thumbnail_url",
            "welcome.embed.image_mode", "welcome.embed.image_url", "welcome.embed.footer_text",
            "welcome.embed.footer_icon_mode", "welcome.embed.footer_icon_url",
          ],
          senderFieldIds: [
            "welcome.webhook.enabled", "welcome.webhook.name_mode", "welcome.webhook.name",
            "welcome.webhook.avatar_mode", "welcome.webhook.avatar_url",
          ],
        }],
      },
      "Mensagem privada": {
        kind: "message",
        variables: WELCOME_TEMPLATE_VARIABLES,
        settingsFieldIds: ["welcome.dm_enabled", "welcome.dm_render_mode"],
        editors: [{
          id: "welcome-dm",
          label: "Mensagem privada",
          description: "Conteúdo enviado diretamente ao novo membro.",
          presentation: "adaptive",
          fieldIds: ["welcome.dm.title", "welcome.dm.body", "welcome.dm.footer"],
        }],
      },
    },
    fields: [
      { id: "welcome.enabled", label: "Ativar boas-vindas", type: "boolean", scope: "welcome", path: "enabled", group: "Mensagem de entrada" },
      { id: "welcome.channel_id", label: "Canal de boas-vindas", type: "channel", scope: "welcome", path: "channel_id", group: "Mensagem de entrada" },
      { id: "welcome.render_mode", label: "Formato público", type: "select", scope: "welcome", path: "render_mode", options: WELCOME_MODE_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.style", label: "Estilo Components V2", type: "select", scope: "welcome", path: "style", options: WELCOME_STYLE_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.delete_on_leave_enabled", label: "Apagar se o membro sair em até 24h", type: "boolean", scope: "welcome", path: "delete_on_leave_enabled", group: "Mensagem de entrada" },
      messageField("welcome.public.title", "Título público", "welcome", "public.title", "Mensagem de entrada", 256),
      messageField("welcome.public.body", "Mensagem pública", "welcome", "public.body", "Mensagem de entrada"),
      messageField("welcome.public.footer", "Rodapé público", "welcome", "public.footer", "Mensagem de entrada", 300),
      messageField("welcome.embed.content", "Texto acima do embed", "welcome", "embed.content", "Mensagem de entrada"),
      messageField("welcome.embed.author_name", "Autor", "welcome", "embed.author_name", "Mensagem de entrada", 256),
      { id: "welcome.embed.author_icon_mode", label: "Ícone do autor", type: "select", scope: "welcome", path: "embed.author_icon_mode", options: WELCOME_EMBED_IMAGE_MODE_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.embed.author_icon_url", label: "URL do ícone do autor", type: "url", scope: "welcome", path: "embed.author_icon_url", maxLength: 1000, group: "Mensagem de entrada" },
      { id: "welcome.embed.author_url", label: "URL do autor", type: "url", scope: "welcome", path: "embed.author_url", maxLength: 1000, group: "Mensagem de entrada" },
      messageField("welcome.embed.title", "Título do embed", "welcome", "embed.title", "Mensagem de entrada", 256),
      { id: "welcome.embed.title_url", label: "URL do título", type: "url", scope: "welcome", path: "embed.title_url", maxLength: 1000, group: "Mensagem de entrada" },
      messageField("welcome.embed.description", "Descrição do embed", "welcome", "embed.description", "Mensagem de entrada"),
      { id: "welcome.embed.color", label: "Cor do embed", type: "color", scope: "welcome", path: "embed.color", group: "Mensagem de entrada" },
      { id: "welcome.embed.color_mode", label: "Modo da cor", type: "select", scope: "welcome", path: "embed.color_mode", options: WELCOME_COLOR_MODE_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.embed.thumbnail_mode", label: "Thumbnail", type: "select", scope: "welcome", path: "embed.thumbnail_mode", options: WELCOME_EMBED_IMAGE_MODE_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.embed.thumbnail_url", label: "URL da thumbnail", type: "url", scope: "welcome", path: "embed.thumbnail_url", maxLength: 1000, group: "Mensagem de entrada" },
      { id: "welcome.embed.image_mode", label: "Imagem principal", type: "select", scope: "welcome", path: "embed.image_mode", options: WELCOME_EMBED_MAIN_IMAGE_MODE_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.embed.image_url", label: "URL da imagem", type: "url", scope: "welcome", path: "embed.image_url", maxLength: 1000, group: "Mensagem de entrada" },
      messageField("welcome.embed.footer_text", "Rodapé do embed", "welcome", "embed.footer_text", "Mensagem de entrada", 1000),
      { id: "welcome.embed.footer_icon_mode", label: "Ícone do rodapé", type: "select", scope: "welcome", path: "embed.footer_icon_mode", options: WELCOME_EMBED_IMAGE_MODE_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.embed.footer_icon_url", label: "URL do ícone do rodapé", type: "url", scope: "welcome", path: "embed.footer_icon_url", maxLength: 1000, group: "Mensagem de entrada" },
      { id: "welcome.dm_enabled", label: "Enviar mensagem no privado", type: "boolean", scope: "welcome", path: "dm_enabled", group: "Mensagem privada" },
      { id: "welcome.dm_render_mode", label: "Formato da DM", type: "select", scope: "welcome", path: "dm_render_mode", options: WELCOME_MODE_OPTIONS, group: "Mensagem privada" },
      messageField("welcome.dm.title", "Título da DM", "welcome", "dm.title", "Mensagem privada", 256),
      messageField("welcome.dm.body", "Mensagem da DM", "welcome", "dm.body", "Mensagem privada"),
      messageField("welcome.dm.footer", "Rodapé da DM", "welcome", "dm.footer", "Mensagem privada", 300),
      { id: "welcome.decorative_emoji_enabled", label: "Emojis decorativos", type: "boolean", scope: "welcome", path: "decorative_emoji_enabled", group: "Aparência" },
      { id: "welcome.accent_color", label: "Cor de destaque", type: "color", scope: "welcome", path: "accent_color", group: "Aparência" },
      { id: "welcome.accent_color_mode", label: "Modo da cor", type: "select", scope: "welcome", path: "accent_color_mode", options: WELCOME_COLOR_MODE_OPTIONS, group: "Aparência" },
      { id: "welcome.media_mode", label: "Imagem ou banner", type: "select", scope: "welcome", path: "media_mode", options: WELCOME_MEDIA_MODE_OPTIONS, group: "Aparência" },
      { id: "welcome.media_url", label: "URL da imagem ou banner", type: "url", scope: "welcome", path: "media_url", maxLength: 1000, group: "Aparência" },
      { id: "welcome.auto_role_ids", label: "Cargos automáticos", description: "Cargos aplicados quando um membro entra.", type: "role_multi", scope: "welcome", path: "auto_role_ids", group: "Cargos" },
      { id: "welcome.webhook.enabled", label: "Enviar como webhook", description: "Use nome e avatar personalizados nesta mensagem.", type: "boolean", scope: "welcome", path: "webhook.enabled", group: "Mensagem de entrada" },
      { id: "welcome.webhook.name_mode", label: "Nome exibido", type: "select", scope: "welcome", path: "webhook.name_mode", options: WELCOME_WEBHOOK_NAME_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.webhook.name", label: "Nome personalizado", type: "text", scope: "welcome", path: "webhook.name", maxLength: 80, group: "Mensagem de entrada" },
      { id: "welcome.webhook.avatar_mode", label: "Avatar exibido", type: "select", scope: "welcome", path: "webhook.avatar_mode", options: WELCOME_WEBHOOK_AVATAR_OPTIONS, group: "Mensagem de entrada" },
      { id: "welcome.webhook.avatar_url", label: "URL do avatar", type: "url", scope: "welcome", path: "webhook.avatar_url", maxLength: 1000, group: "Mensagem de entrada" },
    ],
  },
  {
    id: "forms", label: "Formulários", emoji: "📝", description: "Painel, perguntas, respostas e aprovação da verificação.",
    groups: ["Canais", "Painel", "Perguntas", "Resposta", "Aprovação"],
    groupMetadata: {
      Painel: {
        kind: "message",
        editors: [{
          id: "forms-panel",
          label: "Painel público",
          presentation: "components_v2",
          description: "Título, descrição, botão, imagem, cor e prévia do painel.",
          fieldIds: [
            "forms.panel.title", "forms.panel.description", "forms.panel.button_label", "forms.panel.button_emoji",
            "forms.panel.button_style", "forms.panel.media_url", "forms.panel.accent_color",
          ],
        }],
      },
      Resposta: {
        kind: "message",
        variables: FORM_TEMPLATE_VARIABLES,
        editors: [{
          id: "forms-response",
          label: "Resposta enviada à equipe",
          presentation: "components_v2",
          description: "Conteúdo, imagem, cor e prévia do resumo recebido pela equipe.",
          fieldIds: [
            "forms.response.title", "forms.response.intro", "forms.response.footer",
            "forms.response.media_url", "forms.response.accent_color",
          ],
        }],
      },
      Aprovação: {
        kind: "message",
        variables: FORM_TEMPLATE_VARIABLES,
        settingsFieldIds: [
          "forms.approval.enabled", "forms.approval.role_id", "forms.approval.approve_label",
          "forms.approval.approve_emoji", "forms.approval.approve_style", "forms.approval.reject_label",
          "forms.approval.reject_emoji", "forms.approval.reject_style",
        ],
        editors: [
          {
            id: "forms-approve-dm",
            label: "Mensagem ao aprovar",
            presentation: "components_v2",
            description: "Mensagem privada enviada após a aprovação.",
            fieldIds: ["forms.approval.approve_dm"],
          },
          {
            id: "forms-reject-dm",
            label: "Mensagem ao rejeitar",
            presentation: "components_v2",
            description: "Mensagem privada enviada após a rejeição.",
            fieldIds: ["forms.approval.reject_dm"],
          },
        ],
      },
    },
    fields: [
      { id: "forms.form_channel_id", label: "Canal do formulário", type: "channel", scope: "guild", path: "forms.form_channel_id", group: "Canais" },
      { id: "forms.responses_channel_id", label: "Canal de respostas", type: "channel", scope: "guild", path: "forms.responses_channel_id", group: "Canais" },
      { id: "forms.panel.title", label: "Título do painel", type: "text", scope: "guild", path: "forms.panel.title", maxLength: 250, group: "Painel" },
      { id: "forms.panel.description", label: "Descrição do painel", type: "textarea", scope: "guild", path: "forms.panel.description", maxLength: 1000, group: "Painel" },
      { id: "forms.panel.button_label", label: "Texto do botão", type: "text", scope: "guild", path: "forms.panel.button_label", maxLength: 80, group: "Painel" },
      { id: "forms.panel.button_emoji", label: "Emoji do botão", type: "text", scope: "guild", path: "forms.panel.button_emoji", maxLength: 32, group: "Painel" },
      { id: "forms.panel.button_style", label: "Cor do botão", type: "select", scope: "guild", path: "forms.panel.button_style", options: BUTTON_STYLE_OPTIONS, group: "Painel" },
      { id: "forms.panel.media_url", label: "Imagem do painel", type: "url", scope: "guild", path: "forms.panel.media_url", maxLength: 400, group: "Painel" },
      { id: "forms.panel.accent_color", label: "Cor de destaque", type: "color", scope: "guild", path: "forms.panel.accent_color", group: "Painel" },
      { id: "forms.modal.title", label: "Título do formulário", type: "text", scope: "guild", path: "forms.modal.title", maxLength: 45, group: "Perguntas" },
      { id: "forms.modal.fields", label: "Perguntas", description: "Até cinco campos exibidos no modal do Discord.", type: "form_fields", scope: "guild", path: "forms.modal.fields", group: "Perguntas" },
      { id: "forms.response.title", label: "Título da resposta", type: "text", scope: "guild", path: "forms.response.title", maxLength: 250, group: "Resposta" },
      { id: "forms.response.intro", label: "Texto introdutório", type: "textarea", scope: "guild", path: "forms.response.intro", maxLength: 700, group: "Resposta" },
      { id: "forms.response.footer", label: "Rodapé", type: "textarea", scope: "guild", path: "forms.response.footer", maxLength: 700, group: "Resposta" },
      { id: "forms.response.media_url", label: "Imagem da resposta", type: "url", scope: "guild", path: "forms.response.media_url", maxLength: 400, group: "Resposta" },
      { id: "forms.response.accent_color", label: "Cor da resposta", type: "color", scope: "guild", path: "forms.response.accent_color", group: "Resposta" },
      { id: "forms.approval.enabled", label: "Ativar aprovação da staff", type: "boolean", scope: "guild", path: "forms.approval.enabled", group: "Aprovação" },
      { id: "forms.approval.role_id", label: "Cargo aplicado ao aprovar", type: "role", scope: "guild", path: "forms.approval.role_id", group: "Aprovação" },
      { id: "forms.approval.approve_label", label: "Texto do botão Aprovar", type: "text", scope: "guild", path: "forms.approval.approve_label", maxLength: 80, group: "Aprovação" },
      { id: "forms.approval.approve_emoji", label: "Emoji de aprovação", type: "text", scope: "guild", path: "forms.approval.approve_emoji", maxLength: 32, group: "Aprovação" },
      { id: "forms.approval.approve_style", label: "Cor do botão Aprovar", type: "select", scope: "guild", path: "forms.approval.approve_style", options: BUTTON_STYLE_OPTIONS, group: "Aprovação" },
      { id: "forms.approval.reject_label", label: "Texto do botão Rejeitar", type: "text", scope: "guild", path: "forms.approval.reject_label", maxLength: 80, group: "Aprovação" },
      { id: "forms.approval.reject_emoji", label: "Emoji de rejeição", type: "text", scope: "guild", path: "forms.approval.reject_emoji", maxLength: 32, group: "Aprovação" },
      { id: "forms.approval.reject_style", label: "Cor do botão Rejeitar", type: "select", scope: "guild", path: "forms.approval.reject_style", options: BUTTON_STYLE_OPTIONS, group: "Aprovação" },
      { id: "forms.approval.approve_dm", label: "DM ao aprovar", type: "textarea", scope: "guild", path: "forms.approval.approve_dm", maxLength: 1000, group: "Aprovação" },
      { id: "forms.approval.reject_dm", label: "DM ao rejeitar", type: "textarea", scope: "guild", path: "forms.approval.reject_dm", maxLength: 1000, group: "Aprovação" },
    ],
  },
  {
    id: "tickets", label: "Tickets", emoji: "🎫", description: "Painel, fluxos de atendimento, permissões e transcrições.",
    groups: ["Painel", "Atendimento", "Comportamento", "Fluxos", "Textos", "Denúncias", "Permissões"],
    groupMetadata: {
      Painel: {
        kind: "message",
        settingsFieldIds: ["tickets.panel.channel_id"],
        editors: [{
          id: "tickets-panel",
          label: "Painel público",
          presentation: "components_v2",
          description: "Título, descrição, seletor, imagens, cor e prévia do painel de atendimento.",
          fieldIds: [
            "tickets.panel.title", "tickets.panel.description", "tickets.panel.placeholder",
            "tickets.panel.accent_color", "tickets.panel.image_url", "tickets.panel.side_image_url",
          ],
        }],
      },
      Textos: {
        kind: "message",
        editors: [
          { id: "tickets-partnership-texts", label: "Parceria", description: "Confirmação e mensagem de abertura.", fieldIds: ["tickets.texts.partnership_confirm", "tickets.texts.partnership_opening"] },
          { id: "tickets-report-texts", label: "Denúncia", description: "Aviso do formulário e mensagem de abertura.", fieldIds: ["tickets.texts.report_modal_notice", "tickets.texts.report_opening"] },
          { id: "tickets-other-texts", label: "Outros", description: "Mensagem de abertura do atendimento geral.", fieldIds: ["tickets.texts.other_opening"] },
          { id: "tickets-suggestion-texts", label: "Sugestão", description: "Mensagem após publicar uma sugestão.", fieldIds: ["tickets.texts.suggestion_published"] },
          { id: "tickets-close-texts", label: "Encerramento", description: "Aviso exibido ao fechar o ticket.", fieldIds: ["tickets.texts.close_notice"] },
        ],
      },
    },
    fields: [
      { id: "tickets.panel.channel_id", label: "Canal do painel", type: "channel", scope: "guild", path: "tickets.panel.channel_id", group: "Painel" },
      { id: "tickets.panel.title", label: "Título", type: "text", scope: "guild", path: "tickets.panel.title", maxLength: 250, group: "Painel" },
      { id: "tickets.panel.description", label: "Descrição", type: "textarea", scope: "guild", path: "tickets.panel.description", maxLength: 1200, group: "Painel" },
      { id: "tickets.panel.placeholder", label: "Placeholder do seletor", type: "text", scope: "guild", path: "tickets.panel.placeholder", maxLength: 150, group: "Painel" },
      { id: "tickets.panel.accent_color", label: "Cor de destaque", type: "color", scope: "guild", path: "tickets.panel.accent_color", group: "Painel" },
      { id: "tickets.panel.image_url", label: "Imagem principal", type: "url", scope: "guild", path: "tickets.panel.image_url", maxLength: 1000, group: "Painel" },
      { id: "tickets.panel.side_image_url", label: "Imagem lateral", type: "url", scope: "guild", path: "tickets.panel.side_image_url", maxLength: 1000, group: "Painel" },
      { id: "tickets.channels.category_id", label: "Categoria dos tickets", type: "channel", scope: "guild", path: "tickets.channels.category_id", group: "Atendimento" },
      { id: "tickets.channels.logs_channel_id", label: "Canal de logs", type: "channel", scope: "guild", path: "tickets.channels.logs_channel_id", group: "Atendimento" },
      { id: "tickets.channels.suggestions_channel_id", label: "Canal de sugestões", type: "channel", scope: "guild", path: "tickets.channels.suggestions_channel_id", group: "Atendimento" },
      { id: "tickets.roles.staff_role_id", label: "Staff geral", type: "role", scope: "guild", path: "tickets.roles.staff_role_id", group: "Atendimento" },
      { id: "tickets.roles.partnership_staff_role_id", label: "Staff de parcerias", type: "role", scope: "guild", path: "tickets.roles.partnership_staff_role_id", group: "Atendimento" },
      { id: "tickets.roles.report_staff_role_id", label: "Staff de denúncias", type: "role", scope: "guild", path: "tickets.roles.report_staff_role_id", group: "Atendimento" },
      { id: "tickets.roles.other_staff_role_id", label: "Staff de outros", type: "role", scope: "guild", path: "tickets.roles.other_staff_role_id", group: "Atendimento" },
      { id: "tickets.options.allow_multiple_open_tickets", label: "Permitir vários tickets por usuário", type: "boolean", scope: "guild", path: "tickets.options.allow_multiple_open_tickets", group: "Comportamento" },
      { id: "tickets.options.transcript_on_close", label: "Gerar transcrição ao fechar", type: "boolean", scope: "guild", path: "tickets.options.transcript_on_close", group: "Comportamento" },
      { id: "tickets.options.use_server_webhook", label: "Usar identidade do servidor", type: "boolean", scope: "guild", path: "tickets.options.use_server_webhook", group: "Comportamento" },
      { id: "tickets.enabled.partnership", label: "Ativar Parceria", type: "boolean", scope: "guild", path: "tickets.enabled.partnership", group: "Fluxos" },
      { id: "tickets.enabled.report", label: "Ativar Denúncia", type: "boolean", scope: "guild", path: "tickets.enabled.report", group: "Fluxos" },
      { id: "tickets.enabled.suggestion", label: "Ativar Sugestão", type: "boolean", scope: "guild", path: "tickets.enabled.suggestion", group: "Fluxos" },
      { id: "tickets.enabled.other", label: "Ativar Outros", type: "boolean", scope: "guild", path: "tickets.enabled.other", group: "Fluxos" },
      ...ticketOptionFields("partnership", "Parceria"),
      ...ticketOptionFields("report", "Denúncia"),
      ...ticketOptionFields("suggestion", "Sugestão"),
      ...ticketOptionFields("other", "Outros"),
      ...Object.entries({
        partnership_confirm: "Confirmação de parceria", partnership_opening: "Abertura de parceria", report_modal_notice: "Aviso da denúncia",
        report_opening: "Abertura da denúncia", other_opening: "Abertura de Outros", suggestion_published: "Sugestão publicada", close_notice: "Aviso de fechamento",
      }).map(([key, label]) => ({ id: `tickets.texts.${key}`, label, type: "textarea" as const, scope: "guild" as const, path: `tickets.texts.${key}`, maxLength: 1800, group: "Textos" })),
      { id: "tickets.report_types", label: "Tipos de denúncia", description: "Uma opção por linha.", type: "string_list", scope: "guild", path: "tickets.report_types", group: "Denúncias" },
      ...["everyone", "staff", "creator"].flatMap((scope) => [
        "view_channel", "send_messages", "read_message_history", "attach_files", "embed_links", "add_reactions",
        ...(scope === "staff" ? ["manage_messages", "manage_channels"] : []),
        ...(scope === "creator" ? ["mention_everyone"] : []),
      ].map((permission) => ({
        id: `tickets.permissions.${scope}.${permission}`,
        label: `${scope === "everyone" ? "Demais membros" : scope === "staff" ? "Equipe" : "Autor"}: ${({
          view_channel: "ver o canal",
          send_messages: "enviar mensagens",
          read_message_history: "ler o histórico",
          attach_files: "enviar arquivos",
          embed_links: "incorporar links",
          add_reactions: "adicionar reações",
          manage_messages: "gerenciar mensagens",
          manage_channels: "gerenciar o canal",
          mention_everyone: "mencionar todos",
        } as Record<string, string>)[permission] || permission}`,
        type: "boolean" as const, scope: "guild" as const, path: `tickets.permissions.${scope}.${permission}`, group: "Permissões",
      }))),
    ],
  },
  {
    id: "color_roles", label: "Cargos de cor", emoji: "🎨", description: "Até três painéis com opções vinculadas aos cargos do servidor.",
    groups: ["Painel", "Mensagens"],
    groupMetadata: {
      Painel: {
        kind: "message",
        settingsFieldIds: ["color_roles.channel_id"],
        editors: [],
      },
      Mensagens: {
        kind: "message",
        editors: [{
          id: "color-role-feedback",
          label: "Mensagens de resultado",
          description: "Respostas exibidas ao aplicar, remover ou trocar uma cor.",
          fieldIds: [
            "color_roles.templates.apply", "color_roles.templates.remove", "color_roles.templates.switch",
            "color_roles.templates.no_role", "color_roles.templates.hierarchy", "color_roles.templates.missing_panel",
          ],
        }],
      },
    },
    fields: [
      { id: "color_roles.channel_id", label: "Canal do painel", type: "channel", scope: "guild", path: "color_roles.channel_id", group: "Painel" },
      { id: "color_roles.panel_layout", label: "Painéis", description: "Adicione, remova e reorganize até três painéis.", type: "color_panel_layout", scope: "guild", path: "color_roles.panel_layout", group: "Painel" },
      { id: "color_roles.slots", label: "Opções dos painéis", description: "Nome e cargo vinculado de cada opção.", type: "color_slots", scope: "guild", path: "color_roles.slots", group: "Painel" },
      ...Object.entries({
        apply: "Cor aplicada", remove: "Cor removida", switch: "Cor trocada", no_role: "Cor sem cargo", hierarchy: "Erro de hierarquia", missing_panel: "Painel antigo",
      }).map(([key, label]) => ({ id: `color_roles.templates.${key}`, label, type: "textarea" as const, scope: "guild" as const, path: `color_roles.templates.${key}`, maxLength: 1000, group: "Mensagens" })),
    ],
  },
  {
    id: "birthday", label: "Aniversários", emoji: "🎂", description: "Cadastro, calendário e avisos automáticos de aniversário.",
    groups: ["Geral", "Canais", "Registro de datas", "Avisos", "Calendário"],
    groupMetadata: {
      "Registro de datas": {
        kind: "message",
        variables: BIRTHDAY_REGISTER_VARIABLES,
        settingsFieldIds: ["birthday.options.leap_day_mode", "birthday.options.valid_reaction"],
        editors: [{
          id: "birthday-register-messages",
          label: "Mensagens de cadastro",
          description: "Respostas ao salvar, atualizar ou informar uma data inválida.",
          fieldIds: ["birthday.templates.saved", "birthday.templates.updated", "birthday.templates.invalid"],
        }],
      },
      Avisos: {
        kind: "message",
        variables: BIRTHDAY_ANNOUNCE_VARIABLES,
        settingsFieldIds: [
          "birthday.timezone", "birthday.announce_hour", "birthday.announce_minute",
          "birthday.options.show_age", "birthday.options.group_announcements", "birthday.options.delete_on_leave",
        ],
        editors: [{
          id: "birthday-announcements",
          label: "Mensagens de aniversário",
          description: "Avisos individuais e agrupados enviados automaticamente.",
          fieldIds: ["birthday.templates.announce_single", "birthday.templates.announce_group"],
        }],
      },
      Calendário: {
        kind: "message",
        variables: BIRTHDAY_CALENDAR_VARIABLES,
        editors: [{
          id: "birthday-calendar",
          label: "Calendário",
          description: "Conteúdo do calendário e estado sem aniversários cadastrados.",
          fieldIds: ["birthday.templates.calendar", "birthday.templates.empty_calendar"],
        }],
      },
    },
    fields: [
      { id: "birthday.options.allow_update", label: "Permitir atualizar a própria data", type: "boolean", scope: "birthday", path: "options.allow_update", group: "Geral" },
      { id: "birthday.register_channel_id", label: "Canal do calendário/cadastro", type: "channel", scope: "birthday", path: "register_channel_id", group: "Canais" },
      { id: "birthday.announce_channel_id", label: "Canal de avisos", type: "channel", scope: "birthday", path: "announce_channel_id", group: "Canais" },
      { id: "birthday.options.leap_day_mode", label: "Aniversário em 29/02", type: "select", scope: "birthday", path: "options.leap_day_mode", options: BIRTHDAY_LEAP_MODE_OPTIONS, group: "Registro de datas" },
      { id: "birthday.options.valid_reaction", label: "Reação em data válida", type: "text", scope: "birthday", path: "options.valid_reaction", maxLength: 20, group: "Registro de datas" },
      messageField("birthday.templates.saved", "Mensagem ao salvar", "birthday", "templates.saved", "Registro de datas"),
      messageField("birthday.templates.updated", "Mensagem ao atualizar", "birthday", "templates.updated", "Registro de datas"),
      messageField("birthday.templates.invalid", "Mensagem de data inválida", "birthday", "templates.invalid", "Registro de datas"),
      { id: "birthday.timezone", label: "Fuso horário", type: "text", scope: "birthday", path: "timezone", maxLength: 64, placeholder: "America/Sao_Paulo", group: "Avisos" },
      { id: "birthday.announce_hour", label: "Hora do aviso", type: "number", scope: "birthday", path: "announce_hour", min: 0, max: 23, group: "Avisos" },
      { id: "birthday.announce_minute", label: "Minuto do aviso", type: "number", scope: "birthday", path: "announce_minute", min: 0, max: 59, group: "Avisos" },
      { id: "birthday.options.show_age", label: "Mostrar idade", type: "boolean", scope: "birthday", path: "options.show_age", group: "Avisos" },
      { id: "birthday.options.group_announcements", label: "Agrupar aniversariantes", type: "boolean", scope: "birthday", path: "options.group_announcements", group: "Avisos" },
      { id: "birthday.options.delete_on_leave", label: "Remover cadastro quando sair", type: "boolean", scope: "birthday", path: "options.delete_on_leave", group: "Avisos" },
      messageField("birthday.templates.announce_single", "Aviso individual", "birthday", "templates.announce_single", "Avisos"),
      messageField("birthday.templates.announce_group", "Aviso agrupado", "birthday", "templates.announce_group", "Avisos"),
      messageField("birthday.templates.calendar", "Template do calendário", "birthday", "templates.calendar", "Calendário"),
      messageField("birthday.templates.empty_calendar", "Calendário vazio", "birthday", "templates.empty_calendar", "Calendário"),
    ],
  },
  {
    id: "tts", label: "Texto pra Voz", emoji: "🔊", description: "Engine, voz, idioma, prefixos e comportamento do leitor.",
    groups: ["Voz", "Prefixos", "Comportamento"],
    fields: [
      { id: "tts.engine", label: "Mecanismo de voz", type: "select", scope: "guild", path: "tts_defaults.engine", options: TTS_ENGINE_OPTIONS, group: "Voz" },
      { id: "tts.voice", label: "Voz", description: "Voz usada pelo Microsoft Edge.", type: "select", scope: "guild", path: "tts_defaults.voice", options: TTS_VOICE_OPTIONS, group: "Voz" },
      { id: "tts.language", label: "Idioma", description: "Idioma usado pelo Google TTS.", type: "select", scope: "guild", path: "tts_defaults.language", options: TTS_LANGUAGE_OPTIONS, group: "Voz" },
      { id: "tts.rate", label: "Velocidade", description: "Ajuste de -100% a +100%.", type: "text", scope: "guild", path: "tts_defaults.rate", maxLength: 16, placeholder: "+0%", group: "Voz" },
      { id: "tts.pitch", label: "Tom", description: "Ajuste de -100Hz a +100Hz.", type: "text", scope: "guild", path: "tts_defaults.pitch", maxLength: 16, placeholder: "+0Hz", group: "Voz" },
      { id: "tts.voice_channel_id", label: "Canal de voz preferido", description: "Canal usado como referência quando configurado.", type: "channel", scope: "guild", path: "tts_voice_channel_id", group: "Voz" },
      { id: "tts.atts_prefix", label: "Prefixo ATTS", type: "text", scope: "guild", path: "atts_prefix", maxLength: 8, placeholder: "%", group: "Prefixos" },
      { id: "tts.teto_prefix", label: "Prefixo Kasane Teto", description: "Aciona a engine experimental no phone worker.", type: "text", scope: "guild", path: "teto_prefix", maxLength: 8, placeholder: "'", group: "Prefixos" },
      { id: "tts.gtts_prefix", label: "Prefixo gTTS", type: "text", scope: "guild", path: "gtts_prefix", maxLength: 8, placeholder: ".", group: "Prefixos" },
      { id: "tts.edge_prefix", label: "Prefixo Edge", type: "text", scope: "guild", path: "edge_prefix", maxLength: 8, placeholder: ",", group: "Prefixos" },
      { id: "tts.speech_limit_seconds", label: "Limite por leitura", description: "Tempo máximo reproduzido em cada mensagem.", type: "number", scope: "guild", path: "speech_limit_seconds", min: 1, max: 600, group: "Comportamento" },
      { id: "tts.announce_author_enabled", label: "Anunciar o nome do autor", description: "Lê o nome antes do conteúdo da mensagem.", type: "boolean", scope: "guild", path: "announce_author_enabled", group: "Comportamento" },
      { id: "tts.auto_leave_enabled", label: "Sair automaticamente", description: "Desconecta quando não houver mais ninguém no canal.", type: "boolean", scope: "guild", path: "auto_leave_enabled", group: "Comportamento" },
      { id: "tts.ignored_tts_role_id", label: "Cargo ignorado pelo TTS", type: "role", scope: "guild", path: "ignored_tts_role_id", group: "Comportamento" },
      { id: "tts.ignored_tts_role_enabled", label: "Ativar cargo ignorado", type: "boolean", scope: "guild", path: "ignored_tts_role_enabled", group: "Comportamento" },
    ],
  },
];

function snowflakeToLong(value: string): Long {
  const text = String(value ?? "").trim();
  if (!/^\d{1,25}$/.test(text)) return Long.ZERO;
  try { return Long.fromString(text, false); } catch { return Long.ZERO; }
}
function snowflakeFromRaw(raw: unknown): Long {
  const match = String(raw ?? "").trim().match(/\d{15,25}/);
  return match ? snowflakeToLong(match[0]) : Long.ZERO;
}
function isLongLike(value: unknown): value is Long {
  return value instanceof Long || (typeof value === "object" && value !== null && typeof (value as { low?: unknown }).low === "number" && typeof (value as { high?: unknown }).high === "number");
}
function serializeSnowflake(value: unknown): string {
  if (isLongLike(value)) { const text = value.toString(); return text === "0" ? "" : text; }
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? String(Math.trunc(value)) : "";
  const text = String(value ?? "").trim();
  return text === "0" ? "" : text;
}
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)); }
function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value) || isLongLike(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}
function deepMerge(defaults: Record<string, unknown>, raw: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = { ...defaults };
  for (const [key, value] of Object.entries(raw)) {
    if (isPlainObject(value) && isPlainObject(defaults[key])) result[key] = deepMerge(defaults[key] as Record<string, unknown>, value);
    else result[key] = value;
  }
  return result;
}
function getPath(source: Record<string, unknown>, path: string): unknown {
  let current: unknown = source;
  for (const part of path.split(".")) {
    if (!part) continue;
    if (typeof current !== "object" || current === null || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}
function setPath(target: Record<string, unknown>, path: string, value: unknown) {
  const parts = path.split(".").filter(Boolean);
  let current = target;
  for (const part of parts.slice(0, -1)) {
    if (!isPlainObject(current[part])) current[part] = {};
    current = current[part] as Record<string, unknown>;
  }
  current[parts[parts.length - 1]] = value;
}
function dotSetForPath(target: Record<string, unknown>, path: string, value: unknown) {
  const cleanPath = path.split(".").filter(Boolean).join(".");
  if (cleanPath && !cleanPath.includes("$") && !cleanPath.includes("..")) target[cleanPath] = value;
}
function cleanColor(raw: unknown, fallback = ""): string {
  const text = String(raw ?? "").trim();
  if (!text) return fallback;
  const normalized = text.startsWith("#") ? text : `#${text}`;
  return /^#[0-9a-fA-F]{6}$/.test(normalized) ? normalized.toUpperCase() : fallback;
}
function normalizeFormFields(raw: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(raw)) return defaultFormsConfig().modal.fields;
  return raw.slice(0, 5).map((item, index) => {
    const value = isPlainObject(item) ? item : {};
    const long = Boolean(value.long);
    const maxAllowed = long ? 1000 : 120;
    const minLength = Math.max(0, Math.min(maxAllowed, Math.trunc(Number(value.min_length) || 0)));
    const maxLength = Math.max(Math.max(1, minLength), Math.min(maxAllowed, Math.trunc(Number(value.max_length) || maxAllowed)));
    return {
      id: `field${index + 1}`,
      label: String(value.label || `Pergunta ${index + 1}`).trim().slice(0, 45) || `Pergunta ${index + 1}`,
      placeholder: String(value.placeholder || "").slice(0, 100),
      response_label: String(value.response_label || value.label || `Pergunta ${index + 1}`).trim().slice(0, 45),
      required: value.required !== false,
      long,
      show_in_response: value.show_in_response !== false,
      enabled: value.enabled !== false,
      min_length: minLength,
      max_length: maxLength,
    };
  });
}
function normalizeColorPanelLayout(raw: unknown): Array<{ id: string; slots: number[] }> {
  const source = Array.isArray(raw) ? raw : defaultColorPanelLayout();
  const used = new Set<number>();
  const panels: Array<{ id: string; slots: number[] }> = [];
  for (const [index, rawPanel] of source.slice(0, 3).entries()) {
    const panel = isPlainObject(rawPanel) ? rawPanel : {};
    const slots: number[] = [];
    for (const rawSlot of Array.isArray(panel.slots) ? panel.slots : []) {
      const number = Math.trunc(Number(rawSlot));
      if (number < 1 || number > 30 || used.has(number)) continue;
      used.add(number);
      slots.push(number);
      if (slots.length >= 10) break;
    }
    if (!slots.length) continue;
    const requestedId = String(panel.id || "").trim().slice(0, 80);
    let id = requestedId && !panels.some((item) => item.id === requestedId) ? requestedId : `panel-${index + 1}`;
    let suffix = 2;
    while (panels.some((item) => item.id === id)) {
      id = `panel-${index + 1}-${suffix}`;
      suffix += 1;
    }
    panels.push({ id, slots });
  }
  return panels.length ? panels : [{ id: "panel-1", slots: [1] }];
}

function normalizeColorSlots(raw: unknown): Record<string, unknown> {
  const defaults = defaultColorSlots();
  const source = isPlainObject(raw) ? raw : {};
  const result: Record<string, unknown> = {};
  for (const [number, defaultRaw] of Object.entries(defaults)) {
    const base = defaultRaw as Record<string, unknown>;
    const item = isPlainObject(source[number]) ? source[number] as Record<string, unknown> : {};
    const name = String(item.name || base.name).trim().slice(0, 80) || String(base.name);
    const roleId = snowflakeFromRaw(item.role_id);
    const hasRoleId = roleId.toString() !== "0";
    let textHex = cleanColor(item.text_hex, String(base.text_hex)).toLowerCase();
    let roleHex = cleanColor(item.role_hex, String(base.role_hex)).toLowerCase();
    const placeholderWhite = !hasRoleId
      && Number(number) !== 30
      && textHex === "#ffffff"
      && roleHex === "#ffffff";
    if (placeholderWhite) {
      textHex = String(base.text_hex).toLowerCase();
      roleHex = String(base.role_hex).toLowerCase();
    }
    result[number] = {
      number: Number(number),
      name,
      text_hex: textHex,
      role_hex: roleHex,
      role_id: roleId,
      role_name: String(item.role_name || name).trim().slice(0, 100) || name,
      managed: Boolean(item.managed),
    };
  }
  return result;
}
function serializeColorSlots(raw: unknown): Record<string, unknown> {
  const source = isPlainObject(raw) ? raw : {};
  return Object.fromEntries(Object.entries(source).map(([key, item]) => {
    const value = isPlainObject(item) ? { ...item } : {};
    value.role_id = serializeSnowflake(value.role_id);
    return [key, value];
  }));
}
function serializeFieldValue(field: DashboardFieldDefinition, value: unknown): unknown {
  if (field.type === "channel" || field.type === "role") return serializeSnowflake(value);
  if (field.type === "role_multi") return Array.isArray(value) ? value.map(serializeSnowflake).filter(Boolean) : [];
  if (field.type === "color_slots") return serializeColorSlots(value);
  if (field.type === "color_panel_layout") return normalizeColorPanelLayout(value);
  return value;
}
const DASHBOARD_PREFIX_FIELD_IDS = new Set([
  "general.bot_prefix",
  "tts.atts_prefix",
  "tts.teto_prefix",
  "tts.gtts_prefix",
  "tts.edge_prefix",
]);

function normalizeDashboardPrefix(raw: unknown, maxLength = 8): string {
  return String(raw ?? "").trim().slice(0, maxLength);
}

function validateDashboardPrefixes(guild: Record<string, unknown>): void {
  const entries: Array<[string, string]> = [
    ["bot", normalizeDashboardPrefix(getPath(guild, "bot_prefix"))],
    ["ATTS", normalizeDashboardPrefix(getPath(guild, "atts_prefix"))],
    ["Kasane Teto", normalizeDashboardPrefix(getPath(guild, "teto_prefix"))],
    ["gTTS", normalizeDashboardPrefix(getPath(guild, "gtts_prefix"))],
    ["Edge", normalizeDashboardPrefix(getPath(guild, "edge_prefix"))],
  ];
  const seen = new Map<string, string>();
  for (const [label, value] of entries) {
    if (!value) throw new Error(`O prefixo de ${label} não pode ficar vazio.`);
    if (/\s/u.test(value) || Array.from(value).some((character) => character.codePointAt(0)! < 33)) {
      throw new Error(`O prefixo de ${label} não pode conter espaços ou caracteres invisíveis.`);
    }
    const other = seen.get(value);
    if (other) throw new Error(`Os prefixos de ${other} e ${label} não podem ser iguais (${value}).`);
    seen.set(value, label);
  }
}

function normalizeFieldValue(field: DashboardFieldDefinition, raw: unknown): unknown {
  if (field.type === "boolean") return raw === true || raw === "true" || raw === "1" || raw === 1;
  if (field.type === "number") {
    const n = Number(raw);
    if (!Number.isFinite(n)) return field.min ?? 0;
    return Math.max(field.min ?? Number.MIN_SAFE_INTEGER, Math.min(field.max ?? Number.MAX_SAFE_INTEGER, Math.trunc(n)));
  }
  if (field.type === "channel" || field.type === "role") return snowflakeFromRaw(raw);
  if (field.type === "role_multi") {
    const values = Array.isArray(raw) ? raw : String(raw ?? "").split(/[\s,;]+/);
    const seen = new Set<string>();
    return values.map((item) => serializeSnowflake(snowflakeFromRaw(item))).filter((item) => item && !seen.has(item) && Boolean(seen.add(item))).slice(0, 25).map(snowflakeToLong);
  }
  if (field.type === "select") {
    const value = String(raw ?? "").trim();
    const allowed = new Set((field.options ?? []).map((item) => item.value));
    return allowed.has(value) ? value : ((field.options ?? [])[0]?.value ?? "");
  }
  if (field.type === "color") return cleanColor(raw);
  if (field.type === "url") {
    let value = String(raw ?? "").trim();
    if ((value.startsWith("<") && value.endsWith(">"))
      || (value.startsWith('"') && value.endsWith('"'))
      || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1).trim();
    value = value.replace(/&amp;/gi, "&");
    if (!value) return "";
    try {
      const parsed = new URL(value);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
      return value.slice(0, field.maxLength ?? 1000);
    } catch {
      return "";
    }
  }
  if (field.type === "string_list") {
    const list = Array.isArray(raw) ? raw : String(raw ?? "").split(/\r?\n/);
    return Array.from(new Set(list.map((item) => String(item).trim().slice(0, 80)).filter(Boolean))).slice(0, 40);
  }
  if (field.type === "form_fields") return normalizeFormFields(raw);
  if (field.type === "color_slots") return normalizeColorSlots(raw);
  if (field.type === "color_panel_layout") return normalizeColorPanelLayout(raw);
  if (DASHBOARD_PREFIX_FIELD_IDS.has(field.id)) return normalizeDashboardPrefix(raw, field.maxLength ?? 8);
  return String(raw ?? "").slice(0, field.maxLength ?? (field.type === "textarea" ? 1800 : 300));
}
function allFields() { return sections.flatMap((section) => section.fields); }
function isConfiguredValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value > 0;
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (isPlainObject(value)) return Object.keys(value).length > 0;
  return true;
}

type DashboardSectionState = {
  enabled: boolean | null;
  state: "active" | "inactive" | "partial" | "configured" | "pending";
  status: string;
};

function hasValue(values: Record<string, unknown>, fieldId: string): boolean {
  return isConfiguredValue(values[fieldId]);
}

function sectionState(sectionId: string, values: Record<string, unknown>): DashboardSectionState {
  if (sectionId === "welcome") {
    const enabled = Boolean(values["welcome.enabled"]);
    const hasChannel = hasValue(values, "welcome.channel_id");
    if (!enabled) return { enabled: false, state: "inactive", status: "Desativada" };
    if (!hasChannel) return { enabled: true, state: "partial", status: "Requer canal" };
    return { enabled: true, state: "active", status: "Ativa" };
  }

  if (sectionId === "birthday") {
    const register = hasValue(values, "birthday.register_channel_id");
    const announce = hasValue(values, "birthday.announce_channel_id");
    if (register && announce) return { enabled: null, state: "active", status: "Ativa" };
    if (register || announce) return { enabled: null, state: "partial", status: "Configuração parcial" };
    return { enabled: null, state: "pending", status: "Não configurada" };
  }

  if (sectionId === "forms") {
    const formChannel = hasValue(values, "forms.form_channel_id");
    const responseChannel = hasValue(values, "forms.responses_channel_id");
    if (formChannel && responseChannel) return { enabled: null, state: "active", status: "Ativa" };
    if (formChannel || responseChannel) return { enabled: null, state: "partial", status: "Configuração parcial" };
    return { enabled: null, state: "pending", status: "Não configurada" };
  }

  if (sectionId === "tickets") {
    const panel = hasValue(values, "tickets.panel.channel_id");
    const category = hasValue(values, "tickets.channels.category_id");
    const staff = hasValue(values, "tickets.roles.staff_role_id");
    const enabledFlow = ["partnership", "report", "suggestion", "other"].some((flow) => Boolean(values[`tickets.enabled.${flow}`]));
    if (panel && category && staff && enabledFlow) return { enabled: null, state: "active", status: "Ativa" };
    // Fluxos possuem padrões habilitados; sozinhos não significam que o módulo foi configurado.
    if (panel || category || staff) return { enabled: null, state: "partial", status: "Configuração parcial" };
    return { enabled: null, state: "pending", status: "Não configurada" };
  }

  if (sectionId === "color_roles") {
    const panel = hasValue(values, "color_roles.channel_id");
    const slots = values["color_roles.slots"];
    const hasLinkedRole = isPlainObject(slots) && Object.values(slots).some((slot) => isPlainObject(slot) && isConfiguredValue(slot.role_id));
    if (panel && hasLinkedRole) return { enabled: null, state: "active", status: "Ativa" };
    if (panel || hasLinkedRole) return { enabled: null, state: "partial", status: "Configuração parcial" };
    return { enabled: null, state: "pending", status: "Não configurada" };
  }

  if (sectionId === "tts") return { enabled: null, state: "configured", status: "Disponível" };
  if (sectionId === "general") return { enabled: null, state: "configured", status: "Configurado" };
  return { enabled: null, state: "configured", status: "Configurado" };
}

export function createDashboardConfigService(options: CreateDashboardConfigServiceOptions): DashboardConfigService {
  let client: MongoClient | null = null;
  let db: Db | null = null;
  let coll: Collection<Document> | null = null;

  async function getCollection(): Promise<Collection<Document>> {
    if (!options.mongoUri) throw new Error("mongodb_not_configured");
    if (coll) return coll;
    client = new MongoClient(options.mongoUri);
    await client.connect();
    db = client.db(options.mongoDbName);
    coll = db.collection(options.mongoCollectionName);
    return coll;
  }
  async function readDoc(guildId: string, type: string, defaults: Record<string, unknown>) {
    const collection = await getCollection();
    const doc = await collection.findOne({ type, guild_id: snowflakeToLong(guildId) }, { projection: { _id: 0 } });
    return deepMerge(defaults, (doc as Record<string, unknown> | null) ?? {});
  }
  async function readAll(guildId: string) {
    const [guild, welcome, birthday] = await Promise.all([
      readDoc(guildId, "guild", defaultGuildDoc(guildId)),
      readDoc(guildId, WELCOME_DOC_CONFIG, defaultWelcomeDoc(guildId)),
      readDoc(guildId, BIRTHDAY_DOC_CONFIG, defaultBirthdayDoc(guildId)),
    ]);
    return { guild, welcome, birthday };
  }
  function valuesFromDocs(docs: Awaited<ReturnType<typeof readAll>>) {
    const values: Record<string, unknown> = {};
    for (const field of allFields()) values[field.id] = serializeFieldValue(field, getPath(docs[field.scope], field.path));
    return values;
  }
  async function saveDocs(guildId: string, patches: Map<DashboardFieldScope, Record<string, unknown>>, changedSections: string[]) {
    const collection = await getCollection();
    const guildIdValue = snowflakeToLong(guildId);
    const jobs: Promise<unknown>[] = [];
    for (const [scope, patch] of patches.entries()) {
      if (!Object.keys(patch).length) continue;
      const type = scope === "guild" ? "guild" : scope === "welcome" ? WELCOME_DOC_CONFIG : BIRTHDAY_DOC_CONFIG;
      jobs.push(collection.updateOne(
        { type, guild_id: guildIdValue },
        { $set: { type, guild_id: guildIdValue, ...patch } },
        { upsert: true },
      ));
    }
    await Promise.all(jobs);
    const revisionResult = await collection.findOneAndUpdate(
      { type: "guild", guild_id: guildIdValue },
      { $set: { type: "guild", guild_id: guildIdValue, dashboard_updated_at: Math.floor(Date.now() / 1000), dashboard_changed_sections: changedSections }, $inc: { dashboard_revision: 1 } },
      { upsert: true, returnDocument: "after", projection: { _id: 0, dashboard_revision: 1 } },
    );
    return typeof revisionResult?.dashboard_revision === "number" ? revisionResult.dashboard_revision : undefined;
  }

  return {
    listSections() { return clone(sections); },
    async getSummary(guildId: string) {
      const docs = await readAll(guildId);
      const values = valuesFromDocs(docs);
      return {
        guildId,
        sections: sections.map((section) => {
          const semantic = sectionState(section.id, values);
          const configured = section.fields.filter((field) => isConfiguredValue(values[field.id])).length;
          return {
            id: section.id, label: section.label, emoji: section.emoji, description: section.description,
            enabled: semantic.enabled, state: semantic.state, configured, total: section.fields.length,
            status: semantic.status,
          };
        }),
      };
    },
    async getSettings(guildId: string) {
      const docs = await readAll(guildId);
      return { guildId, sections: clone(sections), values: valuesFromDocs(docs) };
    },
    async updateSettings(guildId: string, updates: Record<string, unknown>) {
      const docs = await readAll(guildId);
      const fieldsById = new Map(allFields().map((field) => [field.id, field]));
      const patches = new Map<DashboardFieldScope, Record<string, unknown>>();
      const saved: string[] = [];
      const changedSections = new Set<string>();
      for (const [fieldId, rawValue] of Object.entries(updates || {}).slice(0, 250)) {
        const field = fieldsById.get(fieldId);
        if (!field) continue;
        const value = normalizeFieldValue(field, rawValue);
        setPath(docs[field.scope], field.path, value);
        const scopePatch = patches.get(field.scope) ?? {};
        dotSetForPath(scopePatch, field.path, value);
        patches.set(field.scope, scopePatch);
        saved.push(field.id);
        changedSections.add(field.id.split(".")[0] || field.scope);
      }
      if (saved.some((fieldId) => DASHBOARD_PREFIX_FIELD_IDS.has(fieldId))) {
        validateDashboardPrefixes(docs.guild);
      }
      if (saved.includes("color_roles.panel_layout")) {
        const layout = normalizeColorPanelLayout(getPath(docs.guild, "color_roles.panel_layout"));
        const panelCount = layout.length;
        setPath(docs.guild, "color_roles.panel_layout", layout);
        setPath(docs.guild, "color_roles.panel_count", panelCount);
        const guildPatch = patches.get("guild") ?? {};
        dotSetForPath(guildPatch, "color_roles.panel_layout", layout);
        dotSetForPath(guildPatch, "color_roles.panel_count", panelCount);
        patches.set("guild", guildPatch);
      }

      if (saved.length && changedSections.has("welcome")) {
        const channelId = Number(getPath(docs.welcome, "channel_id") || 0);
        const webhook = isPlainObject(getPath(docs.welcome, "webhook"))
          ? { ...(getPath(docs.welcome, "webhook") as Record<string, unknown>) }
          : {};
        webhook.channel_id = Number.isFinite(channelId) && channelId > 0 ? channelId : 0;
        setPath(docs.welcome, "webhook", webhook);
        const welcomePatch = patches.get("welcome") ?? {};
        dotSetForPath(welcomePatch, "webhook.channel_id", webhook.channel_id);
        patches.set("welcome", welcomePatch);
      }
      const changedSectionsList = Array.from(changedSections);
      const revision = saved.length ? await saveDocs(guildId, patches, changedSectionsList) : undefined;
      return { ok: true as const, values: valuesFromDocs(docs), saved, revision, changed_sections: changedSectionsList };
    },
  };
}
