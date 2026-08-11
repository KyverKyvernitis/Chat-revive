import { ArrowRight, Boxes, LayoutGrid } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";

interface HomePageProps {
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function ModulesPage({ modules, onOpen }: HomePageProps) {
  const main = modules.filter((item) => item.group === "main");

  return <section className="osk-dashboard-page osk-home-page osk-modules-page">
    <header className="osk-home-intro">
      <span className="osk-kicker">Recursos do bot</span>
      <span className="osk-modules-title"><span aria-hidden="true"><Boxes size={25} /></span><span><h1>Módulos</h1><p>Ative e configure as funções da Osaka de forma independente.</p></span></span>
    </header>

    <FunctionGroup title="Funções do bot" icon={LayoutGrid} items={main} onOpen={onOpen} />
  </section>;
}

export const HomePage = ModulesPage;

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
    <header>
      <span><GroupIcon size={15} /><h2>{title}</h2></span>
    </header>
    <div className="osk-function-grid">
      {items.map((item) => {
        const Icon = item.icon;
        const state = item.state === "active" ? "active" : "inactive";
        const status = state === "active" ? "Ativa" : "Desativada";
        return <button key={item.id} className="osk-function-card" data-state={state} onClick={() => onOpen(item.id)} aria-label={`${item.label}: ${status}`}>
          <span className="osk-function-icon"><Icon size={20} /></span>
          <span className="osk-function-copy">
            <span className="osk-function-title"><strong>{item.label}</strong><span className="osk-function-state" data-state={state}><i aria-hidden="true" />{status}</span></span>
            <small>{item.description}</small>
          </span>
          <span className="osk-function-arrow"><ArrowRight size={16} /></span>
        </button>;
      })}
    </div>
  </section>;
}
