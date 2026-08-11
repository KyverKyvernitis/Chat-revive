import { Command, LayoutGrid, LogOut, Settings2, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { SmartAvatar } from "./SmartAvatar";

export type DashboardNavigationPage = "general" | "modules" | "commands";

interface SidebarProps {
  activePage: DashboardNavigationPage;
  mobileOpen: boolean;
  botName?: string;
  botAvatarUrl?: string | null;
  gestureDisabled?: boolean;
  onCloseMobile(): void;
  onOpenMobile(): void;
  onNavigate(page: DashboardNavigationPage): void;
  onLogout(): void;
}

type DrawerGestureMode = "opening" | "closing";

type DrawerPointer = {
  pointerId: number;
  mode: DrawerGestureMode;
  startX: number;
  startY: number;
  latestX: number;
  latestAt: number;
  velocityX: number;
  horizontal: boolean;
};

const MOBILE_BREAKPOINT = 980;
const EDGE_GESTURE_MIN_X = 16;
const EDGE_GESTURE_MAX_X = 144;
const AXIS_LOCK_DISTANCE = 8;
const AXIS_RATIO = 1.12;
const OPEN_DISTANCE = 64;
const CLOSE_DISTANCE = 72;
const OPEN_VELOCITY = 0.45;
const CLOSE_VELOCITY = -0.45;

function isGestureBlockedTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest(
    "input, textarea, select, [contenteditable='true'], .osk-message-editor, .osk-account-layer, .osk-select-layer, [data-no-drawer-gesture]",
  ));
}

function hasBlockingOverlay() {
  return Boolean(document.querySelector(
    ".osk-account-layer[data-visible], .osk-select-layer[data-open], .osk-select-layer[data-visible], .osk-message-editor",
  ));
}

export function Sidebar({
  activePage,
  mobileOpen,
  botName = "Osaka",
  botAvatarUrl,
  gestureDisabled = false,
  onCloseMobile,
  onOpenMobile,
  onNavigate,
  onLogout,
}: SidebarProps) {
  const asideRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const pointerRef = useRef<DrawerPointer | null>(null);
  const visualOpenRef = useRef(mobileOpen);
  const suppressClickUntilRef = useRef(0);
  const [visualOpen, setVisualOpenState] = useState(mobileOpen);
  const [dragging, setDragging] = useState(false);
  const [dragOffset, setDragOffset] = useState(0);

  const setVisualOpen = useCallback((open: boolean) => {
    visualOpenRef.current = open;
    setVisualOpenState(open);
  }, []);

  useEffect(() => {
    setVisualOpen(mobileOpen);
    if (!mobileOpen) {
      pointerRef.current = null;
      setDragging(false);
      setDragOffset(0);
    }
  }, [mobileOpen, setVisualOpen]);

  useEffect(() => {
    const aside = asideRef.current;
    if (!aside) return;

    const syncAccessibility = () => {
      const mobile = window.innerWidth <= MOBILE_BREAKPOINT;
      const hidden = mobile && !visualOpenRef.current && !pointerRef.current;
      if (hidden) {
        aside.setAttribute("inert", "");
        aside.setAttribute("aria-hidden", "true");
      } else {
        aside.removeAttribute("inert");
        aside.removeAttribute("aria-hidden");
      }
    };

    syncAccessibility();
    window.addEventListener("resize", syncAccessibility);
    return () => window.removeEventListener("resize", syncAccessibility);
  }, [visualOpen, dragging]);

  useEffect(() => {
    if (!visualOpen) return;
    const previousOverflow = document.body.style.overflow;
    const syncScrollLock = () => {
      document.body.style.overflow = window.innerWidth <= MOBILE_BREAKPOINT ? "hidden" : previousOverflow;
    };
    syncScrollLock();
    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 120);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setVisualOpen(false);
        onCloseMobile();
        return;
      }
      if (event.key !== "Tab" || window.innerWidth > MOBILE_BREAKPOINT || !asideRef.current) return;
      const focusable = Array.from(asideRef.current.querySelectorAll<HTMLElement>(
        "button:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])",
      ));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", syncScrollLock);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", syncScrollLock);
    };
  }, [onCloseMobile, setVisualOpen, visualOpen]);

  useEffect(() => {
    const drawerWidth = () => asideRef.current?.getBoundingClientRect().width || 280;

    const resetGesture = (settleOpen: boolean) => {
      pointerRef.current = null;
      setDragging(false);
      setDragOffset(0);
      setVisualOpen(settleOpen);
    };

    const onPointerDown = (event: PointerEvent) => {
      if (window.innerWidth > MOBILE_BREAKPOINT || gestureDisabled) return;
      if (!event.isPrimary || event.pointerType === "mouse" || pointerRef.current) return;
      if (hasBlockingOverlay()) return;

      const open = visualOpenRef.current;
      if (!open) {
        if (isGestureBlockedTarget(event.target)) return;
        const maxStart = Math.min(EDGE_GESTURE_MAX_X, window.innerWidth * 0.32);
        if (event.clientX < EDGE_GESTURE_MIN_X || event.clientX > maxStart) return;
      }

      pointerRef.current = {
        pointerId: event.pointerId,
        mode: open ? "closing" : "opening",
        startX: event.clientX,
        startY: event.clientY,
        latestX: event.clientX,
        latestAt: event.timeStamp,
        velocityX: 0,
        horizontal: false,
      };
    };

    const onPointerMove = (event: PointerEvent) => {
      const pointer = pointerRef.current;
      if (!pointer || pointer.pointerId !== event.pointerId) return;

      const deltaX = event.clientX - pointer.startX;
      const deltaY = event.clientY - pointer.startY;
      const intendedDelta = pointer.mode === "opening" ? deltaX : -deltaX;

      if (!pointer.horizontal) {
        if (Math.abs(deltaY) >= AXIS_LOCK_DISTANCE && Math.abs(deltaY) > Math.abs(deltaX) * AXIS_RATIO) {
          resetGesture(pointer.mode === "closing");
          return;
        }
        if (intendedDelta >= AXIS_LOCK_DISTANCE && Math.abs(deltaX) > Math.abs(deltaY) * AXIS_RATIO) {
          pointer.horizontal = true;
          setDragging(true);
          suppressClickUntilRef.current = performance.now() + 280;
        }
      }
      if (!pointer.horizontal) return;

      if (event.cancelable) event.preventDefault();
      const elapsed = Math.max(1, event.timeStamp - pointer.latestAt);
      pointer.velocityX = (event.clientX - pointer.latestX) / elapsed;
      pointer.latestX = event.clientX;
      pointer.latestAt = event.timeStamp;

      const width = drawerWidth();
      const offset = pointer.mode === "opening"
        ? Math.max(-width, Math.min(0, -width + Math.max(0, deltaX)))
        : Math.max(-width, Math.min(0, Math.min(0, deltaX)));
      setDragOffset(offset);
    };

    const finishGesture = (event: PointerEvent, cancelled = false) => {
      const pointer = pointerRef.current;
      if (!pointer || pointer.pointerId !== event.pointerId) return;

      const deltaX = event.clientX - pointer.startX;
      const deltaY = event.clientY - pointer.startY;
      const width = drawerWidth();
      const horizontal = pointer.horizontal
        || (Math.abs(deltaX) >= 42 && Math.abs(deltaX) > Math.abs(deltaY) * AXIS_RATIO);

      if (!horizontal || cancelled) {
        resetGesture(pointer.mode === "closing");
        return;
      }

      suppressClickUntilRef.current = performance.now() + 300;
      if (pointer.mode === "opening") {
        const openedDistance = Math.max(0, deltaX);
        const shouldOpen = openedDistance >= Math.min(OPEN_DISTANCE, width * 0.24)
          || pointer.velocityX >= OPEN_VELOCITY;
        pointerRef.current = null;
        setDragging(false);
        setDragOffset(0);
        setVisualOpen(shouldOpen);
        if (shouldOpen) onOpenMobile();
        return;
      }

      const closedDistance = Math.max(0, -deltaX);
      const shouldClose = closedDistance >= Math.min(CLOSE_DISTANCE, width * 0.24)
        || pointer.velocityX <= CLOSE_VELOCITY;
      pointerRef.current = null;
      setDragging(false);
      setDragOffset(0);
      setVisualOpen(!shouldClose);
      if (shouldClose) onCloseMobile();
    };

    const onPointerUp = (event: PointerEvent) => finishGesture(event);
    const onPointerCancel = (event: PointerEvent) => finishGesture(event, true);
    const onClickCapture = (event: MouseEvent) => {
      if (performance.now() >= suppressClickUntilRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    };

    document.addEventListener("pointerdown", onPointerDown, { capture: true, passive: true });
    document.addEventListener("pointermove", onPointerMove, { capture: true, passive: false });
    document.addEventListener("pointerup", onPointerUp, { capture: true, passive: true });
    document.addEventListener("pointercancel", onPointerCancel, { capture: true, passive: true });
    document.addEventListener("click", onClickCapture, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("pointermove", onPointerMove, true);
      document.removeEventListener("pointerup", onPointerUp, true);
      document.removeEventListener("pointercancel", onPointerCancel, true);
      document.removeEventListener("click", onClickCapture, true);
    };
  }, [gestureDisabled, onCloseMobile, onOpenMobile, setVisualOpen]);

  const close = useCallback(() => {
    setVisualOpen(false);
    onCloseMobile();
  }, [onCloseMobile, setVisualOpen]);

  const width = asideRef.current?.getBoundingClientRect().width || 280;
  const progress = dragging
    ? Math.max(0, Math.min(1, 1 - Math.abs(dragOffset) / width))
    : visualOpen ? 1 : 0;

  return <>
    <button
      type="button"
      className="osk-sidebar-backdrop"
      data-open={visualOpen || undefined}
      data-dragging={dragging || undefined}
      style={{ "--osk-drawer-progress": progress } as CSSProperties}
      onClick={close}
      aria-label="Fechar menu"
      tabIndex={visualOpen ? 0 : -1}
    />
    <aside
      ref={asideRef}
      className="osk-dashboard-sidebar"
      data-open={visualOpen || undefined}
      data-dragging={dragging || undefined}
      style={{ "--osk-drawer-drag-x": `${dragOffset}px` } as CSSProperties}
      aria-label="Navegação do painel"
    >
      <div className="osk-sidebar-brand">
        <span className="osk-sidebar-bot">
          <span className="osk-sidebar-bot-glow" aria-hidden="true" />
          <SmartAvatar className="osk-sidebar-bot-avatar" src={botAvatarUrl} name={botName} type="user" alt={`Avatar da ${botName}`} size={54} />
          <span className="osk-sidebar-bot-copy"><strong>{botName}</strong><small>Painel do bot</small></span>
        </span>
        <button ref={closeRef} type="button" className="osk-sidebar-close" onClick={close} aria-label="Fechar menu"><X size={21} /></button>
      </div>
      <nav>
        <SidebarLink label="Geral" icon={Settings2} index={0} active={activePage === "general"} onClick={() => onNavigate("general")} />
        <SidebarLink label="Módulos" icon={LayoutGrid} index={1} active={activePage === "modules"} onClick={() => onNavigate("modules")} />
        <SidebarLink label="Comandos" icon={Command} index={2} active={activePage === "commands"} onClick={() => onNavigate("commands")} />
      </nav>
      <button className="osk-sidebar-logout" onClick={onLogout}><LogOut size={17} /> Sair do painel</button>
    </aside>
  </>;
}

function SidebarLink({ label, icon: Icon, active, onClick, index }: { label: string; icon: typeof Settings2; active: boolean; onClick(): void; index: number }) {
  return <button className="osk-sidebar-link" style={{ "--osk-menu-index": index } as CSSProperties} data-active={active || undefined} aria-current={active ? "page" : undefined} onClick={onClick}>
    <Icon size={18} />
    <span>{label}</span>
  </button>;
}
