import { Link } from "react-router-dom";
import { Incident } from "../api/client";

export default function IncidentList({ incidents }: { incidents: Incident[] }) {
  return (
    <div className="panel">
      <h2>Incidents</h2>
      {incidents.length === 0 ? (
        <p className="muted">No incidents. Deploy with "simulate failure" to generate one.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Service</th>
              <th>Severity</th>
              <th>Status</th>
              <th>Trigger</th>
              <th>Detected</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {incidents.map((i) => (
              <tr key={i.id}>
                <td>{i.service_name}</td>
                <td>
                  <span className={`badge ${i.severity || ""}`}>{i.severity || "—"}</span>
                </td>
                <td>
                  <span className={`badge ${i.status}`}>{i.status}</span>
                </td>
                <td className="muted">{i.trigger_reason}</td>
                <td className="muted mono">
                  {i.detected_at ? new Date(i.detected_at).toLocaleTimeString() : "—"}
                </td>
                <td>
                  <Link to={`/incidents/${i.id}`}>Investigate →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
