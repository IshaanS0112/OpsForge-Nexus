import { useCallback, useEffect, useState } from "react";
import DeploymentStatus from "../components/DeploymentStatus";
import IncidentList from "../components/IncidentList";
import { Incident, listIncidents } from "../api/client";

export default function Dashboard() {
  const [incidents, setIncidents] = useState<Incident[]>([]);

  const refresh = useCallback(() => {
    listIncidents().then(setIncidents).catch(() => setIncidents([]));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <div>
      <DeploymentStatus onChange={refresh} />
      <IncidentList incidents={incidents} />
    </div>
  );
}
