"""Incidents: open, deduplicate, classify cascades, resolve, notify.

Policy:

* One active incident per service. A new trigger for a service that already
  has one escalates and annotates that incident instead of paging again.
* Cascade classification. Among active incidents detected within
  ``cascade_window_min`` of each other, an incident whose service has an
  anomalous upstream is marked ``is_cascading`` and points at the root: the
  anomalous upstream with no anomalous upstream of its own (earliest detected
  if several). The whole window is re-classified whenever an incident opens,
  so a root detected *after* its dependents (a downstream service can trip a
  tick earlier) still collects them — one root cause yields one root incident
  with a cascade tree, not N unrelated-looking incidents.
* An incident resolves once its service has been quiet (no detector
  consensus, no fast/medium burn, no rollout in flight) for
  ``incident_resolve_after_s``.
* New incidents are posted to the n8n webhook, if one is configured, which
  runs the RCA -> impact -> notify -> store pipeline.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Incident
from app.services.dependency_graph import DependencyGraph

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
ACTIVE_STATUSES = ("OPEN", "INVESTIGATING")


class N8nNotifier:
    """Fire-and-forget POST to the n8n incident webhook. Never blocks the
    control loop and never raises: notification is best effort."""

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = settings.n8n_webhook_url if url is None else url
        self.sent: list[dict] = []  # kept for inspection in tests

    def _post(self, payload: dict) -> None:
        try:
            httpx.post(self.url, json=payload, timeout=3.0)
        except httpx.HTTPError as exc:
            logger.warning("n8n webhook failed: %s", exc)

    def send(self, payload: dict) -> None:
        self.sent.append(payload)
        if self.url:
            threading.Thread(target=self._post, args=(payload,), daemon=True).start()

    def incident_opened(self, inc: Incident) -> None:
        self.send(
            {
                "event": "incident_opened",
                "incident_id": inc.id,
                "service_name": inc.service_name,
                "severity": inc.severity,
                "is_cascading": bool(inc.is_cascading),
                "root_incident_id": inc.root_incident_id,
                "trigger_reason": inc.trigger_reason,
            }
        )


class IncidentService:
    def __init__(self, notifier: Optional[N8nNotifier] = None) -> None:
        self.notifier = notifier or N8nNotifier()

    def active(self, db: Session, service: str) -> Optional[Incident]:
        return (
            db.execute(
                select(Incident)
                .where(Incident.service_name == service, Incident.status.in_(ACTIVE_STATUSES))
                .order_by(Incident.detected_at.desc())
            )
            .scalars()
            .first()
        )

    def all_active(self, db: Session) -> list[Incident]:
        return list(db.execute(select(Incident).where(Incident.status.in_(ACTIVE_STATUSES))).scalars())

    def open(
        self,
        db: Session,
        service: str,
        reason: str,
        *,
        severity: str,
        now: datetime,
        graph: DependencyGraph,
        deployment_id: Optional[str] = None,
        context: Optional[dict] = None,
        chaos_experiment_id: Optional[str] = None,
    ) -> tuple[Incident, bool]:
        """Open an incident, or fold the trigger into the service's active
        one. Returns (incident, created)."""
        existing = self.active(db, service)
        if existing is not None:
            if SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(existing.severity or "LOW", 0):
                existing.severity = severity
            if deployment_id and not existing.related_deployment_id:
                existing.related_deployment_id = deployment_id
            if chaos_experiment_id and not existing.chaos_experiment_id:
                existing.chaos_experiment_id = chaos_experiment_id
            ctx = dict(existing.detection_context or {})
            ctx["events"] = [*ctx.get("events", []), {"at": now.isoformat(), "reason": reason}]
            existing.detection_context = ctx
            db.commit()
            return existing, False

        inc = Incident(
            service_name=service,
            detected_at=now,
            status="OPEN",
            severity=severity,
            trigger_reason=reason,
            related_deployment_id=deployment_id,
            detection_context=context,
            chaos_experiment_id=chaos_experiment_id,
            is_cascading=False,
            blast_radius_services=graph.blast_radius(service) if service in graph else [],
        )
        db.add(inc)
        db.flush()
        self.classify_cascades(db, graph, now)
        db.commit()
        db.refresh(inc)
        self.notifier.incident_opened(inc)
        logger.info(
            "incident %s opened for %s (%s)%s",
            inc.id,
            service,
            severity,
            f", cascading from root {inc.root_incident_id}" if inc.is_cascading else "",
        )
        return inc, True

    def classify_cascades(self, db: Session, graph: DependencyGraph, now: datetime) -> None:
        """Recompute root/child relationships for active incidents detected
        within the cascade window of ``now``."""
        window = timedelta(minutes=settings.cascade_window_min)
        recent = [
            i for i in self.all_active(db) if abs((i.detected_at - now).total_seconds()) <= window.total_seconds()
        ]
        by_service = {i.service_name: i for i in recent}
        for inc in recent:
            if inc.service_name not in graph:
                continue
            anomalous_up = [u for u in graph.upstream_closure(inc.service_name) if u in by_service]
            if not anomalous_up:
                inc.is_cascading = False
                inc.root_incident_id = None
                continue
            roots = graph.roots_among(anomalous_up)
            root_service = min(
                roots,
                key=lambda r: (by_service[r].detected_at, graph.distance(inc.service_name, r) or 0, r),
            )
            inc.is_cascading = True
            inc.root_incident_id = by_service[root_service].id

    def resolve(self, db: Session, inc: Incident, now: datetime, note: str) -> None:
        inc.status = "RESOLVED"
        inc.resolved_at = now
        ctx = dict(inc.detection_context or {})
        ctx["resolution"] = {"at": now.isoformat(), "note": note}
        inc.detection_context = ctx

    def cascade_tree(self, db: Session, incident_id: str) -> Optional[dict]:
        """Root incident plus every incident attributed to it."""
        inc = db.get(Incident, incident_id)
        if inc is None:
            return None
        root = db.get(Incident, inc.root_incident_id) if inc.root_incident_id else inc
        children = list(
            db.execute(
                select(Incident).where(Incident.root_incident_id == root.id).order_by(Incident.detected_at)
            ).scalars()
        )
        return {"root": root, "children": children}
