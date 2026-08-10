import {
  ArrowRight,
  Cake,
  ClipboardList,
  DoorOpen,
  LogIn,
  MessagesSquare,
  Palette,
  Ticket,
  Volume2,
} from "lucide-react";
import type { DashboardSupportServerPayload, DashboardUserPayload } from "../types/dashboard";
import { AccountMenu } from "./AccountMenu";
import { SmartAvatar } from "./SmartAvatar";

interface BrowserLandingProps {
  loggedIn: boolean;
  user: DashboardUserPayload | null;
  bot: DashboardUserPayload | null;
  supportServer: DashboardSupportServerPayload | null;
  refreshing?: boolean;
  onLogin(): void;
  onDashboard(): void;
  onRefresh(): void;
  onLogout(): void;
  onNavigate(path: string): void;
}

const features = [
  { icon: DoorOpen, title: "Boas-vindas" },
  { icon: Ticket, title: "Tickets" },
  { icon: ClipboardList, title: "Formulários" },
  { icon: Palette, title: "Cargos de cor" },
  { icon: Cake, title: "Aniversários" },
  { icon: Volume2, title: "TTS" },
];

function displayName(identity: DashboardUserPayload | null, fallback: string) {
  return identity?.global_name?.trim() || identity?.username?.trim() || fallback;
}

export function Brand({
  compact = false,
  bot = null,
}: {
  compact?: boolean;
  bot?: DashboardUserPayload | null;
}) {
  const botName = displayName(bot, "Osaka");
  return (
    <span className="osk-brand-lockup" data-compact={compact || undefined}>
      <span className="osk-brand-symbol" aria-hidden="true"><span className="osk-yin-yang-mark" /></span>
      <span className="osk-brand-copy"><strong>{botName}</strong><small>Painel</small></span>
    </span>
  );
}

export function BrowserLanding({
  loggedIn,
  user,
  bot,
  supportServer,
  refreshing = false,
  onLogin,
  onDashboard,
  onRefresh,
  onLogout,
  onNavigate,
}: BrowserLandingProps) {
  const primaryAction = loggedIn ? onDashboard : onLogin;
  const supportInviteUrl = supportServer?.inviteUrl || "https://discord.gg/RckuzJbvVk";

  return (
    <div className="osk-minimal-landing">
      <header className="osk-minimal-nav">
        <button className="osk-brand-button" onClick={() => onNavigate("/")} aria-label="Página inicial">
          <Brand bot={bot} />
        </button>
        {loggedIn && user ? (
          <AccountMenu
            user={user}
            variant="landing"
            busy={refreshing}
            supportInviteUrl={supportInviteUrl}
            serversLabel="Meus servidores"
            onServers={onDashboard}
            onRefresh={onRefresh}
            onLogout={onLogout}
          />
        ) : (
          <button className="osk-minimal-login" onClick={onLogin} aria-label="Entrar com Discord">
            <LogIn size={16} /><span>Entrar</span>
          </button>
        )}
      </header>

      <main className="osk-minimal-main">
        <section className="osk-minimal-hero">
          <span className="osk-minimal-kicker">Controle com clareza</span>
          <h1><span>Seu servidor,</span><strong>em equilíbrio.</strong></h1>
          <p>Escolha um servidor, ajuste cada função e publique as mudanças com segurança.</p>
          <button className="osk-primary-button osk-minimal-primary-action" onClick={primaryAction}>
            {loggedIn ? "Abrir painel" : "Entrar com Discord"}
            <ArrowRight size={17} />
          </button>
        </section>

        <section className="osk-minimal-features" aria-labelledby="landing-features-title">
          <h2 id="landing-features-title">Funções</h2>
          <div className="osk-minimal-feature-grid">
            {features.map((feature) => (
              <article key={feature.title}>
                <span aria-hidden="true"><feature.icon size={20} /></span>
                <h3>{feature.title}</h3>
              </article>
            ))}
          </div>
        </section>

        <a
          className="osk-support-card"
          href={supportInviteUrl}
          target="_blank"
          rel="noreferrer noopener"
          aria-label="Entrar no servidor de suporte da Osaka"
        >
          {supportServer?.icon ? (
            <SmartAvatar
              className="osk-support-card-avatar"
              src={supportServer.icon}
              name={supportServer.name}
              type="server"
              alt={`Ícone do ${supportServer.name}`}
              size={42}
            />
          ) : (
            <span className="osk-support-card-icon" aria-hidden="true"><MessagesSquare size={20} /></span>
          )}
          <span className="osk-support-card-copy">
            <strong>Servidor de suporte</strong>
            <small>Dúvidas, problemas e novidades da Osaka.</small>
          </span>
          <span className="osk-support-card-action"><span>Entrar</span> <ArrowRight size={15} /></span>
        </a>
      </main>
    </div>
  );
}
