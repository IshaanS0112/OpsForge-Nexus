-- OpsForge Nexus: PostgreSQL schema, for reading.
--
-- The source of truth is the Alembic migrations in backend/migrations
-- (0001 = V1 baseline, 0002 = V3, 0003 = durable rollback cooldown); the
-- backend applies them on startup.
-- This file is `pg_dump --schema-only` of a fresh database after
-- `alembic upgrade head`. A V1 database upgraded in place ends up with the
-- same schema.
--
-- Regenerate:
--   createdb opsforge_schema && DATABASE_URL=postgresql://.../opsforge_schema alembic upgrade head
--   pg_dump --schema-only --no-owner --no-privileges opsforge_schema

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);

CREATE TABLE public.anomaly_consensus (
    id bigint NOT NULL,
    service_name character varying(100) NOT NULL,
    window_end timestamp without time zone NOT NULL,
    zscore_flagged boolean,
    seasonal_flagged boolean,
    isolation_forest_flagged boolean,
    agreeing_layers integer,
    auto_action_taken boolean,
    details jsonb
);

CREATE SEQUENCE public.anomaly_consensus_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.anomaly_consensus_id_seq OWNED BY public.anomaly_consensus.id;

CREATE TABLE public.burn_rate_alerts (
    id bigint NOT NULL,
    service_name character varying(100) NOT NULL,
    severity character varying(20) NOT NULL,
    long_window character varying(10) NOT NULL,
    short_window character varying(10) NOT NULL,
    burn_rate double precision NOT NULL,
    short_burn_rate double precision,
    fired_at timestamp without time zone,
    resolved_at timestamp without time zone
);

CREATE SEQUENCE public.burn_rate_alerts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.burn_rate_alerts_id_seq OWNED BY public.burn_rate_alerts.id;

CREATE TABLE public.business_impact (
    id uuid NOT NULL,
    incident_id uuid NOT NULL,
    affected_requests integer,
    error_rate_delta double precision,
    estimated_impact_value double precision,
    calculation_basis jsonb,
    calculated_at timestamp without time zone
);

CREATE TABLE public.chaos_experiments (
    id uuid NOT NULL,
    service_name character varying(100) NOT NULL,
    fault_type character varying(50) NOT NULL,
    injected_at timestamp without time zone,
    detection_latency_seconds double precision,
    rollback_latency_seconds double precision,
    passed boolean,
    notes text,
    magnitude double precision,
    target character varying(10) DEFAULT 'all'::character varying NOT NULL,
    duration_s integer,
    status character varying(20) DEFAULT 'RUNNING'::character varying NOT NULL,
    deployment_id uuid,
    recovery_latency_seconds double precision,
    detection_bound_s double precision,
    rollback_bound_s double precision,
    affected_pods jsonb,
    timeline jsonb,
    completed_at timestamp without time zone
);

CREATE TABLE public.deployments (
    id uuid NOT NULL,
    service_name character varying(100) NOT NULL,
    version character varying(50) NOT NULL,
    status character varying(30) NOT NULL,
    config_diff jsonb,
    previous_version_id uuid,
    deployed_at timestamp without time zone,
    switched_at timestamp without time zone,
    k8s_namespace character varying(100),
    k8s_deployment_name character varying(150),
    image character varying(200),
    strategy character varying(20) DEFAULT 'BLUE_GREEN'::character varying NOT NULL,
    target_color character varying(10),
    stable_color character varying(10),
    auto_promote boolean DEFAULT true NOT NULL,
    stage_started_at timestamp without time zone,
    status_reason text,
    stage_history jsonb,
    finished_at timestamp without time zone
);

CREATE TABLE public.incidents (
    id uuid NOT NULL,
    service_name character varying(100) NOT NULL,
    detected_at timestamp without time zone,
    resolved_at timestamp without time zone,
    status character varying(30) NOT NULL,
    severity character varying(20),
    trigger_reason text NOT NULL,
    related_deployment_id uuid,
    is_cascading boolean DEFAULT false,
    root_incident_id uuid,
    blast_radius_services jsonb,
    detection_context jsonb,
    chaos_experiment_id uuid
);

CREATE TABLE public.logs (
    id bigint NOT NULL,
    service_name character varying(100) NOT NULL,
    level character varying(10) NOT NULL,
    message text NOT NULL,
    error_signature character varying(200),
    incident_id uuid,
    logged_at timestamp without time zone,
    occurrences integer DEFAULT 1 NOT NULL,
    root_signature character varying(200)
);

CREATE SEQUENCE public.logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.logs_id_seq OWNED BY public.logs.id;

CREATE TABLE public.metrics (
    id bigint NOT NULL,
    service_name character varying(100) NOT NULL,
    metric_name character varying(50) NOT NULL,
    value double precision NOT NULL,
    z_score double precision,
    recorded_at timestamp without time zone,
    seasonal_residual_z double precision,
    isolation_forest_score double precision
);

CREATE SEQUENCE public.metrics_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.metrics_id_seq OWNED BY public.metrics.id;

CREATE TABLE public.rca_reports (
    id uuid NOT NULL,
    incident_id uuid NOT NULL,
    root_cause_candidates jsonb NOT NULL,
    llm_model_used character varying(50),
    generated_at timestamp without time zone,
    signals jsonb
);

CREATE TABLE public.rollback_cooldowns (
    service_name character varying(100) NOT NULL,
    started_at timestamp without time zone NOT NULL,
    until_at timestamp without time zone NOT NULL,
    trigger character varying(40),
    deployment_id uuid
);

CREATE TABLE public.service_dependencies (
    id uuid NOT NULL,
    service_name character varying(100) NOT NULL,
    depends_on_service character varying(100) NOT NULL
);

CREATE TABLE public.sli_samples (
    id bigint NOT NULL,
    service_name character varying(100) NOT NULL,
    color character varying(10),
    window_start timestamp without time zone NOT NULL,
    window_end timestamp without time zone NOT NULL,
    total_requests integer DEFAULT 0 NOT NULL,
    bad_requests integer DEFAULT 0 NOT NULL,
    error_requests integer DEFAULT 0 NOT NULL
);

CREATE SEQUENCE public.sli_samples_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sli_samples_id_seq OWNED BY public.sli_samples.id;

CREATE TABLE public.slo_config (
    service_name character varying(100) NOT NULL,
    slo_target double precision DEFAULT '0.999'::double precision NOT NULL,
    window_days integer DEFAULT 30 NOT NULL,
    latency_threshold_ms double precision DEFAULT '300'::double precision NOT NULL
);

ALTER TABLE ONLY public.anomaly_consensus ALTER COLUMN id SET DEFAULT nextval('public.anomaly_consensus_id_seq'::regclass);

ALTER TABLE ONLY public.burn_rate_alerts ALTER COLUMN id SET DEFAULT nextval('public.burn_rate_alerts_id_seq'::regclass);

ALTER TABLE ONLY public.logs ALTER COLUMN id SET DEFAULT nextval('public.logs_id_seq'::regclass);

ALTER TABLE ONLY public.metrics ALTER COLUMN id SET DEFAULT nextval('public.metrics_id_seq'::regclass);

ALTER TABLE ONLY public.sli_samples ALTER COLUMN id SET DEFAULT nextval('public.sli_samples_id_seq'::regclass);

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY public.anomaly_consensus
    ADD CONSTRAINT anomaly_consensus_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.burn_rate_alerts
    ADD CONSTRAINT burn_rate_alerts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.business_impact
    ADD CONSTRAINT business_impact_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.chaos_experiments
    ADD CONSTRAINT chaos_experiments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT incidents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.logs
    ADD CONSTRAINT logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.metrics
    ADD CONSTRAINT metrics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.rca_reports
    ADD CONSTRAINT rca_reports_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.rollback_cooldowns
    ADD CONSTRAINT rollback_cooldowns_pkey PRIMARY KEY (service_name);

ALTER TABLE ONLY public.service_dependencies
    ADD CONSTRAINT service_dependencies_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.service_dependencies
    ADD CONSTRAINT service_dependencies_service_name_depends_on_service_key UNIQUE (service_name, depends_on_service);

ALTER TABLE ONLY public.sli_samples
    ADD CONSTRAINT sli_samples_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.slo_config
    ADD CONSTRAINT slo_config_pkey PRIMARY KEY (service_name);

CREATE INDEX idx_consensus_service_time ON public.anomaly_consensus USING btree (service_name, window_end);

CREATE INDEX idx_metrics_service_time ON public.metrics USING btree (service_name, recorded_at);

CREATE INDEX idx_sli_service_time ON public.sli_samples USING btree (service_name, window_end);

CREATE INDEX ix_metrics_recorded_at ON public.metrics USING btree (recorded_at);

ALTER TABLE ONLY public.business_impact
    ADD CONSTRAINT business_impact_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);

ALTER TABLE ONLY public.chaos_experiments
    ADD CONSTRAINT chaos_experiments_deployment_id_fkey FOREIGN KEY (deployment_id) REFERENCES public.deployments(id);

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_previous_version_id_fkey FOREIGN KEY (previous_version_id) REFERENCES public.deployments(id);

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT fk_incidents_chaos_experiment_id FOREIGN KEY (chaos_experiment_id) REFERENCES public.chaos_experiments(id);

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT fk_incidents_root_incident_id FOREIGN KEY (root_incident_id) REFERENCES public.incidents(id);

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT incidents_related_deployment_id_fkey FOREIGN KEY (related_deployment_id) REFERENCES public.deployments(id);

ALTER TABLE ONLY public.logs
    ADD CONSTRAINT logs_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);

ALTER TABLE ONLY public.rca_reports
    ADD CONSTRAINT rca_reports_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);

ALTER TABLE ONLY public.rollback_cooldowns
    ADD CONSTRAINT rollback_cooldowns_deployment_id_fkey FOREIGN KEY (deployment_id) REFERENCES public.deployments(id);
