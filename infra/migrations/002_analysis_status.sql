-- Migration 002: Add analysis pipeline tracking to jobs table
-- Replaces the LEFT JOIN jd_analyses pattern with explicit status tracking.
-- Values: pending | analyzing | completed | failed | skipped

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS analysis_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS analysis_error TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS analysis_attempted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_jobs_analysis_status ON jobs (analysis_status);

-- Backfill: jobs that already have jd_analyses should be 'completed'
UPDATE jobs SET analysis_status = 'completed'
WHERE id IN (SELECT job_id FROM jd_analyses)
  AND analysis_status = 'pending';
