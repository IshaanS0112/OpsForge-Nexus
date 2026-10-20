"""Reliability analytics: delivery, reliability, response and cost in one call."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.db.session import get_db
from app.services import analytics

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
def summary(days: float = Query(7.0, gt=0, le=90), db: Session = Depends(get_db)):
    """Everything an operations review asks for over the last ``days``:
    release frequency and change failure rate, measured availability against
    each SLO, how incidents were caught and how long they lasted, and what
    they cost. Figures are derived from the stored rows on every call."""
    return analytics.summary(db, utcnow(), days=days)
