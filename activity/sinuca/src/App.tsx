import { AlertTriangle, ArrowRight, LogIn, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrowserLanding } from "./components/BrowserLanding";
import { CommandsPage } from "./components/CommandsPage";
import { GeneralPage } from "./components/GeneralPage";
import { ModulesPage } from "./components/HomePage";
import { InviteScreen } from "./components/InviteScreen";
import { LegalPage } from "./components/LegalPage";
import { SaveDock } from "./components/SaveDock";
import { SectionEditor } from "./components/SectionEditor";
import { ServerPicker } from "./components/ServerPicker";
import { Sidebar, type DashboardNavigationPage } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { LoadingProgress, LoadingVisual } from "./components/VisualTemplates";
import { mergeDashboardModules, type DashboardVisualModule } from "./moduleCatalog";
import { syncSiteIcon } from "./siteIcon";
import {
  fetchDashboardFull,
  fetchDashboardIdentity,
  fetchDashboardInvite,
  fetchDashboardServers,
  patchDashboardSettings,
  clearDashboardCommandsCache,
} from "./transport/dashboardApi";
import { DashboardHttpError } from "./transport/httpClient";
import { fetchDashboardSession, logoutDashboard, openDiscordLogin } from "./transport/sessionApi";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardSectionSummary,
  DashboardServerCard,
  DashboardSupportServerPayload,
  DashboardUserPayload,
} from "./types/dashboard";

type Route =
  | { page: "landing" }
  | { page: "privacy" }
  | { page: "terms" }
  | { page: "servers" }
  | { page: "invite"; guildId: string }
  | { page: "dashboard"; guildId: string; view: "general" | "modules" | "commands" | "module"; moduleId: string | null };

type DashboardRoute = Extract<Route, { page: "dashboard" }>;
type SessionState = "loading" | "authenticated" | "anonymous";
type BootSessionResult = {
  state: Exclude<SessionState, "loading">;
  user: DashboardUserPayload | null;
  notice?: { type: "error"; text: string };
};

const LOAD_COMPLETE_HOLD_MS = 140;

function isSnowflake(value: string | undefined | null): value is string {
  return Boolean(value && /^\d{15,25}$/.test(value));
}

function parseRoute(pathname = window.location.pathname): Route {
  if (pathname === "/privacy" || pathname === "/privacidade") return { page: "privacy" };
  if (pathname === "/terms" || pathname === "/termos") return { page: "terms" };
  if (pathname === "/dashboard" || pathname === "/dashboard/") return { page: "servers" };
  const invite = pathname.match(/^\/dashboard\/invite\/(\d{15,25})\/?$/);
  if (invite) return { page: "invite", guildId: invite[1] };
  const dashboard = pathname.match(/^\/dashboard\/(\d{15,25})(?:\/(.*?))?\/?$/i);
  if (dashboard) {
    const guildId = dashboard[1];
    const segments = String(dashboard[2] || "").split("/").filter(Boolean);
    if (segments.length === 0 || (segments.length === 1 && ["geral", "general"].includes(segments[0].toLowerCase()))) {
      return { page: "dashboard", guildId, view: "general", moduleId: null };
    }
    if (segments.length === 1 && segments[0].toLowerCase() === "modulos") {
      return { page: "dashboard", guildId, view: "modules", moduleId: null };
    }
    if (segments.length === 1 && segments[0].toLowerCase() === "comandos") {
      return { page: "dashboard", guildId, view: "commands", moduleId: null };
    }
    if (segments.length === 2 && segments[0].toLowerCase() === "modulos" && /^[a-z0-9_-]+$/i.test(segments[1])) {
      return { page: "dashboard", guildId, view: "module", moduleId: segments[1] };
    }
    if (segments.length === 1 && /^[a-z0-9_-]+$/i.test(segments[0])) {
      return { page: "dashboard", guildId, view: "module", moduleId: segments[0] };
    }
    return { page: "dashboard", guildId, view: "modules", moduleId: null };
  }
  return { page: "landing" };
}

function routePath(route: Route): string {
  if (route.page === "privacy") return "/privacy";
  if (route.page === "terms") return "/terms";
  if (route.page === "servers") return "/dashboard";
  if (route.page === "invite") return `/dashboard/invite/${route.guildId}`;
  if (route.page === "dashboard") {
    if (route.view === "modules") return `/dashboard/${route.guildId}/modulos`;
    if (route.view === "commands") return `/dashboard/${route.guildId}/comandos`;
    if (route.view === "module" && route.moduleId) return `/dashboard/${route.guildId}/modulos/${route.moduleId}`;
    return `/dashboard/${route.guildId}/geral`;
  }
  return "/";
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

function normalizeInputValue(field: DashboardFieldDefinition, raw: unknown): unknown {
  if (["role_multi", "string_list", "form_fields", "color_slots", "color_panel_layout"].includes(field.type)) return raw;
  if (field.type === "boolean") return Boolean(raw);
  if (field.type === "number") {
    if (raw === "" || raw === null || raw === undefined) return 0;
    const number = Number(raw);
    return Number.isFinite(number) ? number : 0;
  }
  if (field.type === "channel" || field.type === "role") {
    const match = String(raw ?? "").match(/\d{15,25}/);
    return match?.[0] || "";
  }
  return typeof raw === "string" ? raw : raw ?? "";
}

function errorText(error: unknown): string {
  if (error instanceof DashboardHttpError) {
    const map: Record<string, string> = {
      session_required: "Sua sessão expirou. Entre novamente com o Discord.",
      session_invalid: "Sua sessão do Discord não é mais válida.",
      access_denied: "Sua conta não tem permissão para configurar este servidor.",
      rate_limited: "Muitas solicitações em pouco tempo. Aguarde um momento.",
      session_store_unavailable: "O serviço de sessões está temporariamente indisponível.",
      discord_unavailable: "O Discord está temporariamente indisponível. Tente novamente em instantes.",
      origin_denied: "A origem desta solicitação não foi autorizada.",
    };
    const key = typeof error.payload === "object" && error.payload ? String((error.payload as Record<string, unknown>).error || "") : "";
    return map[key] || error.message;
  }
  return error instanceof Error ? error.message : "Ocorreu uma falha inesperada.";
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute());
  const [sessionState, setSessionState] = useState<SessionState>("loading");
  const [user, setUser] = useState<DashboardUserPayload | null>(null);
  const [botIdentity, setBotIdentity] = useState<DashboardUserPayload | null>(null);
  const [supportServer, setSupportServer] = useState<DashboardSupportServerPayload | null>(null);
  const [manageable, setManageable] = useState<DashboardServerCard[]>([]);
  const [needsInvite, setNeedsInvite] = useState<DashboardServerCard[]>([]);
  const [serversLoaded, setServersLoaded] = useState(false);
  const [loadingServers, setLoadingServers] = useState(false);
  const [selectedServer, setSelectedServer] = useState<DashboardServerCard | null>(null);
  const [sections, setSections] = useState<DashboardSectionDefinition[]>([]);
  const [summary, setSummary] = useState<DashboardSectionSummary[]>([]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [guildOptions, setGuildOptions] = useState<DashboardOptionsPayload | null>(null);
  const [bootProgress, setBootProgress] = useState(0);
  const [loadingDashboard, setLoadingDashboard] = useState(false);
  const [dashboardProgress, setDashboardProgress] = useState(0);
  const [saving, setSaving] = useState(false);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [messageEditorActive, setMessageEditorActive] = useState(false);
  const [commandsRefreshToken, setCommandsRefreshToken] = useState(0);
  const [notice, setNotice] = useState<{ type: "error" | "success" | "info"; text: string } | null>(null);
  const dashboardLoadRef = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const loadedGuildRef = useRef<string | null>(null);
  const activeGuildRef = useRef<string | null>(null);
  const savingRef = useRef(false);

  const visualModules = useMemo(() => mergeDashboardModules(summary), [summary]);
  const selectedSectionId = route.page === "dashboard"
    ? route.view === "general" ? "general" : route.view === "module" ? route.moduleId : null
    : null;
  const selectedSection = useMemo(() => sections.find((section) => section.id === selectedSectionId) ?? null, [sections, selectedSectionId]);
  const selectedModule = useMemo(() => route.page === "dashboard" && route.view === "module"
    ? visualModules.find((item) => item.id === selectedSectionId) ?? null
    : null, [route, selectedSectionId, visualModules]);
  const changedFields = useMemo(() => selectedSection?.fields.filter((field) => !valuesEqual(values[field.id], draft[field.id])) ?? [], [draft, selectedSection, values]);
  const hasUnsavedChanges = changedFields.length > 0;

  const closeMobileMenu = useCallback(() => setMobileMenuOpen(false), []);
  const openMobileMenu = useCallback(() => setMobileMenuOpen(true), []);

  const currentRoutePath = routePath(route);

  useEffect(() => {
    activeGuildRef.current = route.page === "dashboard" ? route.guildId : null;
  }, [route]);

  useEffect(() => () => dashboardLoadRef.current.controller?.abort(), []);

  useEffect(() => {
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => { window.history.scrollRestoration = previous; };
  }, []);

  useEffect(() => {
    syncSiteIcon(botIdentity?.avatarUrl);
  }, [botIdentity?.avatarUrl]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [currentRoutePath]);

  const navigate = useCallback((next: Route, replace = false, bypassGuard = false) => {
    if (!bypassGuard && hasUnsavedChanges) {
      if (!window.confirm("Descartar as alterações que ainda não foram salvas?")) return false;
      setDraft(values);
    }
    const nextGuildId = next.page === "dashboard" ? next.guildId : null;
    if (activeGuildRef.current !== nextGuildId) dashboardLoadRef.current.controller?.abort();
    activeGuildRef.current = nextGuildId;
    const path = routePath(next);
    window.history[replace ? "replaceState" : "pushState"]({}, "", path);
    setRoute(next);
    setMobileMenuOpen(false);
    setNotice(null);
    return true;
  }, [hasUnsavedChanges, values]);

  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    let sessionFinished = false;
    let finishTimer: number | null = null;
    const authError = new URLSearchParams(window.location.search).get("auth_error");
    if (authError) {
      setNotice({ type: "error", text: `Não foi possível concluir o login (${authError}).` });
      window.history.replaceState({}, "", window.location.pathname);
    }
    setBootProgress(0);
    void fetchDashboardIdentity(controller.signal)
      .then((payload) => {
        if (disposed) return;
        setBotIdentity(payload.bot || null);
        setSupportServer(payload.supportServer || null);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!disposed && !sessionFinished) setBootProgress(50);
      });
    void fetchDashboardSession(controller.signal)
      .then((session): BootSessionResult => ({
        state: session.authenticated ? "authenticated" : "anonymous",
        user: session.user || null,
      }))
      .catch((error): BootSessionResult => {
        if (error instanceof DashboardHttpError && error.status === 401) {
          return { state: "anonymous", user: null };
        }
        return { state: "anonymous", user: null, notice: { type: "error", text: errorText(error) } };
      })
      .then((session) => {
        if (disposed) return;
        sessionFinished = true;
        setBootProgress(100);
        finishTimer = window.setTimeout(() => {
          if (disposed) return;
          setUser(session.user);
          if (session.notice) setNotice(session.notice);
          setSessionState(session.state);
        }, LOAD_COMPLETE_HOLD_MS);
      });

    return () => {
      disposed = true;
      controller.abort();
      if (finishTimer !== null) window.clearTimeout(finishTimer);
    };
  }, []);

  useEffect(() => {
    const normalizedPath = routePath(parseRoute(window.location.pathname));
    if (normalizedPath === window.location.pathname) return;
    window.history.replaceState({}, "", `${normalizedPath}${window.location.search}${window.location.hash}`);
  }, []);

  useEffect(() => {
    const onPopState = () => {
      if (messageEditorActive) {
        window.dispatchEvent(new Event("osk:message-editor-back"));
        return;
      }
      if (hasUnsavedChanges) {
        if (!window.confirm("Descartar as alterações que ainda não foram salvas?")) {
          window.history.pushState({}, "", routePath(route));
          return;
        }
        setDraft(values);
      }
      setRoute(parseRoute());
      setNotice(null);
      setMobileMenuOpen(false);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [hasUnsavedChanges, messageEditorActive, route, values]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!hasUnsavedChanges) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasUnsavedChanges]);

  useEffect(() => {
    setMessageEditorActive(false);
  }, [selectedSectionId]);

  const loadServers = useCallback(async (force = false) => {
    if (sessionState !== "authenticated" || (serversLoaded && !force)) return;
    setLoadingServers(true);
    try {
      const payload = await fetchDashboardServers();
      setManageable(payload.manageable || []);
      setNeedsInvite(payload.needsInvite || []);
      if (payload.user) setUser(payload.user);
      setServersLoaded(true);
    } catch (error) {
      if (error instanceof DashboardHttpError && error.status === 401) {
        setSessionState("anonymous");
        setUser(null);
        loadedGuildRef.current = null;
      }
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      setLoadingServers(false);
    }
  }, [serversLoaded, sessionState]);

  useEffect(() => {
    if (sessionState !== "authenticated") return;
    if (["servers", "invite", "dashboard"].includes(route.page)) void loadServers();
  }, [loadServers, route.page, sessionState]);

  const loadDashboard = useCallback(async (guildId: string, quiet = false) => {
    if (!isSnowflake(guildId) || sessionState !== "authenticated") return;
    dashboardLoadRef.current.controller?.abort();
    const controller = new AbortController();
    const generation = dashboardLoadRef.current.generation + 1;
    dashboardLoadRef.current = { generation, controller };
    const changingGuild = loadedGuildRef.current !== guildId;
    if (!quiet) {
      setDashboardProgress(0);
      setLoadingDashboard(true);
      if (changingGuild) {
        setSections([]);
        setSummary([]);
        setValues({});
        setDraft({});
        setGuildOptions(null);
      }
    }
    let completedSuccessfully = false;
    try {
      const payload = await fetchDashboardFull(guildId, controller.signal, (progress) => {
        if (quiet || dashboardLoadRef.current.generation !== generation || activeGuildRef.current !== guildId) return;
        setDashboardProgress((current) => Math.max(current, progress));
      });
      if (dashboardLoadRef.current.generation !== generation || activeGuildRef.current !== guildId) return;
      loadedGuildRef.current = guildId;
      if (payload.user) setUser(payload.user);
      if (payload.bot) setBotIdentity(payload.bot);
      setSections(payload.sections || []);
      setValues(payload.values || {});
      setDraft(payload.values || {});
      setSummary(payload.summary || []);
      setGuildOptions(payload.options || { ok: false, channels: [], roles: [], error: "options_unavailable" });
      setNotice(quiet ? { type: "success", text: "Dados atualizados com os valores persistidos." } : null);
      completedSuccessfully = true;
      if (!quiet) setDashboardProgress(100);
    } catch (error) {
      if (error instanceof DashboardHttpError && error.code === "aborted") return;
      if (dashboardLoadRef.current.generation !== generation) return;
      if (error instanceof DashboardHttpError && error.status === 401) {
        setSessionState("anonymous");
        setUser(null);
        loadedGuildRef.current = null;
      }
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      if (dashboardLoadRef.current.generation === generation) {
        if (!quiet && completedSuccessfully) {
          await new Promise((resolve) => window.setTimeout(resolve, LOAD_COMPLETE_HOLD_MS));
        }
        if (dashboardLoadRef.current.generation === generation) {
          dashboardLoadRef.current.controller = null;
          setLoadingDashboard(false);
        }
      }
    }
  }, [sessionState]);

  const activeDashboardGuildId = route.page === "dashboard" ? route.guildId : null;
  useEffect(() => {
    if (!activeDashboardGuildId || sessionState !== "authenticated") return;
    void loadDashboard(activeDashboardGuildId);
  }, [activeDashboardGuildId, loadDashboard, sessionState]);

  useEffect(() => {
    if (route.page !== "dashboard" || route.view !== "module" || !route.moduleId || sections.length === 0 || selectedSection || loadingDashboard) return;
    const next: DashboardRoute = { page: "dashboard", guildId: route.guildId, view: "modules", moduleId: null };
    window.history.replaceState({}, "", routePath(next));
    setRoute(next);
    setNotice({ type: "info", text: "Esse módulo não existe mais. Voltamos para Módulos." });
  }, [loadingDashboard, route, sections.length, selectedSection]);

  useEffect(() => {
    if (route.page !== "dashboard" || !serversLoaded) return;
    const server = manageable.find((item) => item.id === route.guildId) || null;
    if (server) setSelectedServer(server);
  }, [manageable, route, serversLoaded]);

  const handleLogout = useCallback(async () => {
    if (hasUnsavedChanges && !window.confirm("Sair e descartar as alterações que ainda não foram salvas?")) return;
    try { await logoutDashboard(); } catch { /* O cookie também expira no servidor. */ }
    setUser(null);
    setSessionState("anonymous");
    setManageable([]);
    setNeedsInvite([]);
    setServersLoaded(false);
    loadedGuildRef.current = null;
    navigate({ page: "landing" }, true, true);
  }, [hasUnsavedChanges, navigate]);

  const handleLogin = useCallback(() => {
    openDiscordLogin(route.page === "landing" || route.page === "privacy" || route.page === "terms" ? "/dashboard" : routePath(route));
  }, [route]);

  const handleFieldChange = useCallback((field: DashboardFieldDefinition, raw: unknown) => {
    setDraft((current) => ({ ...current, [field.id]: normalizeInputValue(field, raw) }));
  }, []);

  const handleSave = useCallback(async () => {
    if (route.page !== "dashboard" || !selectedSection || changedFields.length === 0 || savingRef.current) return;
    const guildId = route.guildId;
    savingRef.current = true;
    setSaving(true);
    setNotice(null);
    try {
      const updates = Object.fromEntries(changedFields.map((field) => [field.id, draft[field.id]]));
      const result = await patchDashboardSettings(guildId, updates);
      if (activeGuildRef.current !== guildId) return;
      const mergedValues = { ...values, ...result.values };
      setValues(mergedValues);
      setDraft(mergedValues);
      if (result.summary) setSummary(result.summary);
      if (result.saved.includes("general.bot_prefix")) clearDashboardCommandsCache(guildId);
      const count = result.saved.length;
      setNotice({
        type: "success",
        text: result.summary_error
          ? `${count} alteração${count === 1 ? "" : "ões"} salva${count === 1 ? "" : "s"}. O resumo será atualizado na próxima abertura.`
          : `${count} alteração${count === 1 ? "" : "ões"} salva${count === 1 ? "" : "s"}. O bot sincronizará os módulos compatíveis automaticamente.`,
      });
    } catch (error) {
      if (activeGuildRef.current !== guildId) return;
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, [changedFields, draft, route, selectedSection, values]);

  const openSection = useCallback((sectionId: string) => {
    if (route.page !== "dashboard") return;
    navigate({ page: "dashboard", guildId: route.guildId, view: "module", moduleId: sectionId });
  }, [navigate, route]);

  const navigateDashboardPage = useCallback((page: DashboardNavigationPage) => {
    if (route.page !== "dashboard") return;
    navigate({ page: "dashboard", guildId: route.guildId, view: page, moduleId: null });
  }, [navigate, route]);

  const openInvite = useCallback(async (guildId: string) => {
    if (inviteBusy) return;
    const popup = window.open("about:blank", "_blank");
    if (popup) {
      popup.opener = null;
      popup.document.title = "Abrindo convite da Osaka...";
      popup.document.body.textContent = "Preparando o convite seguro do Discord...";
    }
    setInviteBusy(true);
    setNotice(null);
    try {
      const payload = await fetchDashboardInvite(guildId);
      if (!payload.invite_url) throw new Error("O backend não retornou o endereço do convite.");
      if (popup && !popup.closed) popup.location.replace(payload.invite_url);
      else window.location.assign(payload.invite_url);
    } catch (error) {
      if (popup && !popup.closed) popup.close();
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      setInviteBusy(false);
    }
  }, [inviteBusy]);

  const handleChangeServer = useCallback(() => navigate({ page: "servers" }), [navigate]);
  const handleDiscard = useCallback(() => setDraft(values), [values]);
  const handleRefreshDashboard = useCallback(() => {
    if (route.page !== "dashboard") return;
    if (hasUnsavedChanges && !window.confirm("Recarregar os valores persistidos e descartar as alterações locais?")) return;
    clearDashboardCommandsCache(route.guildId);
    if (route.view === "commands") setCommandsRefreshToken((current) => current + 1);
    void loadDashboard(route.guildId, true);
  }, [hasUnsavedChanges, loadDashboard, route]);

  if (sessionState === "loading") return <FullPageLoading progress={bootProgress} />;

  const protectedRoute = route.page === "servers" || route.page === "invite" || route.page === "dashboard";
  if (protectedRoute && sessionState !== "authenticated") {
    return <LoginRequired onLogin={handleLogin} onHome={() => navigate({ page: "landing" }, true, true)} />;
  }

  return <>
    {notice && <Notice type={notice.type} text={notice.text} onClose={() => setNotice(null)} />}
    {route.page === "landing" && <BrowserLanding loggedIn={sessionState === "authenticated"} user={user} bot={botIdentity} supportServer={supportServer} refreshing={loadingServers} onLogin={handleLogin} onDashboard={() => navigate({ page: "servers" })} onRefresh={() => void loadServers(true)} onLogout={() => void handleLogout()} onNavigate={(path) => navigate(parseRoute(path))} />}
    {route.page === "privacy" && <LegalPage kind="privacy" onBack={() => navigate({ page: "landing" })} />}
    {route.page === "terms" && <LegalPage kind="terms" onBack={() => navigate({ page: "landing" })} />}
    {route.page === "servers" && user && <ServerPicker manageable={manageable} needsInvite={needsInvite} loading={loadingServers} user={user} bot={botIdentity} supportServer={supportServer} onSelect={(server) => { setSelectedServer(server); navigate({ page: "dashboard", guildId: server.id, view: "general", moduleId: null }); }} onInvite={(server) => { setSelectedServer(server); navigate({ page: "invite", guildId: server.id }); }} onRefresh={() => void loadServers(true)} onLogout={() => void handleLogout()} onHome={() => navigate({ page: "landing" })} />}
    {route.page === "invite" && <InviteScreen server={selectedServer || needsInvite.find((item) => item.id === route.guildId) || null} busy={inviteBusy} onBack={() => navigate({ page: "servers" })} onOpenInvite={() => void openInvite(route.guildId)} />}
    {route.page === "dashboard" && <DashboardShell
      route={route}
      selectedServer={selectedServer}
      user={user!}
      botIdentity={botIdentity}
      supportServer={supportServer}
      modules={visualModules}
      selectedSection={selectedSection}
      selectedModule={selectedModule}
      sectionsLoaded={loadedGuildRef.current === route.guildId && sections.length > 0}
      values={values}
      draft={draft}
      guildOptions={guildOptions}
      loading={loadingDashboard}
      loadingProgress={dashboardProgress}
      saving={saving}
      changedCount={changedFields.length}
      mobileMenuOpen={mobileMenuOpen}
      messageEditorActive={messageEditorActive}
      commandsRefreshToken={commandsRefreshToken}
      onCloseMenu={closeMobileMenu}
      onOpenMenu={openMobileMenu}
      onNavigate={navigateDashboardPage}
      onOpenModule={openSection}
      onLogout={() => void handleLogout()}
      onRefresh={handleRefreshDashboard}
      onChangeServer={handleChangeServer}
      onFieldChange={handleFieldChange}
      onMessageEditorActiveChange={setMessageEditorActive}
      onDiscard={handleDiscard}
      onSave={() => void handleSave()}
    />}
  </>;
}

interface DashboardShellProps {
  route: DashboardRoute;
  selectedServer: DashboardServerCard | null;
  user: DashboardUserPayload;
  botIdentity: DashboardUserPayload | null;
  supportServer: DashboardSupportServerPayload | null;
  modules: DashboardVisualModule[];
  selectedSection: DashboardSectionDefinition | null;
  selectedModule: DashboardVisualModule | null;
  sectionsLoaded: boolean;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  loading: boolean;
  loadingProgress: number;
  saving: boolean;
  changedCount: number;
  mobileMenuOpen: boolean;
  messageEditorActive: boolean;
  commandsRefreshToken: number;
  onCloseMenu(): void;
  onOpenMenu(): void;
  onNavigate(page: DashboardNavigationPage): void;
  onOpenModule(sectionId: string): void;
  onLogout(): void;
  onRefresh(): void;
  onChangeServer(): void;
  onFieldChange(field: DashboardFieldDefinition, raw: unknown): void;
  onMessageEditorActiveChange(active: boolean): void;
  onDiscard(): void;
  onSave(): void;
}

function DashboardShell({
  route,
  selectedServer,
  user,
  botIdentity,
  supportServer,
  modules,
  selectedSection,
  selectedModule,
  sectionsLoaded,
  values,
  draft,
  guildOptions,
  loading,
  loadingProgress,
  saving,
  changedCount,
  mobileMenuOpen,
  messageEditorActive,
  commandsRefreshToken,
  onCloseMenu,
  onOpenMenu,
  onNavigate,
  onOpenModule,
  onLogout,
  onRefresh,
  onChangeServer,
  onFieldChange,
  onMessageEditorActiveChange,
  onDiscard,
  onSave,
}: DashboardShellProps) {
  const guildName = selectedServer?.name || `Servidor ${route.guildId.slice(-6)}`;
  const guildIcon = selectedServer?.icon || null;
  const botName = botIdentity?.global_name || botIdentity?.username || "Osaka";
  const activePage: DashboardNavigationPage = route.view === "module" ? "modules" : route.view;
  const editableSection = route.view === "general" || route.view === "module" ? selectedSection : null;


  return <div className="osk-dashboard-shell" data-has-draft={changedCount > 0 || undefined}>
    <Sidebar
      activePage={activePage}
      mobileOpen={mobileMenuOpen}
      botName={botName}
      botAvatarUrl={botIdentity?.avatarUrl}
      onCloseMobile={onCloseMenu}
      onOpenMobile={onOpenMenu}
      gestureDisabled={messageEditorActive}
      onNavigate={onNavigate}
      onLogout={onLogout}
    />
    <div className="osk-dashboard-main">
      <Topbar guildName={guildName} guildIcon={guildIcon} user={user} supportServer={supportServer} busy={loading} onRefresh={onRefresh} onChangeServer={onChangeServer} onLogout={onLogout} onOpenMenu={onOpenMenu} />
      <main className="osk-dashboard-content">
        <div key={`${route.view}:${route.moduleId || "root"}`} className="osk-page-motion">
          {loading && !sectionsLoaded ? <DashboardLoading progress={loadingProgress} /> : route.view === "general" && selectedSection ? (
            <GeneralPage
              section={selectedSection}
              values={values}
              draft={draft}
              guildOptions={guildOptions}
              guildName={guildName}
              guildIcon={guildIcon}
              onChange={onFieldChange}
            />
          ) : route.view === "commands" ? (
            <CommandsPage guildId={route.guildId} refreshToken={commandsRefreshToken} />
          ) : route.view === "module" && selectedSection ? (
            <SectionEditor
              section={selectedSection}
              module={selectedModule}
              values={values}
              draft={draft}
              guildOptions={guildOptions}
              previewBotName={botName}
              previewBotAvatarUrl={botIdentity?.avatarUrl}
              previewGuildName={guildName}
              previewGuildAvatarUrl={guildIcon}
              onChange={onFieldChange}
              onMessageEditorActiveChange={onMessageEditorActiveChange}
              onBack={() => onNavigate("modules")}
            />
          ) : <ModulesPage modules={modules} onOpen={onOpenModule} />}
        </div>
      </main>
    </div>
    {!messageEditorActive && editableSection && <SaveDock changedCount={changedCount} sectionLabel={editableSection.label} saving={saving} onDiscard={onDiscard} onSave={onSave} />}
  </div>;
}

function FullPageLoading({ progress }: { progress: number }) {
  return <div className="osk-full-loading" aria-busy="true"><LoadingVisual size={30} /><LoadingProgress progress={progress} label="Carregando o painel" /></div>;
}

function DashboardLoading({ progress }: { progress: number }) {
  return <div className="osk-dashboard-loading" aria-busy="true"><LoadingVisual size={28} /><LoadingProgress progress={progress} label="Carregando configurações do servidor" /></div>;
}

function LoginRequired({ onLogin, onHome }: { onLogin(): void; onHome(): void }) {
  return <div className="osk-login-required"><div><span><LogIn size={24} /></span><h1>Entre para continuar</h1><p>O painel precisa confirmar sua conta e as permissões do servidor pelo Discord.</p><button className="osk-primary-button" onClick={onLogin}>Entrar com Discord<ArrowRight size={16} /></button><button className="osk-secondary-button" onClick={onHome}>Voltar ao site</button></div></div>;
}

function Notice({ type, text, onClose }: { type: "error" | "success" | "info"; text: string; onClose(): void }) {
  return <div className="osk-global-notice" data-type={type} role="status"><span>{type === "error" ? <AlertTriangle size={17} /> : type === "success" ? <RefreshCw size={17} /> : null}{text}</span><button onClick={onClose} aria-label="Fechar aviso"><X size={16} /></button></div>;
}
