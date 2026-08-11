import type { ReactNode } from "react";
import { useState } from "react";
import type { DashboardOptionsPayload } from "../../types/dashboard";

interface DiscordRichTextProps {
  text: string;
  guildOptions?: DashboardOptionsPayload | null;
  compact?: boolean;
  className?: string;
}

type InlineToken =
  | { type: "text"; value: string }
  | { type: "emoji"; raw: string; name: string; id: string; animated: boolean }
  | { type: "userMention"; raw: string; id: string }
  | { type: "roleMention"; raw: string; id: string }
  | { type: "channelMention"; raw: string; id: string }
  | { type: "templateVariable"; raw: string; key: string; mentionKind?: "user" | "channel" };

const DISCORD_TOKEN_RE = /(<a?:[^:\s>]+:\d{15,25}>|<@!?\d{15,25}>|<@&\d{15,25}>|<#\d{15,25}>|\$\{[A-Za-z0-9_]+\}|\{[A-Za-z0-9_]+\})/g;
const INLINE_MARKDOWN_RE = /(\*\*[^*]+\*\*|__[^_]+__|~~[^~]+~~|\|\|[^|]+\|\||`[^`]+`|\*[^*]+\*|\[[^\]]+\]\(https?:\/\/[^)\s]+\)|https?:\/\/[^\s<]+)/g;

function tokenizeDiscordText(text: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let cursor = 0;
  text.replace(DISCORD_TOKEN_RE, (match, _token, offset: number) => {
    if (offset > cursor) tokens.push({ type: "text", value: text.slice(cursor, offset) });
    const emoji = match.match(/^<(a?):([^:\s>]+):(\d{15,25})>$/);
    if (emoji) {
      tokens.push({ type: "emoji", raw: match, animated: emoji[1] === "a", name: emoji[2], id: emoji[3] });
    } else if (/^<@!?\d{15,25}>$/.test(match)) {
      tokens.push({ type: "userMention", raw: match, id: match.replace(/\D/g, "") });
    } else if (/^<@&\d{15,25}>$/.test(match)) {
      tokens.push({ type: "roleMention", raw: match, id: match.replace(/\D/g, "") });
    } else if (/^<#\d{15,25}>$/.test(match)) {
      tokens.push({ type: "channelMention", raw: match, id: match.replace(/\D/g, "") });
    } else {
      const key = match.startsWith("${") ? match.slice(2, -1) : match.slice(1, -1);
      const lower = key.toLowerCase();
      const mentionKind = lower.includes("channel") ? "channel" : lower.includes("mention") || lower.includes("mencao") ? "user" : undefined;
      tokens.push({ type: "templateVariable", raw: match, key, mentionKind });
    }
    cursor = offset + match.length;
    return match;
  });
  if (cursor < text.length) tokens.push({ type: "text", value: text.slice(cursor) });
  return tokens;
}

function renderInlineMarkdown(text: string, prefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  text.replace(INLINE_MARKDOWN_RE, (match, _token, offset: number) => {
    if (offset > cursor) nodes.push(text.slice(cursor, offset));
    const key = `${prefix}-md-${offset}`;
    if (match.startsWith("**") && match.endsWith("**")) {
      nodes.push(<strong key={key}>{match.slice(2, -2)}</strong>);
    } else if (match.startsWith("__") && match.endsWith("__")) {
      nodes.push(<u key={key}>{match.slice(2, -2)}</u>);
    } else if (match.startsWith("~~") && match.endsWith("~~")) {
      nodes.push(<s key={key}>{match.slice(2, -2)}</s>);
    } else if (match.startsWith("||") && match.endsWith("||")) {
      nodes.push(<span key={key} className="osk-discord-spoiler" title="Spoiler">{match.slice(2, -2)}</span>);
    } else if (match.startsWith("`") && match.endsWith("`")) {
      nodes.push(<code key={key}>{match.slice(1, -1)}</code>);
    } else if (match.startsWith("*") && match.endsWith("*")) {
      nodes.push(<em key={key}>{match.slice(1, -1)}</em>);
    } else if (match.startsWith("[") && match.includes("](http")) {
      const parsedLink = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(match);
      nodes.push(parsedLink
        ? <span key={key} className="osk-discord-link" title={parsedLink[2]}>{parsedLink[1]}</span>
        : match);
    } else if (match.startsWith("http://") || match.startsWith("https://")) {
      nodes.push(<span key={key} className="osk-discord-link">{match}</span>);
    } else {
      nodes.push(match);
    }
    cursor = offset + match.length;
    return match;
  });
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

function DiscordEmoji({ token }: { token: Extract<InlineToken, { type: "emoji" }> }) {
  const primaryExtension = token.animated ? "gif" : "webp";
  const fallbackExtension = token.animated ? "webp" : "png";
  const [extension, setExtension] = useState(primaryExtension);
  const [failed, setFailed] = useState(false);

  if (failed) {
    return <span className="osk-discord-emoji-fallback" title={token.raw}>:{token.name}:</span>;
  }

  return (
    <img
      className="osk-discord-emoji"
      src={`https://cdn.discordapp.com/emojis/${token.id}.${extension}?size=44&quality=lossless`}
      alt={`:${token.name}:`}
      title={`:${token.name}:`}
      loading="lazy"
      decoding="async"
      draggable={false}
      onError={() => {
        if (extension !== fallbackExtension) setExtension(fallbackExtension);
        else setFailed(true);
      }}
    />
  );
}

function mentionLabel(token: InlineToken, guildOptions?: DashboardOptionsPayload | null): string {
  if (token.type === "roleMention") {
    const role = guildOptions?.roles?.find((item) => item.id === token.id);
    return `@${role?.name ?? "cargo"}`;
  }
  if (token.type === "channelMention") {
    const channel = guildOptions?.channels?.find((item) => item.id === token.id);
    return `#${channel?.name ?? "canal"}`;
  }
  if (token.type === "templateVariable") {
    if (token.mentionKind === "channel") return "#canal";
    if (token.mentionKind === "user") return token.key.includes("convid") || token.key.includes("inviter") ? "@convidador" : "@membro";
    return token.raw;
  }
  return "@usuário";
}

function renderTextToken(value: string, keyPrefix: string): ReactNode[] {
  const blocks = value.split(/(```[\s\S]*?```)/g);
  const result: ReactNode[] = [];
  blocks.forEach((block, blockIndex) => {
    const blockKey = `${keyPrefix}-block-${blockIndex}`;
    if (block.startsWith("```") && block.endsWith("```")) {
      result.push(<pre key={blockKey} className="osk-discord-codeblock"><code>{block.slice(3, -3).trim()}</code></pre>);
      return;
    }
    const lines = block.split("\n");
    lines.forEach((line, lineIndex) => {
      const quoted = line.startsWith("> ");
      const lineContent = quoted ? line.slice(2) : line;
      const renderedLine = renderInlineMarkdown(lineContent, `${blockKey}-${lineIndex}`);
      if (quoted) result.push(<span key={`${blockKey}-quote-${lineIndex}`} className="osk-discord-quote">{renderedLine}</span>);
      else result.push(...renderedLine);
      if (lineIndex < lines.length - 1) result.push(<br key={`${blockKey}-br-${lineIndex}`} />);
    });
  });
  return result;
}

export function DiscordRichText({ text, guildOptions, compact, className }: DiscordRichTextProps) {
  if (!text.trim()) return null;
  const nodes = tokenizeDiscordText(text).flatMap((token, index): ReactNode[] => {
    const key = `discord-token-${index}-${token.type === "text" ? "text" : token.raw}`;
    if (token.type === "text") return renderTextToken(token.value, key);
    if (token.type === "emoji") return [<DiscordEmoji key={key} token={token} />];
    if (token.type === "roleMention" || token.type === "channelMention" || token.type === "userMention") {
      return [
        <span key={key} className="osk-discord-mention" data-kind={token.type.replace("Mention", "")} title={token.raw}>
          {mentionLabel(token, guildOptions)}
        </span>,
      ];
    }
    if (token.type === "templateVariable" && token.mentionKind) {
      return [
        <span key={key} className="osk-discord-mention" data-kind={token.mentionKind} title={token.raw}>
          {mentionLabel(token, guildOptions)}
        </span>,
      ];
    }
    return [
      <span key={key} className="osk-discord-variable" title="Variável de template">
        {token.raw}
      </span>,
    ];
  });

  return (
    <span className={className ?? "osk-discord-rich-text"} data-compact={compact ? "true" : "false"}>
      {nodes}
    </span>
  );
}
