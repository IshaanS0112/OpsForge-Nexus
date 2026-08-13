"""Central configuration. All tunable thresholds live here so the engines stay
declarative and the values are auditable and easy to tune per environment."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str = "postgresql://opsforge:opsforge@postgres:5432/opsforge"

    # --- LLM (Claude) ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-5"
    llm_timeout_seconds: float = 20.0

    # --- Deployment / health-check gate ---
    health_check_interval_s: int = 5
    health_check_window_s: int = 60
    health_min_success_rate: float = 0.99
    health_max_p95_latency_ms: float = 500.0
    health_consecutive_failures_to_rollback: int = 3
    # When True, the health gate runs its polls without real sleeps. Prod keeps
    # this False so polling is genuinely time-based; tests/demo set it True.
    health_check_fast: bool = False

    # --- Rollback engine thresholds ---
    rollback_error_rate_threshold: float = 0.05          # 5%
    rollback_error_rate_sustain_s: int = 30
    rollback_latency_multiplier: float = 2.0             # 2x baseline
    rollback_latency_sustain_s: int = 60
    rollback_cooldown_s: int = 300                        # 5 min anti-flapping

    # --- Anomaly detector (z-score) ---
    anomaly_window_size: int = 100                        # N readings
    anomaly_z_threshold: float = 3.0
    anomaly_consecutive_to_incident: int = 3              # K consecutive

    # --- RCA ---
    rca_lookback_minutes: int = 15
    rca_deployment_correlation_minutes: int = 5

    # --- Business impact ---
    revenue_per_request: float = 0.50                     # configurable mock default


settings = Settings()
