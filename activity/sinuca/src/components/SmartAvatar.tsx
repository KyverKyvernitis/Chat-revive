import { useEffect, useState } from "react";
import { guildInitials } from "../moduleCatalog";

interface SmartAvatarProps {
  src?: string | null;
  name: string;
  type: "user" | "server";
  size?: number;
  alt?: string;
  className?: string;
}

/**
 * Avatar/ícone com imagem real e fallback automático para iniciais.
 * Reaproveita as classes visuais existentes (osk-guild-avatar, osk-server-avatar,
 * osk-user-chip-avatar) — só troca o conteúdo interno por <img> quando há uma
 * imagem válida, preservando o fundo roxo/azul como fallback.
 */
export function SmartAvatar({ src, name, type, size, alt, className }: SmartAvatarProps) {
  const [broken, setBroken] = useState(false);
  let normalizedSrc = String(src || "").trim();
  if ((normalizedSrc.startsWith("<") && normalizedSrc.endsWith(">"))
    || (normalizedSrc.startsWith('"') && normalizedSrc.endsWith('"'))
    || (normalizedSrc.startsWith("'") && normalizedSrc.endsWith("'"))) {
    normalizedSrc = normalizedSrc.slice(1, -1).trim();
  }
  normalizedSrc = normalizedSrc.replace(/&amp;/gi, "&");

  useEffect(() => {
    setBroken(false);
  }, [normalizedSrc]);

  const showImage = Boolean(normalizedSrc) && !broken;
  const fallbackName = name && name.trim() ? name : type === "user" ? "Você" : "Servidor";
  const style = size ? { width: size, height: size } : undefined;

  return (
    <span className={className} style={style} data-avatar-type={type}>
      {showImage ? (
        <img
          className="osk-avatar-img"
          src={normalizedSrc}
          alt={alt ?? fallbackName}
          loading="eager"
          decoding="async"
          onError={() => setBroken(true)}
        />
      ) : (
        <span aria-hidden="true">{guildInitials(fallbackName)}</span>
      )}
    </span>
  );
}
