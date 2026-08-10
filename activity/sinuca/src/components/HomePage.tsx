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
      <p>Escolha uma função. Cada área salva apenas as próprias alterações.</p>
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

  return <section className="osk-function-group">
    <header><span><GroupIcon size={15} /><h2>{title}</h2></span><small>{items.length}</small></header>
    <div className="osk-function-grid">
      {items.map((item) => {
        const Icon = item.icon;
        const state = item.state === "active" || item.state === "configured"
          ? "active"
          : item.state === "partial" || item.state === "pending"
            ? "partial"
            : "inactive";
        const status = item.status || (state === "active" ? "Ativo" : state === "partial" ? "Configuração parcial" : "Inativo");
        return <button key={item.id} className="osk-function-card" data-state={state} onClick={() => onOpen(item.id)} aria-label={`${item.label}: ${status}`}>
          <span className="osk-function-icon"><Icon size={20} /></span>
          <span className="osk-function-copy">
            <span className="osk-function-title"><strong>{item.label}</strong><span className="osk-function-state" data-state={state}><i aria-hidden="true" />{status}</span></span>
            <small>{item.description}</small>
          </span>
          <span className="osk-function-arrow"><ArrowRight size={17} /></span>
        </button>;
      })}
    </div>
  </section>;
}
