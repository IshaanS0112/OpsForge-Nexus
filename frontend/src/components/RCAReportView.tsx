import { RCAReport } from "../api/client";

export default function RCAReportView({ report }: { report: RCAReport }) {
  const fallback = report.llm_model_used === "rule-based-fallback";
  return (
    <div className="panel">
      <h2>Root-cause analysis</h2>
      <p className="muted">
        Model:{" "}
        <span className="mono">{report.llm_model_used}</span>{" "}
        {fallback && <span className="badge MEDIUM">rule-based fallback (LLM unavailable)</span>}
      </p>
      {report.root_cause_candidates.map((c, idx) => (
        <div key={idx} style={{ marginBottom: 14 }}>
          <div className="row">
            <strong>
              #{idx + 1} {c.cause}
            </strong>
            <span className={`badge ${c.confidence}`}>{c.confidence}</span>
          </div>
          <div className="evidence">Evidence: {c.evidence}</div>
        </div>
      ))}
    </div>
  );
}
