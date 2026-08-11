import { LoaderCircle } from "lucide-react";
import { useEffect, useState, type CSSProperties } from "react";

type ImageState = "loading" | "ready" | "failed";
type TemplateStyle = CSSProperties & Record<`--${string}`, string | number | undefined>;

function configuredValue(value: string | undefined) {
  return value?.trim() || "";
}

function boundedOpacity(value: string | undefined, fallback: string) {
  const normalized = configuredValue(value);
  if (!normalized) return fallback;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? String(Math.min(1, Math.max(0, parsed))) : fallback;
}

function useImageState(src: string) {
  const [state, setState] = useState<ImageState>(src ? "loading" : "failed");

  useEffect(() => {
    setState(src ? "loading" : "failed");
  }, [src]);

  return { state, setState };
}

const decorativeImageUrl = configuredValue(import.meta.env.VITE_DASHBOARD_DECORATIVE_IMAGE_URL);
const loadingGifUrl = configuredValue(import.meta.env.VITE_DASHBOARD_LOADING_GIF_URL);

/**
 * Ponto de troca para uma arte transparente da landing page.
 * Aceita PNG, WebP, GIF ou SVG por caminho local ou URL e desaparece se falhar.
 */
export function DecorativeVisualTemplate() {
  const { state, setState } = useImageState(decorativeImageUrl);

  if (!decorativeImageUrl || state === "failed") return null;

  const style: TemplateStyle = {
    "--osk-decorative-size": configuredValue(import.meta.env.VITE_DASHBOARD_DECORATIVE_IMAGE_SIZE) || "clamp(16rem, 31vw, 29rem)",
    "--osk-decorative-top": configuredValue(import.meta.env.VITE_DASHBOARD_DECORATIVE_IMAGE_TOP) || "clamp(6rem, 10vw, 9rem)",
    "--osk-decorative-right": configuredValue(import.meta.env.VITE_DASHBOARD_DECORATIVE_IMAGE_RIGHT) || "clamp(1rem, 7vw, 7rem)",
    "--osk-decorative-opacity": boundedOpacity(import.meta.env.VITE_DASHBOARD_DECORATIVE_IMAGE_OPACITY, "0.9"),
  };

  return (
    <span className="osk-decorative-template" style={style} aria-hidden="true" data-ready={state === "ready" || undefined}>
      <img
        src={decorativeImageUrl}
        alt=""
        loading="eager"
        decoding="async"
        referrerPolicy="no-referrer"
        onLoad={() => setState("ready")}
        onError={() => setState("failed")}
      />
    </span>
  );
}

/** GIF transparente opcional com fallback para o spinner nativo do painel. */
export function LoadingVisual({ size = 30 }: { size?: number }) {
  const { state, setState } = useImageState(loadingGifUrl);
  const style: TemplateStyle = {
    "--osk-loading-visual-size": configuredValue(import.meta.env.VITE_DASHBOARD_LOADING_GIF_SIZE) || `${Math.max(54, size * 2)}px`,
  };

  return (
    <span className="osk-loading-visual" style={style} aria-hidden="true" data-ready={state === "ready" || undefined}>
      {loadingGifUrl && state !== "failed" ? (
        <img
          src={loadingGifUrl}
          alt=""
          decoding="async"
          referrerPolicy="no-referrer"
          onLoad={() => setState("ready")}
          onError={() => setState("failed")}
        />
      ) : null}
      {state !== "ready" ? <LoaderCircle size={size} className="osk-spin" /> : null}
    </span>
  );
}
