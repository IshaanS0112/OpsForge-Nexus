"""RCA engine — structured signal collection BEFORE any LLM call.

RCA is signal-driven, not a prompt wrapper: correlated signals are collected and
structured before the model is ever called. The order is enforced:

  Step 1  define the lookback window
  Step 2  collect *structured* signals (deployment, metric anomalies, top error
          signatures, correlation flag) — NOT a raw log dump
  Step 3  ONLY now call the LLM with a constrained JSON-only prompt
  Step 4  parse the structured JSON response

If the LLM is unavailable or returns junk, ``_rule_based_fallback`` ranks
candidates by deployment proximity so the system degrades gracefully instead of
returning nothing.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Deployment, Incident, Log, RCAReport
from app.services.anomaly_detector import AnomalyDetectorService

logger = logging.getLogger(__name__)

RCA_SYSTEM_PROMPT = (
    "You are a root-cause analysis assistant. You will receive structured "
    "incident context (deployment info, metric anomalies, error signatures). "
    "Rank the top 3 most likely root causes. For each, state your confidence "
    "(low/medium/high) and cite which specific piece of evidence supports it. "
    "Do not speculate beyond the provided data. Respond in JSON only, as "
    '{"root_cause_candidates": [{"cause": str, "confidence": str, "evidence": str}]}.'
)


class RCAEngine:
    def __init__(self, anomaly_service: Optional[AnomalyDetectorService] = None, llm_client=None):
        self.anomaly_service = anomaly_service or AnomalyDetectorService()
        # llm_client is injectable so tests can pass a stub / force failures.
        self._llm_client = llm_client

    # ---------------- Step 1 + 2: structured signal collection ----------------
    def collect_signals(self, db: Session, incident: Incident) -> dict[str, Any]:
        window_start = incident.detected_at - timedelta(minutes=settings.rca_lookback_minutes)

        recent_deployment = self._recent_deployment(db, incident, window_start)
        metric_anomalies = self.anomaly_service.metric_anomalies(
            db, incident.service_name, since=window_start
        )
        top_errors = self._top_error_signatures(db, incident, window_start)
        correlation_flag = self._correlation_flag(incident, recent_deployment)

        return {
            "incident_id": str(incident.id),
            "service_name": incident.service_name,
            "recent_deployment": recent_deployment,
            "metric_anomalies": metric_anomalies,
            "top_error_signatures": top_errors,
            "correlation_flag": correlation_flag,
        }

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
        }

    def _top_error_signatures(self, db: Session, incident: Incident, since: datetime) -> list[dict]:
        stmt = (
            select(Log.error_signature)
            .where(
                Log.service_name == incident.service_name,
                Log.level == "ERROR",
                Log.logged_at >= since,
                Log.error_signature.isnot(None),
            )
        )
        sigs = [row[0] for row in db.execute(stmt).all()]
        counter = Counter(sigs)
        return [
            {"signature": sig, "count": count}
            for sig, count in counter.most_common(5)
        ]

    def _correlation_flag(self, incident: Incident, recent_deployment: Optional[dict]) -> Optional[str]:
        if recent_deployment and recent_deployment["minutes_before_incident"] <= (
            settings.rca_deployment_correlation_minutes
        ):
            return "deployment_within_5min_of_anomaly_onset"
        return None

    # ---------------- Step 3: constrained LLM call ----------------
    def _call_llm(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Call Claude with a constrained, JSON-only prompt. Raises on any
        failure so the caller can fall back."""
        client = self._llm_client
        if client is None:
            if not settings.anthropic_api_key:
                raise RuntimeError("no anthropic_api_key configured")
            from anthropic import Anthropic

            client = Anthropic(api_key=settings.anthropic_api_key)

        resp = client.messages.create(
            model=settings.llm_model,
            max_tokens=1024,
            system=RCA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(signals)}],
        )
        text = resp.content[0].text if resp.content else "{}"
        return self._parse_llm_json(text)

    @staticmethod
    def _parse_llm_json(text: str) -> dict[str, Any]:
        # Tolerate ```json fences the model sometimes adds.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        data = json.loads(cleaned)
        candidates = data.get("root_cause_candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("LLM response missing root_cause_candidates")
        # validate shape
        for c in candidates:
            if not all(k in c for k in ("cause", "confidence", "evidence")):
                raise ValueError("candidate missing required keys")
        return {"root_cause_candidates": candidates}

    # ---------------- Fallback ----------------
    def _rule_based_fallback(self, signals: dict[str, Any]) -> dict[str, Any]:
        candidates: list[dict] = []
        dep = signals.get("recent_deployment")
        if dep:
            conf = "high" if signals.get("correlation_flag") else "medium"
            candidates.append(
                {
                    "cause": f"Recent deployment {dep['version']} likely introduced the regression",
                    "confidence": conf,
                    "evidence": f"Deployed {dep['minutes_before_incident']} min before incident onset",
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
        if not candidates:
            candidates.append(
                {
                    "cause": "Unclassified anomaly — insufficient correlating signals",
                    "confidence": "low",
                    "evidence": "No recent deployment or dominant error signature found",
                }
            )
        return {"root_cause_candidates": candidates}

    # ---------------- Orchestration ----------------
    def run(self, db: Session, incident: Incident) -> RCAReport:
        signals = self.collect_signals(db, incident)  # Steps 1-2 always run
        model_used = settings.llm_model
        try:
            result = self._call_llm(signals)  # Step 3-4
        except Exception as exc:  # noqa: BLE001 — graceful degradation is the point
            logger.warning("LLM RCA failed (%s); using rule-based fallback", exc)
            result = self._rule_based_fallback(signals)
            model_used = "rule-based-fallback"

        report = RCAReport(
            incident_id=incident.id,
            root_cause_candidates=result["root_cause_candidates"],
            llm_model_used=model_used,
        )
        db.add(report)
        incident.status = "INVESTIGATING"
        db.commit()
        db.refresh(report)
        return report
