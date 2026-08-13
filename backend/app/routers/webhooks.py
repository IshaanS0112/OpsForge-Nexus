"""Webhook endpoints for n8n orchestration.

n8n posts the combined RCA + business-impact report back here for storage after
running the pipeline, closing the loop described in the architecture doc.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Incident
from app.schemas import N8NCallback

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/n8n-callback")
def n8n_callback(payload: N8NCallback, db: Session = Depends(get_db)):
    """Receive the final combined report from the n8n workflow and mark the
    incident investigated. RCA/impact rows are already persisted by their own
    endpoints; this records that the orchestrated pipeline completed."""
    incident = db.get(Incident, payload.incident_id)
    if not incident:
        raise HTTPException(404, "incident not found")
    if payload.notified and incident.status == "OPEN":
        incident.status = "INVESTIGATING"
        db.commit()
    logger.info("n8n callback stored for incident %s (notified=%s)", payload.incident_id, payload.notified)
    return {"status": "stored", "incident_id": payload.incident_id}
