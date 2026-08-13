"""Service layer. Stateful engines (cooldown, anomaly streaks) are exposed as
module-level singletons so their in-memory state survives across API requests
within a worker."""
from app.services.anomaly_detector import AnomalyDetectorService
from app.services.business_impact import BusinessImpactService
from app.services.rca_engine import RCAEngine
from app.services.rollback_engine import RollbackEngine

# Shared singletons (see docs: prod would back cooldown/streak state with Redis).
rollback_engine = RollbackEngine()
anomaly_service = AnomalyDetectorService()
rca_engine = RCAEngine(anomaly_service=anomaly_service)
business_impact_service = BusinessImpactService()

__all__ = [
    "rollback_engine",
    "anomaly_service",
    "rca_engine",
    "business_impact_service",
    "RollbackEngine",
    "AnomalyDetectorService",
    "RCAEngine",
    "BusinessImpactService",
]
