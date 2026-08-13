import { useEffect, useState } from "react";
import { Deployment, listDeployments, rollbackDeployment } from "../api/client";

export default function DeploymentHistory() {
  const [deployments, setDeployments] = useState<Deployment[]>([]);

  const refresh = () => listDeployments().then(setDeployments).catch(() => setDeployments([]));
  useEffect(() => {
    refresh();
  }, []);

  async function rollback(id: string) {
    await rollbackDeployment(id);
    refresh();
  }

  return (
    <div className="panel">
      <h2>Deployment history</h2>
      <table>
        <thead>
          <tr>
            <th>Service</th>
            <th>Version</th>
            <th>Status</th>
            <th>Deployed</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {deployments.map((d) => (
            <tr key={d.id}>
              <td>{d.service_name}</td>
              <td className="mono">{d.version}</td>
              <td>
                <span className={`badge ${d.status}`}>{d.status}</span>
              </td>
              <td className="muted mono">
                {d.deployed_at ? new Date(d.deployed_at).toLocaleString() : "—"}
              </td>
              <td>
                {d.status === "LIVE" && (
                  <button className="danger" onClick={() => rollback(d.id)}>
                    Rollback
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
