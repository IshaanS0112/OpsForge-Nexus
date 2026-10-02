"""Central configuration. All tunable thresholds live here so the engines stay
declarative and the values are auditable and easy to tune per environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str = "postgresql://opsforge:opsforge@postgres:5432/opsforge"

    # --- LLM (Claude) ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5"
    # The model thinks before answering, so allow more than a bare completion.
    llm_timeout_seconds: float = 60.0
    llm_effort: str = "high"  # low | medium | high | xhigh | max

    # --- Cluster ---
    # "kind": real API calls against the local cluster. "simulated": in-memory
    # stand-in with the same interface. The dashboard labels which is active.
    cluster_mode: str = "simulated"
    kubeconfig: str = ""  # empty = default kubeconfig resolution
    k8s_context: str = ""
    k8s_namespace: str = "opsforge"
    # Host the gateway uses to reach NodePorts: the kind node container on the
    # compose network, or localhost via the kind config's port mappings.
    cluster_node_host: str = "localhost"
    demo_service_image: str = "python:3.12-alpine"
    demo_replicas: int = 1
    demo_startup_delay_s: float = 15.0  # readiness warm-up per pod
    demo_workers: int = 4
    demo_cpu_work_iters: int = 300
    rollout_timeout_s: int = 180  # ReplicaSet readiness deadline for a new colour
    rollout_fast_fail_s: int = 20  # a fatal pod state (ImagePullBackOff...) held this long fails early

    # --- Control loop / gateway / synthetic load ---
    control_loop_enabled: bool = True
    control_loop_tick_s: float = 5.0
    loadgen_enabled: bool = True
    loadgen_rps_per_service: float = 10.0
    # Traffic follows a sinusoidal "day"; the demo compresses one day into
    # this many seconds so the seasonal detector can learn a cycle quickly.
    loadgen_cycle_period_s: float = 600.0
    loadgen_cycle_amplitude: float = 0.5
    # A "scheduled batch job": the same slice of every cycle carries extra
    # load. Plain z-score flags it every time; the seasonal layer learns it.
    loadgen_burst_start: float = 0.20  # phase of the cycle, 0..1
    loadgen_burst_share: float = 0.05  # share of the cycle the burst lasts
    loadgen_burst_multiplier: float = 2.5
    gateway_timeout_s: float = 3.0
    gateway_max_inflight: int = 400

    # --- Deployment / health-check gate (blue-green strategy) ---
    health_check_interval_s: int = 5
    health_check_window_s: int = 60
    health_min_success_rate: float = 0.99
    health_max_p95_latency_ms: float = 500.0
    health_consecutive_failures_to_rollback: int = 3
    # When True, the health gate runs its polls without real sleeps. Prod keeps
    # this False so polling is genuinely time-based; tests/demo set it True.
    health_check_fast: bool = False
    health_probe_requests: int = 10  # smoke requests per health poll against the new colour
    # A canary's quality is judged by its burn-rate gates under real traffic;
    # before any user traffic it only has to prove it is serving at all.
    canary_smoke_requests: int = 20
    canary_smoke_min_success_rate: float = 0.7

    # --- Canary rollout ---
    canary_stages: tuple[int, ...] = (5, 25, 50, 100)
    canary_stage_window_s: float = 45.0  # confirmation window per stage
    canary_min_requests: int = 20  # canary requests needed before a stage can pass
    canary_max_stage_wait_s: float = 300.0  # insufficient traffic for this long -> fail
    canary_max_burn_rate: float = 14.4  # a canary burning faster than a FAST_BURN page fails
    canary_sprt_alpha: float = 0.05  # max chance of failing a canary that only burns at budget
    canary_sprt_beta: float = 0.05

    # --- Rollback engine ---
    rollback_cooldown_s: int = 300  # 5 min anti-flapping
    post_deploy_watch_s: int = 900  # a LIVE release stays rollback-eligible this long

    # --- SLO / burn rate (Google SRE workbook ch.5 multi-window, multi-burn-rate) ---
    slo_default_target: float = 0.999
    slo_default_window_days: int = 30
    slo_default_latency_ms: float = 300.0
    burn_min_requests: int = 20  # short window needs this many requests to fire

    # --- Anomaly detection suite ---
    anomaly_window_size: int = 100  # z-score: N readings
    anomaly_z_threshold: float = 3.0
    anomaly_consecutive_to_incident: int = 3  # K consecutive (applied to every layer)
    seasonal_period_s: float = 600.0  # one "day"; production would use 86400
    seasonal_bucket_s: float = 10.0  # STL runs over bucket means
    seasonal_min_periods: int = 2
    seasonal_refit_every_s: float = 30.0
    iforest_train_window: int = 720  # presumed-normal vectors kept per service
    iforest_min_train: int = 60
    iforest_retrain_every: int = 60  # ticks
    iforest_contamination: float = 0.005  # threshold = this quantile of training scores
    iforest_trees: int = 100
    consensus_min_layers: int = 2

    # --- Incidents / cascade ---
    cascade_window_min: float = 5.0
    # How far a dependent's incident may lead its upstream root: an effect
    # cannot precede its cause by more than detection itself takes.
    cascade_skew_s: float = 90.0
    incident_resolve_after_s: float = 60.0  # quiet this long -> auto-resolve
    data_retention_hours: float = 96.0

    # --- Chaos drills ---
    chaos_detection_bound_s: float = 90.0
    chaos_rollback_bound_s: float = 90.0
    chaos_default_duration_s: int = 60

    # --- RCA ---
    rca_lookback_minutes: int = 15
    rca_deployment_correlation_minutes: int = 5

    # --- Business impact ---
    revenue_per_request: float = 0.50  # configurable mock default

    # --- n8n ---
    n8n_webhook_url: str = ""  # e.g. http://n8n:5678/webhook/opsforge-incident


settings = Settings()
