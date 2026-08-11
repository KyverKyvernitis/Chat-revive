import { RotateCcw, WandSparkles } from "lucide-react";

interface MessageJsonEditorProps {
  value: string;
  error: string | null;
  dirty: boolean;
  applying?: boolean;
  onChange(value: string): void;
  onApply?(): void;
  onDiscard?(): void;
}

export function MessageJsonEditor({ value, error, dirty, applying, onChange, onApply, onDiscard }: MessageJsonEditorProps) {
  return (
    <div className="osk-message-json">
      <div className="osk-message-json__head">
        <small>Edite os IDs completos dos campos. A prévia só muda depois de aplicar um JSON válido.</small>
        {dirty && <span className="osk-badge" data-state="changed">não aplicado</span>}
      </div>
      <textarea
        className="osk-message-json__textarea"
        aria-label="JSON da mensagem"
        value={value}
        spellCheck={false}
        aria-invalid={Boolean(error)}
        onChange={(event) => onChange(event.target.value)}
      />
      {error && <p className="osk-message-json__error">{error}</p>}
      <div className="osk-message-json__actions">
        <button type="button" className="osk-secondary-button" disabled={!dirty || applying} onClick={onDiscard}><RotateCcw size={15} />Descartar JSON</button>
        <button type="button" className="osk-primary-button" disabled={!dirty || applying} onClick={onApply}><WandSparkles size={15} />{applying ? "Aplicando..." : "Aplicar JSON"}</button>
      </div>
    </div>
  );
}
