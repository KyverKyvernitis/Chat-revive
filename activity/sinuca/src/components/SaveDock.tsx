import { RotateCcw, Save } from "lucide-react";

interface SaveDockProps {
  changedCount: number;
  sectionLabel: string;
  saving: boolean;
  onDiscard(): void;
  onSave(): void;
}

export function SaveDock({ changedCount, sectionLabel, saving, onDiscard, onSave }: SaveDockProps) {
  if (changedCount <= 0) return null;
  return <div className="osk-save-dock" role="region" aria-label="Alterações não salvas" aria-live="polite">
    <div className="osk-save-dock-copy">
      <span className="osk-save-dot" />
      <span><strong>Alterações não salvas</strong><small>{changedCount} campo{changedCount === 1 ? "" : "s"} em {sectionLabel}</small></span>
    </div>
    <div className="osk-save-dock-actions">
      <button className="osk-secondary-button osk-secondary-button--small" onClick={onDiscard} disabled={saving}><RotateCcw size={14} />Desfazer</button>
      <button className="osk-primary-button osk-primary-button--small" onClick={onSave} disabled={saving}><Save size={14} />{saving ? "Salvando..." : "Salvar no servidor"}</button>
    </div>
  </div>;
}
