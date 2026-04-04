-- Job Search Intelligence Platform — Database Schema
-- PostgreSQL 15 (RDS in production, Docker locally)
-- All timestamps are UTC (CLAUDE.md convention)

-- ============================================================================
-- 1. jobs — core job listings from all sources
-- Valid statuses: to_apply | waiting_for_referral | applied | assessment | assignment | interview | offer | rejected
-- ============================================================================
CREATE TABLE IF NOT EXISTS jobs (
    id              BIGSERIAL PRIMARY KEY,
    simplify_id     TEXT UNIQUE,                        -- external ID for dedup
    company         TEXT NOT NULL,
    role            TEXT NOT NULL,
    location        TEXT,
    link            TEXT,
    date_posted     TIMESTAMPTZ,
    source          TEXT NOT NULL DEFAULT 'github',      -- github, email, manual
    status          TEXT NOT NULL DEFAULT 'to_apply',    -- see valid statuses above
    jd_s3_key       TEXT UNIQUE,                         -- S3 key for raw JD text (dedup)
    ats_url         TEXT UNIQUE,                         -- direct ATS application link (dedup)
    match_score     REAL,                               -- best fit score from Resume Matcher
    referral_status TEXT NOT NULL DEFAULT 'none',        -- none, requested, received
    referral_accepts TEXT NOT NULL DEFAULT 'unknown',    -- unknown, yes, no
    follow_up_flagged BOOLEAN NOT NULL DEFAULT FALSE,   -- flagged for follow-up action
    follow_up_snoozed TIMESTAMPTZ,                     -- snoozed until this date
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_json        JSONB,                              -- original source data
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Analysis pipeline tracking
    analysis_status TEXT NOT NULL DEFAULT 'pending',    -- pending | analyzing | completed | failed | skipped
    analysis_error  TEXT,                               -- error message if analysis_status = 'failed'
    analysis_attempted_at TIMESTAMPTZ                   -- last time analysis was attempted
);

CREATE INDEX idx_jobs_status ON jobs (status);
CREATE INDEX idx_jobs_company ON jobs (company);
CREATE INDEX idx_jobs_source ON jobs (source);
CREATE INDEX idx_jobs_date_posted ON jobs (date_posted DESC);
CREATE UNIQUE INDEX idx_jobs_company_role_source ON jobs (company, role, source);
CREATE INDEX idx_jobs_analysis_status ON jobs (analysis_status);

-- ============================================================================
-- 2. labeled_emails — confirmed email classifications (feeds ChromaDB few-shot)
-- ============================================================================
CREATE TABLE IF NOT EXISTS labeled_emails (
    id              BIGSERIAL PRIMARY KEY,
    email_id        TEXT UNIQUE NOT NULL,                -- Gmail message ID
    subject         TEXT,
    snippet         TEXT,
    embedding       BYTEA,                              -- ONNX all-MiniLM-L6-v2 embedding
    stage           TEXT NOT NULL,                       -- irrelevant, status_update, recommendation (stage 1)
                                                        -- or applied, assessment, assignment, interview, offer, rejected (stage 2)
    confirmed_by    TEXT NOT NULL DEFAULT 'user',        -- user or auto
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_labeled_emails_stage ON labeled_emails (stage);

-- ============================================================================
-- 3. labeling_queue — emails awaiting human classification in dashboard
-- ============================================================================
CREATE TABLE IF NOT EXISTS labeling_queue (
    id              BIGSERIAL PRIMARY KEY,
    email_id        TEXT UNIQUE NOT NULL,                -- Gmail message ID
    subject         TEXT,
    snippet         TEXT,
    body            TEXT,                                -- full email body for context
    guessed_stage   TEXT,                               -- agent's best guess
    guessed_company TEXT,                               -- extracted company name
    guessed_role    TEXT,                               -- extracted role title
    match_candidates JSONB,                             -- potential job matches [{job_id, company, role}]
    embedding       BYTEA,                              -- for similarity search
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_labeling_queue_resolved ON labeling_queue (resolved);

-- ============================================================================
-- 4. embedding_cache — avoid re-embedding identical content
-- ============================================================================
CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash    TEXT PRIMARY KEY,                    -- SHA-256 of input text
    embedding       BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 5. config — key-value store for runtime configuration
-- ============================================================================
CREATE TABLE IF NOT EXISTS config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 6. jd_analyses — structured fields extracted from JDs by JD Analyzer
-- ============================================================================
CREATE TABLE IF NOT EXISTS jd_analyses (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    raw_jd_text     TEXT,                               -- original JD before stripping
    jd_source       TEXT,                               -- ATS type: greenhouse, lever, workday, etc.
    required_skills TEXT[],                              -- extracted required skills
    preferred_skills TEXT[],                             -- extracted nice-to-haves
    tech_stack      TEXT[],                              -- specific technologies mentioned
    role_type       TEXT,                                -- backend, frontend, fullstack, ml, devops, etc.
    experience_range INT4RANGE,                          -- e.g. [2,5) years
    deal_breakers   TEXT[],                              -- clearance, sponsorship, etc.
    remote_policy   TEXT,                                -- remote, hybrid, onsite, unknown
    confidence_scores JSONB,                            -- per-field confidence from LLM
    extraction_notes TEXT,                               -- LLM's notes on ambiguities
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_jd_analyses_job_id ON jd_analyses (job_id);

-- ============================================================================
-- 7. resumes — supports multiple resumes with different emphasis
-- ============================================================================
CREATE TABLE IF NOT EXISTS resumes (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,                       -- user-friendly name, e.g. "Backend Focus"
    s3_key          TEXT NOT NULL,                       -- S3 key for PII-redacted resume
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 8. match_reports — per-resume match results from Resume Matcher RAG pipeline
-- ============================================================================
CREATE TABLE IF NOT EXISTS match_reports (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_id       BIGINT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    jd_analysis_id  BIGINT REFERENCES jd_analyses(id) ON DELETE SET NULL,
    overall_fit_score REAL,                             -- 0.0 - 1.0
    fit_category    TEXT,                                -- strong, moderate, weak
    skill_gaps      TEXT[],                              -- skills in JD but not in resume
    reasoning       TEXT,                                -- LLM's explanation of the score
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_match_reports_job_resume ON match_reports (job_id, resume_id);
CREATE INDEX idx_match_reports_fit_score ON match_reports (overall_fit_score DESC);

-- ============================================================================
-- 9. followup_recommendations — populated by Follow-up Advisor daily_check
-- ============================================================================
CREATE TABLE IF NOT EXISTS followup_recommendations (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    urgency_level   TEXT NOT NULL,                       -- high, medium, low
    recommended_action TEXT NOT NULL,                    -- e.g. "send follow-up", "check status"
    urgency_reasoning TEXT,                              -- why this urgency level
    acted_on        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_followup_urgency ON followup_recommendations (urgency_level, acted_on);

-- ============================================================================
-- 10. orchestration_runs — tracks agent chain executions for debugging
-- ============================================================================
CREATE TABLE IF NOT EXISTS orchestration_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID NOT NULL,                       -- LangGraph run ID
    event_type      TEXT NOT NULL,                       -- github_check, email_check, resume_upload, daily_followup
    event_source    TEXT,                                -- what triggered this run
    agent_chain     TEXT[],                              -- ordered list of agents invoked
    agent_results   JSONB,                              -- per-agent results/metadata
    status          TEXT NOT NULL DEFAULT 'running',     -- running, completed, failed
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_orchestration_runs_status ON orchestration_runs (status);
CREATE INDEX idx_orchestration_runs_started ON orchestration_runs (started_at DESC);

-- ============================================================================
-- 11. answer_memory — stores Q&A pairs for RAG retrieval in applications
-- ============================================================================
CREATE TABLE IF NOT EXISTS answer_memory (
    id              BIGSERIAL PRIMARY KEY,
    question_text   TEXT NOT NULL,
    answer_text     TEXT NOT NULL,
    question_type   TEXT,                                  -- behavioral, technical, situational, etc.
    embedding_s3_key TEXT,                                 -- S3 key for Bedrock Titan embedding
    company         TEXT,
    role            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 12. deadlines — tracks application/assessment/interview deadlines
-- ============================================================================
CREATE TABLE IF NOT EXISTS deadlines (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    deadline_text   TEXT,                                  -- original text from email/JD
    deadline_date   DATE NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_deadlines_job_id ON deadlines(job_id);
CREATE INDEX idx_deadlines_date ON deadlines(deadline_date);

-- ============================================================================
-- 13. pipeline_metrics — operational metrics from all pipeline sources
-- ============================================================================
CREATE TABLE IF NOT EXISTS pipeline_metrics (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,                          -- agent name, lambda, api, etc.
    metric_name     TEXT NOT NULL,                          -- e.g. "emails_processed", "jds_analyzed"
    metric_value    NUMERIC,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
