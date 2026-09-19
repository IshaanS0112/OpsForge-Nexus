"""The control loop: one reconciliation tick every ``control_loop_tick_s``.

Each tick, in order:

    1. drain the gateway's measurements of the last tick's edge traffic
    2. scrape pod CPU / memory
    3. run the three-layer anomaly suite per service (dependency order, so an
       upstream root is classified before its dependents)
    4. persist metrics (with every layer's output), SLI counts, error
       signatures, consensus rows
    5. evaluate multiwindow burn rates; record alert transitions
    6. open incidents where >= 2 layers agree or FAST_BURN pages
       (cascade-classified)
    7. feed presumed-normal readings to the Isolation Forest
    8. advance every in-flight rollout (deployment controller)
    9. post-deploy rollback check for recently LIVE releases
   10. advance chaos drills
   11. resolve incidents whose service has been quiet long enough
   12. retention / downsampling, then publish a snapshot for the dashboard

Everything left of the RCA engine is deterministic code; the LLM never
decides anomalies, burn rates or rollbacks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import catalog
from app.clock import utcnow
from app.config import settings
from app.db import session as db_session
from app.models import AnomalyConsensus, Deployment, Incident, Log, Metric, ServiceDependency, SLISample, SLOConfig
from app.services.anomaly_suite import AnomalySuite, SuiteResult, epoch
from app.services.burn_rate_engine import BurnRateService, ServiceBurn
from app.services.canary_engine import CanaryEngine
from app.services.chaos_engine import ChaosEngine
from app.services.dependency_graph import DependencyGraph
from app.services.deployment_controller import DeploymentController
from app.services.deployment_engine import ACTIVE_STATES
from app.services.gateway import Gateway, HttpTransport, LoadGenerator, RoutingTable, SimTransport, TrafficRecorder
from app.services.incident_service import SEVERITY_ORDER, IncidentService, N8nNotifier
from app.services.k8s_client import build_cluster
from app.services.rollback_engine import RollbackEngine
from app.services.telemetry import Telemetry

logger = logging.getLogger(__name__)

SEVERITY_FOR_BURN = {"FAST_BURN": "CRITICAL", "MEDIUM_BURN": "HIGH", "SLOW_BURN": "MEDIUM"}


def seed_catalog(db: Session) -> None:
    """Hand-defined graph and SLOs: seeded once, editable afterwards."""
    if db.execute(select(func.count(ServiceDependency.id))).scalar() == 0:
        for service, dep in catalog.edges():
            db.add(ServiceDependency(service_name=service, depends_on_service=dep))
    existing = {r[0] for r in db.execute(select(SLOConfig.service_name)).all()}
    for spec in catalog.CATALOG:
        if spec.name not in existing:
            db.add(
                SLOConfig(
                    service_name=spec.name,
                    slo_target=settings.slo_default_target,
                    window_days=settings.slo_default_window_days,
                    latency_threshold_ms=spec.slo_latency_ms,
                )
            )
    db.commit()


class Runtime:
    def __init__(
        self,
        cluster=None,
        *,
        rollback: Optional[RollbackEngine] = None,
        tick_s: float = settings.control_loop_tick_s,
        loadgen: bool = settings.loadgen_enabled,
        notifier: Optional[N8nNotifier] = None,
    ) -> None:
        self.cluster = cluster if cluster is not None else build_cluster()
        self.tick_s = tick_s
        self.routing = RoutingTable()
        self.recorder = TrafficRecorder()
        self.transport = SimTransport(self.cluster) if self.cluster.mode == "simulated" else HttpTransport(self.cluster)
        self.gateway = Gateway(self.transport, self.routing, self.recorder)
        self.loadgen = LoadGenerator(self.gateway, catalog.names(), ready=lambda: self.serving) if loadgen else None
        self.serving = False  # every live colour Ready: synthetic traffic may flow
        self.burn = BurnRateService()
        self.canary = CanaryEngine(self.burn)
        self.rollback = rollback or RollbackEngine()
        self.notifier = notifier or N8nNotifier()
        self.incidents = IncidentService(self.notifier)
        self.suite = AnomalySuite()
        self.telemetry = Telemetry(self.cluster)
        self.chaos = ChaosEngine(self.cluster, self.notifier)
        self.deployments = DeploymentController(
            self.cluster, self.gateway, self.routing, self.canary, self.rollback, self.incidents, self.graph
        )
        self._graph: Optional[DependencyGraph] = None
        self._tick_lock = threading.Lock()
        self.bootstrapped = False
        self.cluster_error: Optional[str] = None
        self.tick_count = 0
        self.last_tick_at: Optional[datetime] = None
        self.last_tick_ms: Optional[float] = None
        self.results: dict[str, SuiteResult] = {}
        self.burns: dict[str, ServiceBurn] = {}
        self.quiet_since: dict[str, datetime] = {}
        self.values: dict[str, dict[str, Optional[float]]] = {}
        self.traffic: dict[str, dict[str, Any]] = {}
        self._acting: set[str] = set()  # services with >= 2 layers agreeing last tick
        self._paging: set[str] = set()  # services with FAST_BURN firing last tick
        self._last_retention: Optional[datetime] = None
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self.running = False

    # ---------------------------------------------------------------- graph --
    def graph(self, db: Session) -> DependencyGraph:
        if self._graph is None:
            g = DependencyGraph.from_db(db)
            self._graph = DependencyGraph(g.edges(), nodes=[*g.nodes(), *catalog.names()])
        return self._graph

    def invalidate_graph(self) -> None:
        self._graph = None

    def service_order(self, db: Session) -> list[str]:
        order = [s for s in self.graph(db).topological_order() if catalog.get(s)]
        return order + [s for s in catalog.names() if s not in order]

    # ------------------------------------------------------------ bootstrap --
    def bootstrap(self, db: Session, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        seed_catalog(db)
        self.invalidate_graph()
        for cfg in db.execute(select(SLOConfig)).scalars():
            self.recorder.set_latency_bound(cfg.service_name, cfg.latency_threshold_ms)
        try:
            self.cluster.ensure_base()
            for spec in catalog.CATALOG:
                self.cluster.ensure_service(spec)
            self.deployments.restore_routing(db)
        except Exception as exc:  # noqa: BLE001 — retried every tick; the API stays up meanwhile
            self.cluster_error = f"{type(exc).__name__}: {exc}"
            logger.warning("cluster bootstrap failed (%s); will retry", self.cluster_error)
            return False
        self.suite.seed_from_db(db, catalog.names(), now)
        self.cluster_error = None
        self.bootstrapped = True
        logger.info("runtime bootstrapped against %s cluster", self.cluster.mode)
        return True

    # ------------------------------------------------------------------ tick --
    def tick(self, now: Optional[datetime] = None) -> None:
        with self._tick_lock:
            started = time.perf_counter()
            now = now or utcnow()
            db = db_session.SessionLocal()
            try:
                if not self.bootstrapped and not self.bootstrap(db, now):
                    return
                self._tick(db, now)
            finally:
                db.close()
                self.tick_count += 1
                self.last_tick_at = now
                self.last_tick_ms = round((time.perf_counter() - started) * 1000, 1)

    def _serving_colors(self) -> dict[str, list[str]]:
        out = {}
        for s in catalog.names():
            r = self.routing.route(s)
            out[s] = [r.live_color] + ([r.canary_color] if r.canary_color else [])
        return out

    def _check_serving(self) -> None:
        """Hold synthetic traffic until every service's live colour has Ready
        pods. Checked until true; afterwards pod failures are for the
        detectors to find, not a reason to stop traffic."""
        if self.serving:
            return
        try:
            ready = all(
                self.cluster.rollout_status(s, self.cluster.live_color(s) or "blue").complete for s in catalog.names()
            )
        except Exception as exc:  # noqa: BLE001 — keep waiting
            logger.debug("readiness check failed: %s", exc)
            return
        if ready:
            self.serving = True
            logger.info("all live colours ready; synthetic traffic starting")

    def _tick(self, db: Session, now: datetime) -> None:
        self._check_serving()
        graph = self.graph(db)
        order = self.service_order(db)
        windows = self.recorder.drain()
        try:
            resources = self.telemetry.scrape(self._serving_colors(), epoch(now))
        except Exception as exc:  # noqa: BLE001
            logger.warning("resource scrape failed: %s", exc)
            resources = {}
        chaos_running = self.chaos.running_services(db)

        # --- 3/4: detectors + persistence ---
        results: dict[str, SuiteResult] = {}
        values: dict[str, dict[str, Optional[float]]] = {}
        for s in order:
            w = windows.get(s)
            if w is None or w.total == 0:
                continue
            res = resources.get(s, {})
            values[s] = {
                "error_rate": w.error_rate,
                "latency_p95": w.latency_p95,
                "cpu_usage": res.get("cpu_usage"),
                "memory_usage": res.get("memory_usage"),
            }
            results[s] = self.suite.evaluate(s, values[s], w.errors, w.total, now)
            self.traffic[s] = {
                "requests": w.total,
                "bad": w.bad,
                "errors": w.errors,
                "rps": round(w.total / self.tick_s, 2),
            }
        Telemetry.persist(
            db,
            now,
            self.tick_s,
            windows,
            values,
            {
                s: {"zscores": r.zscores, "residual_z": r.residual_z, "iforest_score": r.iforest_score}
                for s, r in results.items()
            },
        )
        for r in results.values():
            if self.suite.should_record(r):
                db.add(r.to_row())
        db.commit()
        if self.cluster.mode == "simulated":
            for s, w in windows.items():
                self.cluster.offered_rps[s] = w.total / self.tick_s

        # --- 5: burn rates ---
        burns: dict[str, ServiceBurn] = {}
        for s in order:
            burns[s] = self.burn.evaluate(db, s, now)
            self.burn.record_transitions(db, burns[s], now)
        db.commit()

        # --- 6: incidents (roots first): detector consensus, or a FAST_BURN page ---
        for s in order:
            r = results.get(s)
            b = burns[s]
            severity = SEVERITY_FOR_BURN.get(b.worst, "MEDIUM") if b.worst else "MEDIUM"
            acting = r is not None and r.auto_action
            paging = b.worst == "FAST_BURN"
            new_consensus = acting and s not in self._acting
            new_page = paging and s not in self._paging
            (self._acting.add if acting else self._acting.discard)(s)
            (self._paging.add if paging else self._paging.discard)(s)
            if b.worst:
                inc = self.incidents.active(db, s)
                if inc is not None and SEVERITY_ORDER[severity] > SEVERITY_ORDER.get(inc.severity or "LOW", 0):
                    inc.severity = severity
                    db.commit()
            if not (new_consensus or new_page):
                continue
            reasons = []
            if acting:
                flagged = [layer.layer for layer in r.layers if layer.flagged]
                reasons.append(f"{r.agreeing_layers}/3 anomaly layers agree ({', '.join(flagged)})")
            if b.worst:
                ev = next(e for e in b.evaluations if e.severity == b.worst)
                reasons.append(
                    f"{b.worst} {ev.long_window}/{ev.short_window} at {ev.long_burn:.1f}x/{ev.short_burn:.1f}x"
                )
            exp = chaos_running.get(s)
            self.incidents.open(
                db,
                s,
                "; ".join(reasons),
                severity=severity,
                now=now,
                graph=graph,
                context={"consensus": r.summary() if r else None, "burn": b.to_dict()},
                chaos_experiment_id=exp.id if exp else None,
            )

        # --- 7: learning from presumed-normal readings ---
        active_incident = {i.service_name for i in self.incidents.all_active(db)}
        for s, r in results.items():
            normal = not r.auto_action and s not in active_incident and s not in chaos_running
            self.suite.learn(r, normal)

        # --- 8/9: rollouts and post-deploy rollbacks ---
        self.deployments.reconcile(db, now)
        self._post_deploy_rollbacks(db, graph, results, burns, now)

        # --- 10/11: chaos drills, incident resolution ---
        self._update_quiet(db, results, burns, now)
        # A drill's service has recovered when no layer flags it and its edge
        # traffic is clean again; the FAST_BURN alert lags by its 5m window.
        healthy = {
            s: (results.get(s) is None or results[s].agreeing_layers == 0)
            and (s not in windows or windows[s].total == 0 or windows[s].bad / windows[s].total <= 0.01)
            for s in catalog.names()
        }
        self.chaos.watch(
            db,
            now,
            {s: r.agreeing_layers for s, r in results.items()},
            healthy,
            fast_burn=frozenset(s for s, burn in burns.items() if burn.worst == "FAST_BURN"),
        )
        self._resolve_quiet(db, now)

        # --- 12: housekeeping ---
        if self._last_retention is None or now - self._last_retention > timedelta(minutes=10):
            self._retention(db, now)
            self._last_retention = now
        self.results, self.burns, self.values = results, burns, values

    def _post_deploy_rollbacks(
        self,
        db: Session,
        graph: DependencyGraph,
        results: dict[str, SuiteResult],
        burns: dict[str, ServiceBurn],
        now: datetime,
    ) -> None:
        for s, b in burns.items():
            if b.worst != "FAST_BURN":
                continue
            # Only the latest rollout is watched. A newer rollout reuses the
            # standby colour, so the release before it has no warm colour left
            # to flip back to (manual rollback refuses it for the same reason).
            dep = self.deployments.latest_for(db, s)
            if dep is None or not self.deployments.watch_window_open(dep, now):
                continue
            inc = self.incidents.active(db, s)
            root_service = None
            if inc is not None and inc.is_cascading and inc.root_incident_id:
                root = db.get(Incident, inc.root_incident_id)
                root_service = root.service_name if root else None
            fast = next(e for e in b.evaluations if e.severity == "FAST_BURN")
            r = results.get(s)
            decision = self.rollback.evaluate(
                s,
                burn_severity="FAST_BURN",
                burn_rate=fast.long_burn,
                consensus_layers=r.agreeing_layers if r else 0,
                cascade_root=root_service,
            )
            if decision.should_rollback:
                logger.warning("post-deploy rollback of %s %s: %s", s, dep.version, decision.reason)
                self.deployments.rollback_live(db, dep, now, decision)

    def _update_quiet(
        self, db: Session, results: dict[str, SuiteResult], burns: dict[str, ServiceBurn], now: datetime
    ) -> dict[str, bool]:
        in_flight = {
            d.service_name
            for d in db.execute(
                select(Deployment).where(Deployment.status.in_([s.value for s in ACTIVE_STATES]))
            ).scalars()
        }
        quiet = {}
        for s in catalog.names():
            r, b = results.get(s), burns.get(s)
            noisy = (
                (r is not None and r.agreeing_layers > 0)
                or (b is not None and b.worst == "FAST_BURN")
                or s in in_flight
            )
            if noisy:
                self.quiet_since.pop(s, None)
            else:
                self.quiet_since.setdefault(s, now)
            quiet[s] = not noisy
        return quiet

    def _resolve_quiet(self, db: Session, now: datetime) -> None:
        for inc in self.incidents.all_active(db):
            since = self.quiet_since.get(inc.service_name)
            if since is not None and (now - since).total_seconds() >= settings.incident_resolve_after_s:
                self.incidents.resolve(db, inc, now, f"service quiet for {(now - since).total_seconds():.0f}s")
        db.commit()

    def _retention(self, db: Session, now: datetime) -> None:
        """Drop raw readings past retention; downsample SLI counts older than
        two hours into 5-minute buckets so the 30-day error budget stays cheap
        to sum (FAST_BURN's 5m window always reads raw data)."""
        cutoff = now - timedelta(hours=settings.data_retention_hours)
        for model, col in (
            (Metric, Metric.recorded_at),
            (Log, Log.logged_at),
            (AnomalyConsensus, AnomalyConsensus.window_end),
        ):
            db.execute(delete(model).where(col < cutoff))
        compact_before = now - timedelta(hours=2)
        # Only the hour before the cutoff: earlier rows were compacted by a
        # previous pass (this runs every 10 minutes). Rows left raw by a long
        # outage still sum correctly; compaction only saves work.
        raw = [
            r
            for r in db.execute(
                select(SLISample).where(
                    SLISample.window_end >= compact_before - timedelta(hours=1),
                    SLISample.window_end < compact_before,
                )
            ).scalars()
            if r.window_end - r.window_start < timedelta(minutes=5)
        ]
        buckets: dict[tuple, list[SLISample]] = {}
        for r in raw:
            b = int(epoch(r.window_end) // 300)
            buckets.setdefault((r.service_name, r.color, b), []).append(r)
        for (service, color, _bucket), rows in buckets.items():
            if len(rows) < 2:
                continue
            db.add(
                SLISample(
                    service_name=service,
                    color=color,
                    window_start=min(x.window_start for x in rows),
                    window_end=max(x.window_end for x in rows),
                    total_requests=sum(x.total_requests for x in rows),
                    bad_requests=sum(x.bad_requests for x in rows),
                    error_requests=sum(x.error_requests for x in rows),
                )
            )
            for x in rows:
                db.delete(x)
        db.execute(
            delete(SLISample).where(SLISample.window_end < now - timedelta(days=settings.slo_default_window_days + 1))
        )
        db.commit()

    # ------------------------------------------------------ external input --
    def ingest_external(
        self, db: Session, service: str, values: dict[str, Optional[float]], errors: int, total: int, now: datetime
    ) -> SuiteResult:
        """Run one externally supplied reading through the same suite and
        consensus gate the control loop uses (bring-your-own telemetry)."""
        with self._tick_lock:
            result = self.suite.evaluate(service, values, errors, total, now)
            Telemetry.persist(
                db,
                now,
                self.tick_s,
                {},
                {service: values},
                {
                    service: {
                        "zscores": result.zscores,
                        "residual_z": result.residual_z,
                        "iforest_score": result.iforest_score,
                    }
                },
            )
            if self.suite.should_record(result):
                db.add(result.to_row())
            db.commit()
            was_acting = service in self._acting
            if result.auto_action and not was_acting:
                flagged = [layer.layer for layer in result.layers if layer.flagged]
                self.incidents.open(
                    db,
                    service,
                    f"{result.agreeing_layers}/3 anomaly layers agree ({', '.join(flagged)})",
                    severity="HIGH",
                    now=now,
                    graph=self.graph(db),
                    context={"consensus": result.summary()},
                )
            if result.auto_action:
                self._acting.add(service)
            else:
                self._acting.discard(service)
            # Catalog services get this from _update_quiet each tick; the tick
            # then resolves any incident that has been quiet long enough.
            if result.agreeing_layers > 0:
                self.quiet_since.pop(service, None)
            else:
                self.quiet_since.setdefault(service, now)
            active = self.incidents.active(db, service) is not None
            self.suite.learn(result, not result.auto_action and not active)
            self.results[service] = result
            return result

    # ------------------------------------------------------------- status --
    def health_status(self, service: str, recently_rolled_back: bool = False) -> str:
        """One word per service, from the dashboard's status vocabulary:
        rollback > critical > warn > canary > ok."""
        if recently_rolled_back:
            return "rollback"
        r, b = self.results.get(service), self.burns.get(service)
        if (r is not None and r.auto_action) or (b is not None and b.worst == "FAST_BURN"):
            return "critical"
        if (r is not None and r.agreeing_layers > 0) or (b is not None and b.worst is not None):
            return "warn"
        if self.routing.route(service).canary_color:
            return "canary"
        return "ok"

    # ------------------------------------------------------------ lifecycle --
    def wake(self) -> None:
        """Run the next tick now (e.g. right after a deployment is created)."""
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def _run(self) -> None:
        while self.running:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001 — the loop must survive a bad tick
                logger.exception("control loop tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_s)
            except TimeoutError:
                pass
            self._wake.clear()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.running = True
        if self.loadgen is not None:
            self.loadgen.start()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self._wake is not None:
            self._wake.set()
        if self.loadgen is not None:
            await self.loadgen.stop()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        await self.transport.aclose()


_runtime: Optional[Runtime] = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        from app.services import rollback_engine

        _runtime = Runtime(rollback=rollback_engine)
    return _runtime


def set_runtime(rt: Optional[Runtime]) -> None:
    global _runtime
    _runtime = rt
