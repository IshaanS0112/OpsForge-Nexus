# Methods and standards

Every automated decision in OpsForge traces to a published method or an industry
standard definition. Nothing here is a threshold someone picked because it felt
about right. This table is the map: what the system decides, the method it uses,
where that method comes from, the code, and the test that holds it honest.

## Decisions

| Decision | Method | Source | Implementation | Test |
|---|---|---|---|---|
| Is this canary stage safe to promote? | Sequential probability ratio test, α = β = 0.05, H₀ = burning at budget, H₁ = burning at 14.4× | Wald, *Sequential Tests of Statistical Hypotheses* (1945) | [`burn_rate_engine.evaluate_canary_gate`](../backend/app/services/burn_rate_engine.py) | false-fail rate under continuous monitoring, `test_burn_rate_engine.py` |
| Is the service burning its error budget? | Multiwindow, multi-burn-rate alerting: 14.4× (1 h/5 m), 6× (6 h/30 m), 1× (3 d/6 h) | Google, *The Site Reliability Workbook*, ch. 5 "Alerting on SLOs" | [`burn_rate_engine.POLICY`](../backend/app/services/burn_rate_engine.py) | 52-second detection of a total outage, `test_burn_rate_engine.py` |
| Is this reading anomalous, on its own? | Rolling z-score, \|z\| > 3, K = 3 consecutive, per-metric variance floor | Shewhart control-chart practice; 3σ convention | [`anomaly_detector.ZScoreLayer`](../backend/app/services/anomaly_detector.py) | flags a jump off a flat baseline, `test_anomaly_suite.py` |
| Is it anomalous *given the time of day*? | STL decomposition, z-score of the residual | Cleveland, Cleveland, McRae & Terpenning, "STL: A Seasonal-Trend Decomposition Procedure Based on Loess", *J. Official Statistics* (1990) | [`seasonal_detector`](../backend/app/services/seasonal_detector.py) | ignores the recurring batch burst that z-score flags daily, `test_anomaly_suite.py` |
| Robust spread estimate for that residual | Median absolute deviation, scaled by 1.4826 | Rousseeuw & Croux, "Alternatives to the Median Absolute Deviation", *JASA* (1993) | `seasonal_detector.robust_scale` | `test_anomaly_suite.py` |
| Are several metrics drifting together? | Isolation Forest, unsupervised, threshold at the 0.5% quantile of training scores | Liu, Ting & Zhou, "Isolation Forest", *ICDM* (2008) | [`isolation_forest_detector`](../backend/app/services/isolation_forest_detector.py) | catches joint drift no single z-score sees, `test_anomaly_suite.py` |
| Should any of that trigger action? | Consensus: ≥ 2 of 3 independent layers must agree | Ensemble practice — each layer has a different blind spot | [`anomaly_suite`](../backend/app/services/anomaly_suite.py) | one layer never acts, `test_anomaly_suite.py` |
| Which service is the root cause? | Breadth-first blast radius; Kahn topological order over the dependency DAG | Kahn, "Topological sorting of large networks", *CACM* (1962) | [`dependency_graph`](../backend/app/services/dependency_graph.py) | root vs cascade attribution, `test_dependency_graph.py` |
| Should we roll back a live release? | FAST_BURN **and** detector consensus **and** not a cascade victim, with an anti-flapping cooldown | SRE error-budget policy; progressive delivery practice | [`rollback_engine`](../backend/app/services/rollback_engine.py) | 10 scenarios, `test_rollback_engine.py` |
| Does the safety net actually work? | Fault injection with measured detection, rollback and recovery latency against stated bounds | *Principles of Chaos Engineering* (Basiri et al., Netflix, 2016) | [`chaos_engine`](../backend/app/services/chaos_engine.py) | recorded drills in [`docs/drills/`](drills/) |
| What did the incident cost? | Requests that failed beyond the baseline × revenue per request, every input stored | Standard incident-cost framing; inputs auditable rather than modelled | [`business_impact`](../backend/app/services/business_impact.py) | `test_business_impact.py` |

## Delivery metrics

The delivery section of `/analytics/summary` uses DORA's definitions
(Forsgren, Humble & Kim, *Accelerate*, 2018; DORA *State of DevOps* reports).
Where our measurement differs from the standard definition, the difference is
stated rather than glossed over.

| DORA metric | Standard definition | What OpsForge measures | Same? |
|---|---|---|---|
| Deployment frequency | How often an organisation successfully releases to production | Rollouts reaching `LIVE` per day in the window | Yes |
| Change failure rate | Share of deployments causing a failure in production | Rollouts ending `ROLLED_BACK` ÷ all rollouts that finished | Yes, with the system's own rollback as the failure signal |
| Mean time to restore | How long to recover from a failure in production | Incident `detected_at` → `resolved_at`, resolution being a service quiet for 60 s | Yes |
| Lead time for changes | Commit to running in production | **Not measured** | No — it needs version-control linkage this system does not have, so it is absent rather than approximated |

Two more that DORA does not define, reported because this system can measure
them honestly:

- **Time to detect** — fault injection to the first of the system's own
  detection mechanisms firing. Only drills report it, because only a drill knows
  when the fault actually began. Measuring it from an incident's own detection
  time would be circular.
- **Error budget consumed** — bad requests in the window against the share of
  the 30-day budget that window represents.

## Reading the numbers

- A figure the window cannot support is `null`, never `0`. "Nothing shipped" and
  "nothing measured" are different answers.
- Analytics are derived per request from the stored rows, never cached as running
  totals, so any figure can be traced to the deployments, incidents, request
  counts and drills behind it.
- The anomaly model is unsupervised and makes no accuracy claim: there are no
  labelled incidents to validate against. What the tests assert is behavioural —
  that each layer catches the failure mode it exists for, and stays quiet on the
  one it is known to confuse.

Full design reasoning: [`architecture.md`](architecture.md).
