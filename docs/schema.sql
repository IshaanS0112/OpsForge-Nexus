-- Canonical PostgreSQL DDL (reference).
-- The application creates these tables via SQLAlchemy on startup; this file
-- documents the native-Postgres shape (UUID + JSONB) the ORM maps to.

CREATE TABLE deployments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name VARCHAR(100) NOT NULL,
    version VARCHAR(50) NOT NULL,
    status VARCHAR(30) NOT NULL,  -- IDLE, PREPARING_GREEN, DEPLOYING_GREEN, HEALTH_CHECKING, TRAFFIC_SWITCHING, LIVE, ROLLING_BACK, ROLLED_BACK
    config_diff JSONB,
    previous_version_id UUID REFERENCES deployments(id),
    deployed_at TIMESTAMP DEFAULT now(),
    switched_at TIMESTAMP
);

CREATE TABLE incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name VARCHAR(100) NOT NULL,
    detected_at TIMESTAMP DEFAULT now(),
    resolved_at TIMESTAMP,
    status VARCHAR(30) NOT NULL,   -- OPEN, INVESTIGATING, RESOLVED
    severity VARCHAR(20),          -- LOW, MEDIUM, HIGH, CRITICAL
    trigger_reason TEXT NOT NULL,
    related_deployment_id UUID REFERENCES deployments(id)
);

CREATE TABLE metrics (
    id BIGSERIAL PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL,
    metric_name VARCHAR(50) NOT NULL,  -- error_rate, latency_p95, cpu_usage, memory_usage
    value FLOAT NOT NULL,
    z_score FLOAT,
    recorded_at TIMESTAMP DEFAULT now()
);
CREATE INDEX idx_metrics_service_time ON metrics(service_name, recorded_at);

CREATE TABLE logs (
    id BIGSERIAL PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL,
    level VARCHAR(10) NOT NULL,    -- INFO, WARN, ERROR
    message TEXT NOT NULL,
    error_signature VARCHAR(200),  -- deduplication key
    incident_id UUID REFERENCES incidents(id),
    logged_at TIMESTAMP DEFAULT now()
);

CREATE TABLE rca_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID REFERENCES incidents(id) NOT NULL,
    root_cause_candidates JSONB NOT NULL,  -- [{cause, confidence, evidence}]
    llm_model_used VARCHAR(50),
    generated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE business_impact (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID REFERENCES incidents(id) NOT NULL,
    affected_requests INT,
    error_rate_delta FLOAT,
    estimated_impact_value FLOAT,
    calculation_basis JSONB,
    calculated_at TIMESTAMP DEFAULT now()
);
