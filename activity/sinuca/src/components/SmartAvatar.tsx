import { useEffect, useMemo, useState } from "react";
import { guildInitials } from "../moduleCatalog";
import { previewImageCandidates } from "./message-editor/messageEditorUtils";

interface SmartAvatarProps {
  src?: string | null;
  name: string;
  type: "user" | "server";
  size?: number;
  alt?: string;
  className?: string;
  loading?: "eager" | "lazy";
}

/**
 * Avatar/ícone com imagem real e fallback automático para iniciais.
 * Reaproveita as classes visuais existentes (osk-guild-avatar, osk-server-avatar,
 * osk-user-chip-avatar) — só troca o conteúdo interno por <img> quando há uma
 * imagem válida, preservando o fundo roxo/azul como fallback.
 */
export function SmartAvatar({ src, name, type, size, alt, className, loading = "lazy" }: SmartAvatarProps) {
  const candidates = useMemo(() => previewImageCandidates(src), [src]);
  const [candidateIndex, setCandidateIndex] = useState(0);
  const [broken, setBroken] = useState(false);

  useEffect(() => {
    setCandidateIndex(0);
    setBroken(false);
  }, [src]);

  const currentSrc = candidates[candidateIndex] || "";
  const showImage = Boolean(currentSrc) && !broken;
  const fallbackName = name && name.trim() ? name : type === "user" ? "Você" : "Servidor";
  const style = size ? { width: size, height: size } : undefined;

  return (
    <span className={["osk-smart-avatar", className].filter(Boolean).join(" ")} style={style} data-avatar-type={type}>
      {showImage ? (
        <img
          className="osk-avatar-img"
          key={currentSrc}
          src={currentSrc}
          alt={alt ?? fallbackName}
          loading={loading}
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => {
            if (candidateIndex + 1 < candidates.length) {
              setCandidateIndex((current) => current + 1);
              return;
            }
            setBroken(true);
          }}
        />
      ) : (
        <span aria-hidden="true">{guildInitials(fallbackName)}</span>
      )}
    </span>
  );
}
