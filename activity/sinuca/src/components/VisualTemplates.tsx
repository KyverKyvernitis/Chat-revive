import { LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties } from "react";

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

export function resolveCompletedImageState(
  image: Pick<HTMLImageElement, "complete" | "naturalWidth"> | null,
): Exclude<ImageState, "loading"> | null {
  if (!image?.complete) return null;
  return image.naturalWidth > 0 ? "ready" : "failed";
}

function useImageState(src: string) {
  const imageRef = useRef<HTMLImageElement>(null);
  const [state, setState] = useState<ImageState>(src ? "loading" : "failed");

  useEffect(() => {
    if (!src) {
      setState("failed");
      return;
    }

    // O evento load nem sempre é reenviado por todos os navegadores quando a
    // imagem vem do cache. Sincronizar complete/naturalWidth impede que o
    // template permaneça invisível após um recarregamento rápido ou restauração.
    setState(resolveCompletedImageState(imageRef.current) || "loading");
  }, [src]);

  return { imageRef, state, setState };
}

export const BUNDLED_DECORATIVE_IMAGE_URL = "/assets/osaka-landing-character.jpg";
export const BUNDLED_LOADING_GIF_URL = "/assets/osaka-loading.gif?v=ba956b47";

const dashboardEnv = (import.meta as ImportMeta & { env?: Partial<ImportMetaEnv> }).env ?? {};
const decorativeImageUrl = configuredValue(dashboardEnv.VITE_DASHBOARD_DECORATIVE_IMAGE_URL) || BUNDLED_DECORATIVE_IMAGE_URL;
const loadingGifUrl = configuredValue(dashboardEnv.VITE_DASHBOARD_LOADING_GIF_URL) || BUNDLED_LOADING_GIF_URL;

/**
 * Arte decorativa da landing page com possibilidade de substituição por ambiente.
 * Aceita PNG, JPG, WebP, GIF ou SVG por caminho local ou URL e desaparece se falhar.
 */
export function DecorativeVisualTemplate() {
  const { imageRef, state, setState } = useImageState(decorativeImageUrl);

  if (!decorativeImageUrl || state === "failed") return null;

  const style: TemplateStyle = {
    "--osk-decorative-size": configuredValue(dashboardEnv.VITE_DASHBOARD_DECORATIVE_IMAGE_SIZE) || "clamp(16rem, 31vw, 29rem)",
    "--osk-decorative-top": configuredValue(dashboardEnv.VITE_DASHBOARD_DECORATIVE_IMAGE_TOP) || "clamp(6rem, 10vw, 9rem)",
    "--osk-decorative-right": configuredValue(dashboardEnv.VITE_DASHBOARD_DECORATIVE_IMAGE_RIGHT) || "clamp(1rem, 7vw, 7rem)",
    "--osk-decorative-opacity": boundedOpacity(dashboardEnv.VITE_DASHBOARD_DECORATIVE_IMAGE_OPACITY, "0.36"),
  };

  return (
    <span
      className="osk-decorative-template"
      style={style}
      aria-hidden="true"
      data-ready={state === "ready" || undefined}
      data-opaque-source={decorativeImageUrl === BUNDLED_DECORATIVE_IMAGE_URL || undefined}
    >
      <img
        ref={imageRef}
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

/** GIF do projeto com fallback imediato para o spinner nativo do painel. */
export function LoadingVisual({ size = 30 }: { size?: number }) {
  const { imageRef, state, setState } = useImageState(loadingGifUrl);
  const style: TemplateStyle = {
    "--osk-loading-visual-size": configuredValue(dashboardEnv.VITE_DASHBOARD_LOADING_GIF_SIZE) || `${Math.max(54, size * 2)}px`,
  };

  return (
    <span
      className="osk-loading-visual"
      style={style}
      aria-hidden="true"
      data-ready={state === "ready" || undefined}
      data-bundled-gif={loadingGifUrl === BUNDLED_LOADING_GIF_URL || undefined}
    >
      {loadingGifUrl && state !== "failed" ? (
        <img
          ref={imageRef}
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

export function LoadingProgress({ progress, label }: { progress: number; label: string }) {
  const bounded = Math.min(100, Math.max(0, Number.isFinite(progress) ? progress : 0));
  const rounded = Math.round(bounded);
  const style: TemplateStyle = { "--osk-loading-progress": `${bounded}%` };

  return (
    <span
      className="osk-loading-progress"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={rounded}
      aria-valuetext={`${rounded}% concluído`}
    >
      <span style={style} aria-hidden="true" />
    </span>
  );
}
