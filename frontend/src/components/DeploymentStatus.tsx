import { useState } from "react";
import { createDeployment, Deployment, getDeployment } from "../api/client";

const TERMINAL = ["LIVE", "ROLLED_BACK"];

const STATE_FLOW = [
  "PREPARING_GREEN",
  "DEPLOYING_GREEN",
  "HEALTH_CHECKING",
  "TRAFFIC_SWITCHING",
  "LIVE",
];

export default function DeploymentStatus({ onChange }: { onChange: () => void }) {
  const [service, setService] = useState("checkout");
  const [version, setVersion] = useState("v1.0.0");
  const [simulateFailure, setSimulateFailure] = useState(false);
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<Deployment | null>(null);

  async function deploy() {
    setBusy(true);
    try {
      // 202: record created at IDLE; the health gate runs in the background.
      const dep = await createDeployment({
        service_name: service,
        version,
        simulate_failure: simulateFailure,
      });
      setLast(dep);
      // Poll until the deployment reaches a terminal state (LIVE / ROLLED_BACK).
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        const cur = await getDeployment(dep.id);
        setLast(cur);
        onChange();
        if (TERMINAL.includes(cur.status)) break;
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <h2>Trigger deployment</h2>
      <div className="row">
        <input value={service} onChange={(e) => setService(e.target.value)} placeholder="service" />
        <input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="version" />
        <label className="check">
          <input
            type="checkbox"
            checked={simulateFailure}
            onChange={(e) => setSimulateFailure(e.target.checked)}
          />
          simulate failure
        </label>
        <button onClick={deploy} disabled={busy}>
          {busy ? "Deploying…" : "Deploy"}
        </button>
      </div>

      {last && (
        <>
          <h3>Blue-green state machine</h3>
          <div className="row mono">
            {STATE_FLOW.map((s) => {
              const reached =
                last.status === "LIVE"
                  ? true
                  : last.status === "ROLLED_BACK"
                  ? ["PREPARING_GREEN", "DEPLOYING_GREEN", "HEALTH_CHECKING"].includes(s)
                  : STATE_FLOW.indexOf(s) <= STATE_FLOW.indexOf(last.status);
              return (
                <span key={s} style={{ opacity: reached ? 1 : 0.35 }}>
                  {s}
                  {s !== "LIVE" ? " →" : ""}
                </span>
              );
            })}
          </div>
          <p className="row" style={{ marginTop: 12 }}>
            Result: <span className={`badge ${last.status}`}>{last.status}</span>
            {last.status === "ROLLED_BACK" && (
              <span className="muted">health gate failed → auto-rollback → incident opened</span>
            )}
          </p>
        </>
      )}
    </div>
  );
}
