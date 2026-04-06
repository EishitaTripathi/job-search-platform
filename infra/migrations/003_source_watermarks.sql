-- Migration 003: Add source watermarks for incremental adapter fetching.
-- Tracks the latest date_posted per source so adapters only fetch new jobs.

CREATE TABLE IF NOT EXISTS source_watermarks (
    source              TEXT PRIMARY KEY,
    last_fetched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latest_date_posted  DATE,
    jobs_fetched        INT NOT NULL DEFAULT 0
);

-- Backfill from existing jobs
INSERT INTO source_watermarks (source, latest_date_posted, jobs_fetched)
SELECT source, MAX(date_posted)::date, COUNT(*)
FROM jobs
GROUP BY source
ON CONFLICT (source) DO NOTHING;
