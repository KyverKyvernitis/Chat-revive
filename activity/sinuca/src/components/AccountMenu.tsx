import {
  ChevronDown,
  ChevronRight,
  LogOut,
  MessagesSquare,
  RefreshCw,
  Server,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
import type { DashboardServerCard, DashboardUserPayload } from "../types/dashboard";
import { SmartAvatar } from "./SmartAvatar";

interface AccountMenuProps {
  user: DashboardUserPayload;
  currentServer?: Pick<DashboardServerCard, "name" | "icon"> | null;
  busy?: boolean;
  variant?: "landing" | "header";
  serversLabel?: string;
  showServersAction?: boolean;
  supportInviteUrl?: string;
  onServers(): void;
  onRefresh(): void;
  onLogout(): void;
}

const CLOSE_MS = 180;

function identityName(user: DashboardUserPayload) {
  return user.global_name?.trim() || user.username?.trim() || "Conta";
}

export function AccountMenu({
  user,
  currentServer = null,
  busy = false,
  variant = "header",
  serversLabel = currentServer ? "Trocar servidor" : "Meus servidores",
  showServersAction = true,
  supportInviteUrl = "https://discord.gg/RckuzJbvVk",
  onServers,
  onRefresh,
  onLogout,
}: AccountMenuProps) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const sheetRef = useRef<HTMLElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const openFrameOneRef = useRef<number | null>(null);
  const openFrameTwoRef = useRef<number | null>(null);
  const menuId = useId();
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);
  const [position, setPosition] = useState({ top: 0, right: 0, maxHeight: 320 });
  const name = identityName(user);

  const measure = useCallback(() => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const viewportHeight = window.visualViewport?.height ?? window.innerHeight;
    setPosition({
      top: Math.round(rect.bottom + 8),
      right: Math.max(8, Math.round(window.innerWidth - rect.right)),
      maxHeight: Math.max(152, Math.floor(viewportHeight - rect.bottom - 16)),
    });
  }, []);

  const clearOpenFrames = useCallback(() => {
    if (openFrameOneRef.current !== null) window.cancelAnimationFrame(openFrameOneRef.current);
    if (openFrameTwoRef.current !== null) window.cancelAnimationFrame(openFrameTwoRef.current);
    openFrameOneRef.current = null;
    openFrameTwoRef.current = null;
  }, []);

  const open = useCallback(() => {
    clearOpenFrames();
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    measure();
    setMounted(true);
    openFrameOneRef.current = window.requestAnimationFrame(() => {
      openFrameTwoRef.current = window.requestAnimationFrame(() => {
        openFrameOneRef.current = null;
        openFrameTwoRef.current = null;
        setVisible(true);
      });
    });
  }, [clearOpenFrames, measure]);

  const close = useCallback((restoreFocus = true) => {
    clearOpenFrames();
    setVisible(false);
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = window.setTimeout(() => {
      setMounted(false);
      closeTimerRef.current = null;
    }, CLOSE_MS + 40);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
    }
  }, [clearOpenFrames]);

  const toggle = () => {
    if (mounted && visible) close();
    else open();
  };

  useLayoutEffect(() => {
    if (mounted) measure();
  }, [measure, mounted]);

  useEffect(() => {
    if (!mounted) return;
    const focusTimer = visible
      ? window.setTimeout(() => {
        sheetRef.current?.querySelector<HTMLElement>("[role='menuitem']:not([disabled])")?.focus();
      }, 60)
      : null;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      const items = Array.from(sheetRef.current?.querySelectorAll<HTMLElement>("[role='menuitem']:not([disabled])") ?? []);
      if (!items.length) return;
      const current = items.indexOf(document.activeElement as HTMLElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        const next = current < 0
          ? (direction === 1 ? 0 : items.length - 1)
          : (current + direction + items.length) % items.length;
        items[next]?.focus();
      } else if (event.key === "Home") {
        event.preventDefault();
        items[0]?.focus();
      } else if (event.key === "End") {
        event.preventDefault();
        items[items.length - 1]?.focus();
      } else if (event.key === "Tab") {
        event.preventDefault();
        const backwards = event.shiftKey;
        close(false);
        window.requestAnimationFrame(() => {
          const trigger = triggerRef.current;
          if (!trigger) return;
          if (backwards) {
            trigger.focus({ preventScroll: true });
            return;
          }
          const focusable = Array.from(document.querySelectorAll<HTMLElement>(
            "a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])",
          )).filter((element) => !element.closest(".osk-account-layer") && (element === trigger || element.getClientRects().length > 0));
          const triggerIndex = focusable.indexOf(trigger);
          (focusable[triggerIndex + 1] || trigger).focus({ preventScroll: true });
        });
      }
    };
    const onViewportChange = () => measure();
    const visualViewport = window.visualViewport;
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    visualViewport?.addEventListener("resize", onViewportChange);
    visualViewport?.addEventListener("scroll", onViewportChange);
    return () => {
      if (focusTimer !== null) window.clearTimeout(focusTimer);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
      visualViewport?.removeEventListener("resize", onViewportChange);
      visualViewport?.removeEventListener("scroll", onViewportChange);
    };
  }, [close, measure, mounted, visible]);

  useEffect(() => () => {
    clearOpenFrames();
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
  }, [clearOpenFrames]);

  const run = (action: () => void, restoreFocus = true) => {
    close(restoreFocus);
    action();
  };

  return <>
    <button
      ref={triggerRef}
      type="button"
      className="osk-account-trigger"
      data-variant={variant}
      onClick={toggle}
      aria-label={`${visible ? "Fechar" : "Abrir"} menu da conta de ${name}`}
      aria-controls={menuId}
      aria-expanded={visible}
      aria-haspopup="menu"
    >
      <SmartAvatar
        className="osk-account-trigger-avatar"
        src={user.avatarUrl}
        name={name}
        type="user"
        alt={`Avatar de ${name}`}
        size={variant === "landing" ? 32 : 30}
      />
      <span className="osk-account-trigger-copy">
        <small>Conta</small>
        <strong>{name}</strong>
      </span>
      <ChevronDown size={15} aria-hidden="true" />
    </button>

    {mounted && createPortal(
      <div className="osk-account-layer" data-visible={visible || undefined}>
        <button type="button" className="osk-account-backdrop" onClick={() => close()} aria-label="Fechar menu da conta" />
        <section
          id={menuId}
          ref={sheetRef}
          className="osk-account-sheet"
          style={{
            "--osk-account-top": `${position.top}px`,
            "--osk-account-right": `${position.right}px`,
            "--osk-account-max-height": `${position.maxHeight}px`,
          } as CSSProperties}
          role="menu"
          aria-label="Menu da conta"
          aria-hidden={!visible}
        >
          <header className="osk-account-profile">
            <SmartAvatar className="osk-account-profile-avatar" src={user.avatarUrl} name={name} type="user" alt="" size={38} />
            <span>
              <strong>{name}</strong>
              {user.username && <small>@{user.username}</small>}
            </span>
          </header>

          {currentServer && <div className="osk-account-current-server">
            <SmartAvatar src={currentServer.icon} name={currentServer.name} type="server" alt="" size={30} />
            <span><small>Configurando</small><strong>{currentServer.name}</strong></span>
          </div>}

          <nav className="osk-account-actions">
            {showServersAction && <button type="button" role="menuitem" onClick={() => run(onServers, false)}>
              <Server size={17} /><span>{serversLabel}</span><ChevronRight size={15} />
            </button>}
            <button type="button" role="menuitem" onClick={() => run(onRefresh)} disabled={busy}>
              <RefreshCw size={17} className={busy ? "osk-spin" : undefined} /><span>{busy ? "Atualizando..." : "Atualizar dados"}</span>
            </button>
            <a role="menuitem" href={supportInviteUrl} target="_blank" rel="noreferrer noopener" onClick={() => close(false)}>
              <MessagesSquare size={17} /><span>Servidor de suporte</span><ChevronRight size={15} />
            </a>
            <button type="button" role="menuitem" className="osk-account-logout" onClick={() => run(onLogout, false)}>
              <LogOut size={17} /><span>Sair</span>
            </button>
          </nav>
        </section>
      </div>,
      document.body,
    )}
  </>;
}
