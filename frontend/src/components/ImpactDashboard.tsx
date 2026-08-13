import { BusinessImpact } from "../api/client";

export default function ImpactDashboard({ impact }: { impact: BusinessImpact }) {
  return (
    <div className="panel">
      <h2>Business impact</h2>
      <div className="impact-value">
        ${impact.estimated_impact_value?.toFixed(2) ?? "0.00"}
      </div>
      <p className="muted">estimated impact over the incident window</p>
      <div className="row" style={{ gap: 32, marginTop: 8 }}>
        <div>
          <div className="muted">Affected requests</div>
          <strong>{impact.affected_requests ?? 0}</strong>
        </div>
        <div>
          <div className="muted">Error-rate delta</div>
          <strong>{((impact.error_rate_delta ?? 0) * 100).toFixed(2)}%</strong>
        </div>
      </div>
      <h3>Calculation basis (auditable)</h3>
      <pre>{JSON.stringify(impact.calculation_basis, null, 2)}</pre>
    </div>
  );
}
