# Schema Type Map — PostgreSQL <-> Python <-> asyncpg

**Last updated: 2026-04-02**
**Source of truth for schema: `infra/schema.sql`**

> This documents every non-obvious type mapping between PostgreSQL columns and Python code.
> The rules below are derived from 9 real bugs fixed in WORK_REPORT_2026-03-30.md.
> Tests in `tests/test_sql_schema_sync.py` enforce these rules.

---

## Rules (non-negotiable)

1. **TEXT[] columns -> Python `list[str]`.** NEVER use `json.dumps()`. asyncpg accepts Python lists directly.
2. **JSONB columns -> Python `dict`.** asyncpg serializes automatically. Pass dict directly.
3. **INT4RANGE columns -> `asyncpg.Range(lower, upper)`.** NEVER format as string `"[0,5)"`.
4. **BYTEA columns -> Python `bytes`.** Pass bytes directly.
5. **TIMESTAMPTZ columns -> `datetime` with timezone info.** asyncpg handles conversion.
6. **TEXT[] concatenation in SQL:** Use `array_cat(COALESCE(col, '{}'::text[]), $N::text[])`. NEVER use `|| $N::jsonb`.

---

## Complete Type Mapping Table

| Table.Column | PG Type | asyncpg Python Type | BUG: Wrong Pattern | FIX: Right Pattern | Bug Reference |
|---|---|---|---|---|---|
| jd_analyses.required_skills | TEXT[] | list[str] | `json.dumps(skills)` | `skills or []` | WORK_REPORT #5 |
| jd_analyses.preferred_skills | TEXT[] | list[str] | `json.dumps(skills)` | `skills or []` | WORK_REPORT #5 |
| jd_analyses.tech_stack | TEXT[] | list[str] | `json.dumps(stack)` | `stack or []` | WORK_REPORT #5 |
| jd_analyses.deal_breakers | TEXT[] | list[str] | `\|\| $2::jsonb` (JSONB concat) | `array_cat(COALESCE(deal_breakers, '{}'::text[]), $2::text[])` | WORK_REPORT #7 |
| jd_analyses.experience_range | INT4RANGE | asyncpg.Range | `"[0,5)"` (string format) | `asyncpg.Range(0, 5)` | WORK_REPORT #5 |
| jd_analyses.confidence_scores | JSONB | dict | (no known bug) | Pass dict directly | — |
| match_reports.skill_gaps | TEXT[] | list[str] | `json.dumps(gaps)` | `gaps or []` | WORK_REPORT #9 |
| match_reports.overall_fit_score | REAL | float | (no known bug) | Pass float directly | — |
| orchestration_runs.agent_chain | TEXT[] | list[str] | (watch for json.dumps) | Pass list directly | — |
| orchestration_runs.agent_results | JSONB | dict | (no known bug) | Pass dict directly | — |
| jobs.raw_json | JSONB | dict | (no known bug) | Pass dict directly | — |
| labeling_queue.match_candidates | JSONB | dict/list | (no known bug) | Pass directly | — |
| labeled_emails.embedding | BYTEA | bytes | (no known bug) | Pass bytes directly | — |
| labeling_queue.embedding | BYTEA | bytes | (no known bug) | Pass bytes directly | — |
| embedding_cache.embedding | BYTEA | bytes | (no known bug) | Pass bytes directly | — |

---

## All TEXT[] Columns in Schema (9 total)

These columns MUST receive Python `list[str]`, never `json.dumps()`:

| Table | Column | Writers (INSERT/UPDATE locations) |
|---|---|---|
| jd_analyses | required_skills | api/agents/jd_analyzer/tools.py |
| jd_analyses | preferred_skills | api/agents/jd_analyzer/tools.py |
| jd_analyses | tech_stack | api/agents/jd_analyzer/tools.py |
| jd_analyses | deal_breakers | api/agents/jd_analyzer/tools.py, api/agents/sponsorship_screener/tools.py |
| match_reports | skill_gaps | api/agents/resume_matcher/tools.py |
| orchestration_runs | agent_chain | api/agents/cloud_coordinator/tools.py |
| answer_memory | (none — no TEXT[] columns) | — |

## All INT4RANGE Columns (1 total)

| Table | Column | Writers |
|---|---|---|
| jd_analyses | experience_range | api/agents/jd_analyzer/tools.py |

Must use: `asyncpg.Range(lower_int, upper_int)`
Never use: `f"[{lower},{upper})"` or string formatting

## All JSONB Columns (5 total)

| Table | Column | Writers |
|---|---|---|
| jobs | raw_json | lambda/persist/handler.py |
| jd_analyses | confidence_scores | api/agents/jd_analyzer/tools.py |
| orchestration_runs | agent_results | api/agents/cloud_coordinator/tools.py |
| labeling_queue | match_candidates | local/agents/email_classifier/tools.py |

Pass Python dict directly. asyncpg serializes to JSONB automatically.

## All BYTEA Columns (3 total)

| Table | Column | Writers |
|---|---|---|
| labeled_emails | embedding | local/agents/email_classifier/tools.py |
| labeling_queue | embedding | local/agents/email_classifier/tools.py |
| embedding_cache | embedding | local/agents/shared/embedder.py |

Pass Python bytes directly.

---

## psycopg2 (Lambda) vs asyncpg (ECS/local) Differences

| Feature | asyncpg (ECS, local) | psycopg2 (Lambda) |
|---|---|---|
| Parameter syntax | `$1, $2, $3` | `%s, %s, %s` |
| TEXT[] | Python list directly | Python list directly |
| JSONB | Python dict directly | `json.dumps(dict)` required (psycopg2 auto-adapts with register_adapter, but explicit is safer) |
| INT4RANGE | `asyncpg.Range(lo, hi)` | `psycopg2.extras.NumericRange(lo, hi)` |
| Connection pool | `asyncpg.create_pool()` | `psycopg2.connect()` per invocation |

Lambda Persist (`lambda/persist/handler.py`) uses psycopg2.
All other services use asyncpg.
