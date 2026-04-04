-- Migration 001: Redesign for cloud dashboard + answer memory + deadlines + metrics
-- Idempotent — safe to re-run

-- ============================================================================
-- ALTER existing tables
-- ============================================================================

-- jobs: ATS URL for direct application links (unique to dedup across sources)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ats_url TEXT UNIQUE;

-- jobs: referral tracking columns
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS referral_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS referral_accepts TEXT NOT NULL DEFAULT 'unknown';

-- jd_analyses: remote_policy moved out (was redundant with location-based inference)
ALTER TABLE jd_analyses DROP COLUMN IF EXISTS remote_policy;

-- jobs: remove legacy follow-up columns (snooze feature dropped per PRD)
ALTER TABLE jobs DROP COLUMN IF EXISTS follow_up_snoozed;
ALTER TABLE jobs DROP COLUMN IF EXISTS follow_up_flagged;

-- ============================================================================
-- New table: answer_memory — stores Q&A pairs for RAG retrieval in applications
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
-- New table: deadlines — tracks application/assessment/interview deadlines
-- ============================================================================
CREATE TABLE IF NOT EXISTS deadlines (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    deadline_text   TEXT,                                  -- original text from email/JD
    deadline_date   DATE NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_deadlines_job_id ON deadlines(job_id);
CREATE INDEX IF NOT EXISTS idx_deadlines_date ON deadlines(deadline_date);

-- ============================================================================
-- New table: pipeline_metrics — operational metrics from all pipeline sources
-- ============================================================================
CREATE TABLE IF NOT EXISTS pipeline_metrics (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,                          -- agent name, lambda, api, etc.
    metric_name     TEXT NOT NULL,                          -- e.g. "emails_processed", "jds_analyzed"
    metric_value    NUMERIC,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
