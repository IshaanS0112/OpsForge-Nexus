"""RCA engine — structured signal collection BEFORE any LLM call.

RCA is signal-driven, not a prompt wrapper: correlated signals are collected and
structured before the model is ever called. The order is enforced:

  Step 1  define the lookback window
  Step 2  collect *structured* signals — NOT a raw log dump:
            recent deployment + rollout history, z-scored metric anomalies,
            top error signatures and the root signatures they propagated
            from, deployment correlation flag, and (V3) the incident's
            dependency context (cascade flag, root, blast radius), detector
            consensus, burn rate, and any chaos drill that was running
  Step 3  ONLY now call the LLM, whose output is constrained to a JSON schema
  Step 4  validate the result

The model only ranks and explains over this already-computed context; it never
decides anomalies, burn rates or rollbacks. If it is unavailable, declines, or
returns nothing usable, ``_rule_based_fallback`` ranks the same signals, so
the system degrades gracefully instead of returning nothing.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import observability
from app.config import settings
from app.models import AnomalyConsensus, ChaosExperiment, Deployment, Incident, Log, RCAReport
from app.services.anomaly_detector import AnomalyDetectorService

logger = logging.getLogger(__name__)

RCA_SYSTEM_PROMPT = (
    "You are a root-cause analysis assistant for a platform reliability team. You receive "
    "structured incident context that deterministic detectors have already computed: the "
    "recent deployment and its rollout history, metric anomalies with z-scores, error "
    "signatures with counts and the upstream root signatures they propagated from, the "
    "service's position in the dependency graph (whether the incident is cascading, the "
    "root service, the blast radius), how many anomaly-detection layers agreed, the SLO "
    "burn rate, and any chaos drill that was running. Rank the top 3 most likely root causes. "
    "For each, give your confidence (low/medium/high) and cite the specific evidence from the "
    "context that supports it, with its numbers. If the incident is cascading, the root "
    "service's failure outranks symptoms observed downstream. If a chaos drill injected a "
    "fault, say so plainly. Do not speculate beyond the provided data."
)


METRIC_LABELS = {
    "latency_p95": "Latency",
    "error_rate": "Error-rate",
    "cpu_usage": "CPU",
    "memory_usage": "Memory",
}


class RCACandidate(BaseModel):
    cause: str
    confidence: Literal["low", "medium", "high"]
    evidence: str


class RCAResult(BaseModel):
    root_cause_candidates: list[RCACandidate]


class LLMUnusable(RuntimeError):
    """The model call returned but produced nothing we can use."""


class RCAEngine:
    def __init__(self, anomaly_service: Optional[AnomalyDetectorService] = None, llm_client=None):
        self.anomaly_service = anomaly_service or AnomalyDetectorService()
        # llm_client is injectable so tests can pass a stub / force failures.
        self._llm_client = llm_client

    # ---------------- Step 1 + 2: structured signal collection ----------------
    def collect_signals(self, db: Session, incident: Incident) -> dict[str, Any]:
        window_start = incident.detected_at - timedelta(minutes=settings.rca_lookback_minutes)
        window_end = incident.resolved_at or incident.detected_at + timedelta(minutes=2)

        recent_deployment = self._recent_deployment(db, incident, window_start)
        signals: dict[str, Any] = {
            "incident_id": str(incident.id),
            "service_name": incident.service_name,
            "detected_at": incident.detected_at.isoformat(),
            "trigger_reason": incident.trigger_reason,
            "recent_deployment": recent_deployment,
            "rollout": self._rollout(db, incident),
            "metric_anomalies": self.anomaly_service.metric_anomalies(db, incident.service_name, since=window_start),
            "top_error_signatures": self._top_error_signatures(db, incident, window_start, window_end),
            "upstream_root_signatures": self._root_signatures(db, incident, window_start, window_end),
            "correlation_flag": self._correlation_flag(incident, recent_deployment),
            "dependency_context": self._dependency_context(db, incident),
            "detector_consensus": self._detector_consensus(db, incident),
            "burn_rate": self._burn_rate(incident),
            "chaos_context": self._chaos_context(db, incident),
        }
        return signals

    def _recent_deployment(self, db: Session, incident: Incident, since: datetime) -> Optional[dict]:
        stmt = (
            select(Deployment)
            .where(
                Deployment.service_name == incident.service_name,
                Deployment.deployed_at <= incident.detected_at,
                Deployment.deployed_at >= since,
            )
            .order_by(Deployment.deployed_at.desc())
            .limit(1)
        )
        dep = db.execute(stmt).scalars().first()
        if not dep:
            return None
        minutes_before = (incident.detected_at - dep.deployed_at).total_seconds() / 60.0
        return {
            "version": dep.version,
            "deployed_at": dep.deployed_at.isoformat(),
            "minutes_before_incident": round(minutes_before, 1),
            "config_diff": dep.config_diff or {},
            "strategy": dep.strategy,
            "status": dep.status,
        }

    @staticmethod
    def _rollout(db: Session, incident: Incident) -> Optional[dict]:
        if not incident.related_deployment_id:
            return None
        dep = db.get(Deployment, incident.related_deployment_id)
        if dep is None:
            return None
        stages = []
        for h in dep.stage_history or []:
            entry = {"stage": h.get("stage"), "at": h.get("at"), "reason": h.get("reason")}
            gate = h.get("gate")
            if gate:
                entry["gate"] = {
                    k: gate.get(k)
                    for k in ("verdict", "canary_requests", "canary_bad", "canary_burn", "stable_burn", "threshold")
                }
            stages.append(entry)
        return {
            "deployment_id": dep.id,
            "version": dep.version,
            "strategy": dep.strategy,
            "status": dep.status,
            "status_reason": dep.status_reason,
            "image": dep.image,
            "stages": stages,
        }

    @staticmethod
    def _top_error_signatures(db: Session, incident: Incident, since: datetime, until: datetime) -> list[dict]:
        rows = db.execute(
            select(Log.error_signature, func.sum(Log.occurrences))
            .where(
                Log.service_name == incident.service_name,
                Log.level == "ERROR",
                Log.logged_at >= since,
                Log.logged_at <= until,
                Log.error_signature.isnot(None),
            )
            .group_by(Log.error_signature)
        ).all()
        counter = Counter({sig: int(n or 0) for sig, n in rows})
        return [{"signature": sig, "count": count} for sig, count in counter.most_common(5)]

    @staticmethod
    def _root_signatures(db: Session, incident: Incident, since: datetime, until: datetime) -> list[dict]:
        """Where this service's errors came from, as reported back up the call
        chain (e.g. checkout's 502s caused by ledger's 500s)."""
        rows = db.execute(
            select(Log.root_signature, func.sum(Log.occurrences))
            .where(
                Log.service_name == incident.service_name,
                Log.logged_at >= since,
                Log.logged_at <= until,
                Log.root_signature.isnot(None),
            )
            .group_by(Log.root_signature)
        ).all()
        counter = Counter({sig: int(n or 0) for sig, n in rows})
        return [{"signature": sig, "count": count} for sig, count in counter.most_common(3)]

    def _correlation_flag(self, incident: Incident, recent_deployment: Optional[dict]) -> Optional[str]:
        if recent_deployment and recent_deployment["minutes_before_incident"] <= (
            settings.rca_deployment_correlation_minutes
        ):
            return "deployment_within_5min_of_anomaly_onset"
        return None

    @staticmethod
    def _dependency_context(db: Session, incident: Incident) -> dict:
        root = db.get(Incident, incident.root_incident_id) if incident.root_incident_id else None
        children = []
        if not incident.is_cascading:
            children = [
                i.service_name
                for i in db.execute(select(Incident).where(Incident.root_incident_id == incident.id)).scalars()
            ]
        return {
            "is_cascading": bool(incident.is_cascading),
            "root_incident_id": incident.root_incident_id,
            "root_service": root.service_name if root else None,
            "blast_radius_services": incident.blast_radius_services or [],
            "cascading_children": children,
        }

    @staticmethod
    def _detector_consensus(db: Session, incident: Incident) -> Optional[dict]:
        ctx = (incident.detection_context or {}).get("consensus")
        if ctx:
            return {
                k: ctx.get(k)
                for k in ("zscore_flagged", "seasonal_flagged", "isolation_forest_flagged", "agreeing_layers")
            }
        row = (
            db.execute(
                select(AnomalyConsensus)
                .where(
                    AnomalyConsensus.service_name == incident.service_name,
                    AnomalyConsensus.window_end <= incident.detected_at + timedelta(minutes=1),
                )
                .order_by(AnomalyConsensus.agreeing_layers.desc(), AnomalyConsensus.window_end.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return {
            "zscore_flagged": row.zscore_flagged,
            "seasonal_flagged": row.seasonal_flagged,
            "isolation_forest_flagged": row.isolation_forest_flagged,
            "agreeing_layers": row.agreeing_layers,
        }

    @staticmethod
    def _burn_rate(incident: Incident) -> Optional[dict]:
        burn = (incident.detection_context or {}).get("burn")
        if not burn:
            return None
        evals = {e["severity"]: e for e in burn.get("evaluations", [])}
        worst = burn.get("worst")
        e = evals.get(worst) or evals.get("FAST_BURN")
        if e is None:
            return None
        return {
            "window": e["long_window"],
            "value": round(e["long_burn"], 2) if e.get("long_burn") is not None else None,
            "short_window": e["short_window"],
            "short_value": round(e["short_burn"], 2) if e.get("short_burn") is not None else None,
            "severity": worst,
            "slo_target": burn.get("slo_target"),
        }

    @staticmethod
    def _chaos_context(db: Session, incident: Incident) -> Optional[dict]:
        exp = db.get(ChaosExperiment, incident.chaos_experiment_id) if incident.chaos_experiment_id else None
        if exp is None and incident.root_incident_id:
            root = db.get(Incident, incident.root_incident_id)
            if root is not None and root.chaos_experiment_id:
                exp = db.get(ChaosExperiment, root.chaos_experiment_id)
        if exp is None:
            return None
        return {
            "experiment_id": exp.id,
            "service_name": exp.service_name,
            "fault_type": exp.fault_type,
            "magnitude": exp.magnitude,
            "target": exp.target,
            "injected_at": exp.injected_at.isoformat() if exp.injected_at else None,
        }

    # ---------------- Step 3: constrained LLM call ----------------
    def _client(self):
        if self._llm_client is not None:
            return self._llm_client
        if not settings.anthropic_api_key:
            raise RuntimeError("no anthropic_api_key configured")
        from anthropic import Anthropic

        return Anthropic(api_key=settings.anthropic_api_key, timeout=settings.llm_timeout_seconds)

    def _call_llm(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Rank causes with the output constrained to ``RCAResult``'s JSON
        schema (structured outputs), so a reply is valid JSON of the right
        shape by construction. Raises on any failure so the caller falls back."""
        response = self._client().beta.messages.parse(
            model=settings.llm_model,
            max_tokens=16000,  # covers adaptive thinking plus the answer
            system=RCA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(signals, default=str)}],
            output_format=RCAResult,
            output_config={"effort": settings.llm_effort},
            # A declined request is re-run server-side on Anthropic's
            # recommended fallback model instead of returning a refusal.
            fallbacks="default",
            betas=["server-side-fallback-2026-07-01"],
        )
        if response.stop_reason == "refusal":
            raise LLMUnusable(f"model declined: {getattr(response.stop_details, 'category', None)}")
        result = response.parsed_output
        if result is None or not result.root_cause_candidates:
            raise LLMUnusable(f"no usable output (stop_reason={response.stop_reason})")
        self._last_model = response.model
        return {"root_cause_candidates": [c.model_dump() for c in result.root_cause_candidates[:3]]}

    # ---------------- Fallback ----------------
    def _rule_based_fallback(self, signals: dict[str, Any]) -> dict[str, Any]:
        candidates: list[dict] = []
        chaos = signals.get("chaos_context")
        dep_ctx = signals.get("dependency_context") or {}
        rollout = signals.get("rollout")
        dep = signals.get("recent_deployment")
        consensus = signals.get("detector_consensus") or {}
        burn = signals.get("burn_rate") or {}

        if chaos:
            candidates.append(
                {
                    "cause": f"Injected fault: chaos drill {chaos['fault_type']} on {chaos['service_name']}",
                    "confidence": "high",
                    "evidence": f"drill {chaos['experiment_id']} injected {chaos['fault_type']} "
                    f"(magnitude {chaos['magnitude']}, target {chaos['target']}) at {chaos['injected_at']}",
                }
            )
        if dep_ctx.get("is_cascading") and dep_ctx.get("root_service"):
            roots = signals.get("upstream_root_signatures") or []
            root_sig = f"; root signature {roots[0]['signature']} x{roots[0]['count']}" if roots else ""
            candidates.append(
                {
                    "cause": f"Upstream failure in {dep_ctx['root_service']} cascading to {signals['service_name']}",
                    "confidence": "high",
                    "evidence": f"root incident {dep_ctx['root_incident_id']} on {dep_ctx['root_service']}, "
                    f"which {signals['service_name']} depends on{root_sig}",
                }
            )
        if rollout and rollout.get("status") in ("ROLLING_BACK", "ROLLED_BACK"):
            failed = next((s for s in rollout["stages"] if s["stage"] == "ROLLING_BACK"), None)
            candidates.append(
                {
                    "cause": f"Release {rollout['version']} ({rollout['strategy']}) failed its rollout gates",
                    "confidence": "high",
                    "evidence": (failed or {}).get("reason") or rollout.get("status_reason") or "rolled back",
                }
            )
        elif dep:
            conf = "high" if signals.get("correlation_flag") else "medium"
            candidates.append(
                {
                    "cause": f"Recent deployment {dep['version']} likely introduced the regression",
                    "confidence": conf,
                    "evidence": f"Deployed {dep['minutes_before_incident']} min before incident onset",
                }
            )
        # The service's own strongest metric regression: the evidence left for
        # a fault that throws no errors, such as added latency.
        anomalies = sorted(signals.get("metric_anomalies") or [], key=lambda a: -a["z_score"])
        if anomalies and not dep_ctx.get("is_cascading"):
            top = anomalies[0]
            layers = consensus.get("agreeing_layers") or 0
            label = METRIC_LABELS.get(top["metric"], top["metric"])
            candidates.append(
                {
                    "cause": f"{label} regression in {signals['service_name']}",
                    "confidence": "medium" if layers >= 2 else "low",
                    "evidence": f"{top['metric']} peaked at {top['value']:.4g} against a baseline of "
                    f"{top['baseline']} (z={top['z_score']})"
                    + (f"; {layers}/3 detector layers agreed" if layers else ""),
                }
            )
        for err in signals.get("top_error_signatures", [])[:2]:
            candidates.append(
                {
                    "cause": f"Elevated errors: {err['signature']}",
                    "confidence": "medium",
                    "evidence": f"{err['count']} occurrences in lookback window",
                }
            )
        if not candidates and consensus.get("agreeing_layers"):
            anomalies = ", ".join(f"{a['metric']} z={a['z_score']}" for a in signals.get("metric_anomalies", [])[:3])
            candidates.append(
                {
                    "cause": f"Unattributed degradation of {signals['service_name']}",
                    "confidence": "low",
                    "evidence": f"{consensus['agreeing_layers']}/3 detector layers agreed"
                    + (f"; {anomalies}" if anomalies else "")
                    + (f"; {burn['severity']} at {burn['value']}x" if burn.get("severity") else ""),
                }
            )
        if not candidates:
            candidates.append(
                {
                    "cause": "Unclassified anomaly — insufficient correlating signals",
                    "confidence": "low",
                    "evidence": "No recent deployment or dominant error signature found",
                }
            )
        return {"root_cause_candidates": candidates[:3]}

    # ---------------- Orchestration ----------------
    def run(self, db: Session, incident: Incident, blind: bool = False) -> RCAReport:
        """``blind`` withholds the chaos drill context, so a drill can test
        whether RCA finds the injected fault from the evidence alone."""
        signals = self.collect_signals(db, incident)  # Steps 1-2 always run
        if blind:
            signals["chaos_context"] = None
            signals["blind"] = True
        self._last_model = None
        try:
            result = self._call_llm(signals)  # Steps 3-4
            model_used = self._last_model or settings.llm_model
        except Exception as exc:  # noqa: BLE001 — graceful degradation is the point
            logger.warning("LLM RCA failed (%s); using rule-based fallback", exc)
            result = self._rule_based_fallback(signals)
            model_used = "rule-based-fallback"
        observability.RCA_REPORTS.labels(path="fallback" if model_used == "rule-based-fallback" else "model").inc()

        report = RCAReport(
            incident_id=incident.id,
            root_cause_candidates=result["root_cause_candidates"],
            llm_model_used=model_used,
            signals=json.loads(json.dumps(signals, default=str)),
        )
        db.add(report)
        if incident.status == "OPEN":
            incident.status = "INVESTIGATING"
        db.commit()
        db.refresh(report)
        return report
