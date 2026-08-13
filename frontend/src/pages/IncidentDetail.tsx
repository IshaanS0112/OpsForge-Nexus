import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import RCAReportView from "../components/RCAReportView";
import ImpactDashboard from "../components/ImpactDashboard";
import {
  BusinessImpact,
  getBusinessImpact,
  getIncident,
  getRCAReport,
  IncidentDetail as IncidentDetailT,
  RCAReport,
  triggerRCA,
} from "../api/client";

export default function IncidentDetail() {
  const { id = "" } = useParams();
  const [incident, setIncident] = useState<IncidentDetailT | null>(null);
  const [rca, setRca] = useState<RCAReport | null>(null);
  const [impact, setImpact] = useState<BusinessImpact | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getIncident(id).then(setIncident).catch(() => setIncident(null));
    getRCAReport(id).then(setRca).catch(() => setRca(null));
  }, [id]);

  async function runRCA() {
    setBusy(true);
    try {
      setRca(await triggerRCA(id));
      setImpact(await getBusinessImpact(id));
    } finally {
      setBusy(false);
    }
  }

  if (!incident) return <p className="muted">Loading incident…</p>;

  return (
    <div>
      <p>
        <Link to="/">← Dashboard</Link>
      </p>
      <div className="panel">
        <div className="row">
          <h2 className="grow">
            {incident.service_name}{" "}
            <span className={`badge ${incident.severity || ""}`}>{incident.severity}</span>{" "}
            <span className={`badge ${incident.status}`}>{incident.status}</span>
          </h2>
          <button onClick={runRCA} disabled={busy}>
            {busy ? "Analyzing…" : "Trigger RCA + Impact"}
          </button>
        </div>
        <p className="muted">{incident.trigger_reason}</p>

        <h3>Timeline</h3>
        <table>
          <tbody>
            {incident.timeline.map((e, i) => (
              <tr key={i}>
                <td className="mono muted" style={{ width: 180 }}>
                  {new Date(e.at).toLocaleString()}
                </td>
                <td>{e.event}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rca && <RCAReportView report={rca} />}
      {impact && <ImpactDashboard impact={impact} />}

      <div className="panel">
        <h2>Top error signatures (in lookback window)</h2>
        {incident.logs.length === 0 ? (
          <p className="muted">No error logs recorded.</p>
        ) : (
          <ErrorSignatureTable logs={incident.logs} />
        )}
      </div>
    </div>
  );
}

function ErrorSignatureTable({ logs }: { logs: IncidentDetailT["logs"] }) {
  const counts = new Map<string, number>();
  for (const l of logs) {
    if (l.error_signature) counts.set(l.error_signature, (counts.get(l.error_signature) || 0) + 1);
  }
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  return (
    <table>
      <thead>
        <tr>
          <th>Signature</th>
          <th>Count</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([sig, count]) => (
          <tr key={sig}>
            <td className="mono">{sig}</td>
            <td>{count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
