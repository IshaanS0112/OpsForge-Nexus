import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import DeploymentHistory from "./pages/DeploymentHistory";
import IncidentDetail from "./pages/IncidentDetail";

export default function App() {
  return (
    <div>
      <header className="nav">
        <span className="brand">OpsForge Nexus</span>
        <nav className="row">
          <NavLink to="/" end>
            Dashboard
          </NavLink>
          <NavLink to="/deployments">Deployments</NavLink>
        </nav>
      </header>
      <div className="container">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/deployments" element={<DeploymentHistory />} />
          <Route path="/incidents/:id" element={<IncidentDetail />} />
        </Routes>
      </div>
    </div>
  );
}
