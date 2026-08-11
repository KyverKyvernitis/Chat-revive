import { ArrowRight, LayoutGrid, Settings2 } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";

interface HomePageProps {
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function HomePage({ modules, onOpen }: HomePageProps) {
  const main = modules.filter((item) => item.group === "main");
  const system = modules.filter((item) => item.group === "system");

  return <section className="osk-dashboard-page osk-home-page">
    <header className="osk-home-intro">
      <span className="osk-kicker">Visão geral</span>
      <h1>Configure seu servidor.</h1>
      <p>Escolha uma função para configurar. Cada área salva as próprias alterações.</p>
    </header>

    <FunctionGroup title="Funções" icon={LayoutGrid} items={main} onOpen={onOpen} />
    <FunctionGroup title="Configurações" icon={Settings2} items={system} onOpen={onOpen} />
  </section>;
}

function FunctionGroup({
  title,
  icon: GroupIcon,
  items,
  onOpen,
}: {
  title: string;
  icon: typeof LayoutGrid;
  items: DashboardVisualModule[];
  onOpen(id: string): void;
}) {
  if (!items.length) return null;
  const showsState = title === "Funções";

  return <section className="osk-function-group">
    <header>
      <span><GroupIcon size={15} /><h2>{title}</h2></span>
      {!showsState && <small aria-label={`${items.length} ${items.length === 1 ? "configuração" : "configurações"}`}>{items.length}</small>}
    </header>
    <div className="osk-function-grid">
      {items.map((item) => {
        const Icon = item.icon;
        const state = item.state === "active" ? "active" : "inactive";
        const status = state === "active" ? "Ativa" : "Desativada";
        const accessibleLabel = showsState ? `${item.label}: ${status}` : item.label;
        return <button key={item.id} className="osk-function-card" data-state={showsState ? state : undefined} onClick={() => onOpen(item.id)} aria-label={accessibleLabel}>
          <span className="osk-function-icon"><Icon size={20} /></span>
          <span className="osk-function-copy">
            <span className="osk-function-title"><strong>{item.label}</strong>{showsState && <span className="osk-function-state" data-state={state}><i aria-hidden="true" />{status}</span>}</span>
            <small>{item.description}</small>
          </span>
          <span className="osk-function-arrow"><ArrowRight size={16} /></span>
        </button>;
      })}
    </div>
  </section>;
}
