import { NavLink, Route, Routes } from "react-router";
import { useTheme, type ThemePref } from "./lib/hooks";
import ChaosPage from "./pages/ChaosPage";
import ConsolePage from "./pages/ConsolePage";
import DeploymentsPage from "./pages/DeploymentsPage";
import IncidentPage from "./pages/IncidentPage";
import IncidentsPage from "./pages/IncidentsPage";
import InsightsPage from "./pages/InsightsPage";

const NAV = [
  { to: "/", label: "Console", end: true },
  { to: "/insights", label: "Insights", end: false },
  { to: "/incidents", label: "Incidents", end: false },
  { to: "/deployments", label: "Deployments", end: false },
  { to: "/chaos", label: "Chaos drills", end: false },
];

const NEXT_THEME: Record<ThemePref, ThemePref> = { dark: "light", light: "system", system: "dark" };

function ThemeToggle() {
  const [pref, setPref] = useTheme();
  return (
    <button type="button" className="btn" onClick={() => setPref(NEXT_THEME[pref])} title="Switch theme" aria-label={`Theme: ${pref}. Switch theme.`}>
      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
        <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor" />
      </svg>
      <span className="hidden sm:inline">{pref === "system" ? "System" : pref === "dark" ? "Dark" : "Light"}</span>
    </button>
  );
}

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-[var(--ring)] bg-[color-mix(in_oklab,var(--bg)_88%,transparent)] backdrop-blur">
        <div className="mx-auto flex max-w-[1680px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <div className="flex items-center gap-2">
            <img src="/favicon.svg" alt="" width={22} height={22} />
            <span className="text-[15px] font-extrabold tracking-tight">OpsForge Nexus</span>
            <span className="eyebrow hidden md:inline">Ops Console</span>
          </div>
          <nav aria-label="Primary" className="order-3 flex w-full gap-1 overflow-x-auto md:order-none md:w-auto">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-lg px-3 py-1.5 text-[13px] font-semibold transition-colors ${
                    isActive ? "bg-panel-2 text-text ring-1 ring-[var(--ring)]" : "text-text-2 hover:text-text"
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto">
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1680px] px-4 pt-4">
        <Routes>
          <Route path="/" element={<ConsolePage />} />
          <Route path="/insights" element={<InsightsPage />} />
          <Route path="/incidents" element={<IncidentsPage />} />
          <Route path="/incidents/:id" element={<IncidentPage />} />
          <Route path="/deployments" element={<DeploymentsPage />} />
          <Route path="/chaos" element={<ChaosPage />} />
          <Route path="*" element={<p className="py-24 text-center text-muted">Nothing here.</p>} />
        </Routes>
      </main>
    </div>
  );
}
