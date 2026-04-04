# REQUIREMENTS.md -- Source of Truth
## Job Search Intelligence Platform
**Last compiled: 2026-04-03**
**Source documents: CONTEXT.md, ARCHITECTURE.md, README.md, CLAUDE.md, WORK_REPORT_2026-03-30.md, infra/schema.sql, local/pipeline/schemas.py**

> This is THE authoritative document for all project requirements. When code conflicts with this document, this document defines intent; the code needs to change.

---

## Section 1: Functional Requirements

### FR-1.1: Job Discovery & Ingestion

#### FR-1.1.1: Multi-Source Fetching via Pluggable Adapter Pattern

**Definition:** The system fetches job listings from multiple external sources using a pluggable `SourceAdapter` base class. A registry maps source names to adapter classes at runtime. Adapters run inside the JD Ingestion Agent on ECS (replacing the former Lambda Fetch). New sources require only a new adapter class and a registry entry.

**Acceptance Criteria:**
- 8 active adapters are registered in `adapter_registry.py` (6 in-code, 2 commented-out requiring API keys)
- 2 blacklisted adapters (RemoteOK, JSearch) are excluded from the registry with documented TOS reasons
- Each adapter implements `SourceAdapter.fetch(params) -> list[NormalizedJob]`
- Adapters that require API keys gracefully return `[]` when keys are missing
- All adapters normalize output to the `NormalizedJob` dataclass

**Active Adapters:**

| Adapter | Tier | Schedule | API Type |
|---------|------|----------|----------|
| `the_muse` | 1 (daily) | EventBridge `cron(0 6 * * ? *)` | Official public API, no key |
| `simplify` | 2 (daily) | EventBridge `cron(0 6 * * ? *)` | Published GitHub JSON feed |
| `hn_hiring` | 3 (monthly) | EventBridge `cron(0 9 1 * ? *)` | Algolia HN Search API |
| `greenhouse` | 4 (on-demand) | SQS manual trigger | Official public ATS board API |
| `lever` | 4 (on-demand) | SQS manual trigger | Official public ATS board API |
| `ashby` | 4 (on-demand) | SQS manual trigger | Official public ATS board API |
| `adzuna` | -- (disabled) | Commented out | Official API, requires `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` |
| `usajobs` | -- (disabled) | Commented out | US Government API, requires `USAJOBS_API_KEY` + `USAJOBS_EMAIL` |

**Blacklisted (DO NOT re-enable without TOS review):**
- `remoteok`: Actively blocks automated access (HTTP 403). No documented API TOS.
- `jsearch`: Third-party RapidAPI wrapper around Google Jobs. Google does not officially license this aggregation. Extremely limited free tier (100/mo).

**Email-sourced (Tier 5):** LinkedIn, Handshake, Lensa, Jobright -- via email recommendation pipeline, not direct API.

**NormalizedJob Schema** (from `api/agents/jd_ingestion/adapters/base.py`):
```
company: str
role: str
location: str
ats_url: str
date_posted: Optional[str]  # YYYY-MM-DD or None
source: str
source_id: str
raw_json: dict
```

**Implementation Status:** Working
**File Locations:** `api/agents/jd_ingestion/adapter_registry.py`, `api/agents/jd_ingestion/adapters/base.py`, `api/agents/jd_ingestion/adapters/*.py`, `infra/eventbridge.tf`
**Test Coverage:** `tests/test_source_adapters.py`, `tests/test_lambda_fetch.py`

---

#### FR-1.1.2: Three-Layer Content Deduplication

**Definition:** The system prevents duplicate job data at three layers: content-addressable S3 storage, S3 HeadObject pre-check, and database UNIQUE constraints.

**Acceptance Criteria:**
- Layer 1 (Content Hash): `SHA-256(job_content)` produces S3 key `jds/{hash}.json` (adapter mode) or `jds/{hash}.txt` (URL mode). Identical content always maps to the same key.
- Layer 2 (S3 HeadObject): Before writing, `s3.head_object()` checks if the key already exists. If yes, the write is skipped entirely.
- Layer 3 (Database UNIQUE): Four UNIQUE constraints enforce dedup at the database level:
  - `jd_s3_key` (TEXT UNIQUE) -- prevents same S3 object from creating duplicate rows
  - `ats_url` (TEXT UNIQUE) -- prevents same ATS application link from creating duplicates
  - `simplify_id` (TEXT UNIQUE) -- prevents same Simplify external ID from creating duplicates
  - `(company, role, source)` composite UNIQUE index -- prevents same job from same source
- All INSERT statements use `ON CONFLICT DO NOTHING` or `ON CONFLICT DO UPDATE` to handle duplicates gracefully
- Per-adapter watermark filtering (`since` param) prevents re-fetching old jobs

**Implementation Status:** Working
**File Locations:** `api/agents/jd_ingestion/tools.py` (SHA-256 + HeadObject + ON CONFLICT), `infra/schema.sql` (UNIQUE constraints)
**Test Coverage:** `tests/test_lambda_fetch.py`, `tests/test_lambda_persist.py`

---

#### FR-1.1.3: Normalization to Unified NormalizedJob Schema

**Definition:** All source adapters convert heterogeneous API responses into a single `NormalizedJob` dataclass before storage.

**Acceptance Criteria:**
- Every adapter's `fetch()` method returns `list[NormalizedJob]`
- All fields are populated (with sensible defaults for missing data)
- `raw_json` preserves the original source payload for debugging
- `source` field identifies the adapter that produced the record
- `source_id` provides the external identifier for dedup

**Implementation Status:** Working
**File Locations:** `api/agents/jd_ingestion/adapters/base.py` (dataclass definition), `api/agents/jd_ingestion/adapters/*.py` (per-adapter normalization)
**Test Coverage:** `tests/test_source_adapters.py`

---

#### FR-1.1.4: JD Ingestion Agent (Unified LangGraph Agent)

**Definition:** The JD Ingestion Agent is a unified LangGraph agent on ECS that replaces three former components: Lambda Fetch, Lambda Persist, and the standalone Sponsorship Screener agent. It handles the full ingestion pipeline — fetch, screen, store — as a single graph with 3 conditional edges.

**Acceptance Criteria:**
- LangGraph StateGraph with typed state, 3 conditional edges:
  1. `fetch →(has_content)→ screen_sponsorship`, `→(no_content)→ END`
  2. `screen_sponsorship →(qualified)→ store_s3`, `→(disqualified)→ mark_skipped`
  3. `store_s3 →(success)→ persist_db`, `→(failure)→ END`
- Consumes SQS messages (EventBridge-scheduled and on-demand) via `sqs:ReceiveMessage`
- Runs all source adapters from `api/agents/jd_ingestion/adapters/`
- Sponsorship screening happens BEFORE S3 storage (prevents KB pollution)
- Disqualified JDs are marked `analysis_status='skipped'` and never stored to S3
- Replaces: `lambda/fetch/handler.py`, `lambda/persist/handler.py`, `api/agents/sponsorship_screener/`
- SSRF validation (`_validate_url()`) runs on all external URL fetches
- Content-hash dedup (SHA-256) and S3 HeadObject pre-check preserved from former Lambda Fetch

**Implementation Status:** Working
**File Locations:** `api/agents/jd_ingestion/graph.py`, `api/agents/jd_ingestion/tools.py`, `api/agents/jd_ingestion/adapters/`, `api/agents/jd_ingestion/adapter_registry.py`
**Test Coverage:** `tests/test_cloud_agents.py`

---

### FR-1.2: Email Processing

#### FR-1.2.1: Gmail Fetch with Readonly Scope

**Definition:** The system reads emails via the Google Gmail API using the `gmail.readonly` scope exclusively. OAuth flow runs on the host machine (which has a browser); the resulting `token.json` is mounted read-only into Docker.

**Acceptance Criteria:**
- Gmail API scope is exactly `gmail.readonly` -- no send, modify, or delete permissions
- `verify_gmail_scope()` runs at startup and fails loudly if the wrong scope is detected
- `token.json` lives on the host, mounted read-only into Docker via `docker-compose.yml`
- `credentials.json` is stored in AWS Secrets Manager (production) or local `.env` (dev)
- Neither `token.json` nor `credentials.json` is committed to git (pre-commit hook blocks)

**Implementation Status:** Working (Gmail-dependent -- requires OAuth setup)
**File Locations:** `local/gmail/auth.py`, `docker-compose.yml`
**Test Coverage:** None (requires live Gmail credentials)

---

#### FR-1.2.2: Three-Way Email Classification with Confidence Gating

**Definition:** The Email Classifier agent classifies each email into one of three categories: `irrelevant`, `status_update`, or `recommendation`. Classification uses RAG few-shot retrieval from local ChromaDB.

**Acceptance Criteria:**
- Three output classes: `irrelevant`, `status_update`, `recommendation`
- RAG: retrieves 5 most similar labeled examples from local ChromaDB `email_classifications` collection
- Cold start: ALL emails go to dashboard labeling queue (no few-shot examples available yet)
- Confidence gating at threshold `0.85` (defined as `AUTO_CONFIDENCE_THRESHOLD` in `local/agents/email_classifier/graph.py`), implemented as a LangGraph conditional edge:
  - `classify →(high confidence)→ auto_store`: stores label in `labeled_emails` + ChromaDB (grows training set)
  - `classify →(low confidence)→ enqueue_review`: queues for human review in `labeling_queue`
- Classification output schema: `{"email_type": "...", "confidence": float, "reasoning": "...", "company": "...", "role": "...", "urls": [...]}`
- Entity extraction (company, role, URLs) runs in parallel with classification
- Errors are caught per-email; failed emails go to `labeling_queue`

**Implementation Status:** Working (Gmail-dependent)
**File Locations:** `local/agents/email_classifier/graph.py`, `local/agents/email_classifier/tools.py`
**Test Coverage:** `tests/test_local_agents_refactored.py`

---

#### FR-1.2.3: Eight-Stage Application Classification

**Definition:** The Stage Classifier agent classifies status_update emails into one of 8 application stages. Uses the same RAG few-shot mechanism as the Email Classifier.

**Acceptance Criteria:**
- Eight output stages: `to_apply`, `waiting_for_referral`, `applied`, `assessment`, `assignment`, `interview`, `offer`, `rejected` (from `local/pipeline/schemas.py` `StatusPayload.stage` Literal type)
- Same confidence gating at `0.85` threshold
- High confidence: auto-store in `labeled_emails` with `confirmed_by = 'auto'`
- Low confidence: queue for human review with 8-option dropdown in dashboard
- Updates job status in `jobs` table on successful classification

**Implementation Status:** Working (Gmail-dependent)
**File Locations:** `local/agents/stage_classifier/graph.py`, `local/agents/stage_classifier/tools.py`
**Test Coverage:** `tests/test_local_agents_refactored.py`

---

#### FR-1.2.4: ChromaDB Few-Shot Learning Loop

**Definition:** Confirmed classifications become few-shot examples for future emails. The system bootstraps from zero data and improves over time.

**Acceptance Criteria:**
- User selects correct label in dashboard labeling queue
- Selection is stored in `labeled_emails` table and embedded in ChromaDB `email_classifications` collection
- Next classification retrieves better examples from the growing collection
- No fine-tuning -- few-shot only. LoRA is a documented future upgrade path.
- Embeddings produced by ONNX `all-MiniLM-L6-v2` model (local, CPU-only)

**Implementation Status:** Working
**File Locations:** `local/agents/shared/memory.py` (ChromaDB client), `local/agents/shared/embedder.py` (ONNX embedder), `local/agents/email_classifier/tools.py` (store function)
**Test Coverage:** `tests/test_local_agents_refactored.py`

---

### FR-1.3: JD Analysis

#### FR-1.3.1: Boilerplate Stripping

**Definition:** The JD Analyzer agent strips boilerplate sections (benefits, legal, salary ranges, equal opportunity statements) from raw JD text using LLM-based extraction.

**Acceptance Criteria:**
- Input: raw JD text from S3 (`jd_s3_key`)
- Output: cleaned text suitable for Bedrock KB ingestion
- Boilerplate sections (benefits, legal, EEO, salary disclaimers) are removed
- Cleaned text is stored in S3 under `jds/` prefix for Bedrock KB sync
- Original `raw_jd_text` is preserved in `jd_analyses` table

**Implementation Status:** Working
**File Locations:** `api/agents/jd_analyzer/graph.py`, `api/agents/jd_analyzer/tools.py`
**Test Coverage:** `tests/test_cloud_agents.py`

---

#### FR-1.3.2: Structured Field Extraction

**Definition:** The JD Analyzer extracts structured fields from raw JD text using Claude Haiku 4.5.

**Acceptance Criteria:**
- Extracted fields (mapped to `jd_analyses` table columns):
  - `required_skills` (TEXT[]) -- e.g., `["Python", "SQL", "distributed systems"]`
  - `preferred_skills` (TEXT[]) -- e.g., `["Kubernetes", "Go"]`
  - `tech_stack` (TEXT[]) -- e.g., `["AWS", "PostgreSQL", "Docker"]`
  - `role_type` (TEXT) -- e.g., `backend`, `frontend`, `fullstack`, `ml`, `devops`
  - `experience_range` (INT4RANGE) -- e.g., `[2,5)` years
  - `deal_breakers` (TEXT[]) -- e.g., `["clearance_required", "no_sponsorship"]`
  - `remote_policy` (TEXT) -- `remote`, `hybrid`, `onsite`, `unknown`
  - `confidence_scores` (JSONB) -- per-field confidence from LLM
- Fields with confidence < 0.7 trigger a second-pass `resolve_ambiguity` step
- Results stored via `ON CONFLICT (job_id) DO UPDATE` (re-analysis safe)

**Implementation Status:** Working
**File Locations:** `api/agents/jd_analyzer/graph.py`, `api/agents/jd_analyzer/tools.py`, `infra/schema.sql` (jd_analyses table)
**Test Coverage:** `tests/test_cloud_agents.py`

---

#### FR-1.3.3: Sponsorship Screening (Pre-Storage Gate)

**Definition:** Sponsorship screening is now performed by the JD Ingestion Agent BEFORE S3 storage, acting as a quality gate that prevents KB pollution. JDs with disqualifying sponsorship/clearance requirements are marked `analysis_status='skipped'` and never stored to S3 or ingested into Bedrock KB. This replaced the former standalone Sponsorship Screener agent that ran post-storage in the analysis chain.

**Acceptance Criteria:**
- Detects sponsorship restrictions, security clearance requirements, and citizenship requirements
- Runs inside JD Ingestion Agent as a conditional edge BEFORE S3 upload (not after)
- Disqualified JDs: set `analysis_status='skipped'` in `jobs` table, skip S3 storage entirely
- Qualified JDs: proceed to S3 upload and database persist
- Uses Claude Haiku 4.5 for nuanced language analysis (not just keyword matching)
- Prevents KB pollution: only qualified JDs reach Bedrock KB
- Former chain `JD Analyzer -> Sponsorship Screener -> Resume Matcher` replaced by `JD Analyzer -> Resume Matcher` (sponsorship screening happens upstream in ingestion)

**Implementation Status:** Working
**File Locations:** `api/agents/jd_ingestion/graph.py`, `api/agents/jd_ingestion/tools.py`
**Test Coverage:** `tests/test_cloud_agents.py`

---

### FR-1.4: Resume Matching

#### FR-1.4.1: Resume Upload with Presidio PII Redaction

**Definition:** Users upload resumes via `localhost:8001`. Presidio redacts PII (names, emails, phones, SSNs, etc.) before the resume reaches S3 or any cloud service.

**Acceptance Criteria:**
- Upload endpoint at `localhost:8001` (resume_service.py)
- Presidio + spaCy `en_core_web_lg` NER detects: PERSON, EMAIL, PHONE, SSN, CREDIT_CARD, LOCATION, PASSPORT, IP_ADDRESS
- Redacted resume uploaded to S3 under `resumes/` prefix
- Resume record stored in `resumes` table: `id`, `name`, `s3_key`, `uploaded_at`
- Multiple resumes supported (each gets its own ranked results)

**Implementation Status:** Working
**File Locations:** `local/resume_service.py`, `local/agents/shared/redactor.py`, `infra/schema.sql` (resumes table)
**Test Coverage:** `tests/test_pii_boundary.py`

---

#### FR-1.4.2: Four-Stage RAG Pipeline

**Definition:** Resume Matcher uses a 4-stage RAG pipeline to find best-fit JDs from a corpus of 10K+ job descriptions.

**Acceptance Criteria:**
- **Stage 1 -- Recall:** Bedrock KB vector search (`bedrock_agent_runtime.retrieve()`) retrieves top-50 JDs using Titan Embeddings v2 (1024-dim) over managed OpenSearch Serverless
- **Early exit:** Conditional edge — if recall returns empty results, graph routes directly to END (skips filter/rerank/store)
- **Stage 2 -- Resolve:** Batch SQL lookup maps `s3_uri` -> `jd_s3_key` -> `job_id`, dropping orphaned KB documents. Pre-resolved candidates (with `job_id` already set) skip this step.
- **Stage 3 -- Filter:** SQL on `jd_analyses` removes deal-breakers (sponsorship, clearance, experience range mismatch) by `job_id`
- **Stage 4 -- Rerank:** Claude Sonnet 4.6 scores each remaining candidate with structured JSON output (fit_score, reasoning, skill_gaps). Approximately 20 LLM calls per resume, not 10K.
- Top results stored in `match_reports` table with `ON CONFLICT (resume_id, job_id) DO UPDATE`
- Supports targeted mode (specific `job_id`) for `new_jd` events

**Implementation Status:** Working
**File Locations:** `api/agents/resume_matcher/graph.py`, `api/agents/resume_matcher/tools.py`
**Test Coverage:** `tests/test_cloud_agents.py`

---

#### FR-1.4.3: Bedrock KB Ingestion Pipeline

**Definition:** Stripped JD texts are ingested into Bedrock Knowledge Bases for managed vector search. Only qualified JDs (post-sponsorship-screening in JD Ingestion Agent) reach S3 and Bedrock KB. Disqualified JDs are marked `analysis_status='skipped'` and never stored to S3.

**Acceptance Criteria:**
- Only JDs that pass sponsorship screening in JD Ingestion Agent are stored in S3 (`s3://bucket/jds/`)
- Disqualified JDs are set to `analysis_status='skipped'` and never reach S3 or KB
- Bedrock KB data source points at the S3 `jds/` prefix
- Bedrock auto-chunks, embeds (Titan Embeddings v2), and indexes into managed OpenSearch Serverless
- New JDs trigger KB sync via `bedrock_agent.start_ingestion_job()`
- KB sync must run after new JDs land for non-targeted recall to work

**Implementation Status:** Partial -- KB sync trigger not automated (manual or needs EventBridge rule)
**File Locations:** `api/agents/resume_matcher/tools.py`
**Test Coverage:** `tests/test_cloud_agents.py`

---

### FR-1.5: Application Tracking

#### FR-1.5.1: Deadline Extraction

**Definition:** The Deadline Tracker agent extracts concrete dates from emails (assessment deadlines, interview dates, offer expiry).

**Acceptance Criteria:**
- Extracts dates from email content using Phi-3
- Stores in `deadlines` table: `job_id`, `deadline_text` (original text), `deadline_date` (parsed DATE)
- Dashboard surfaces upcoming deadlines sorted by date
- `StatusPayload.deadline` (from `local/pipeline/schemas.py`) is `Optional[date]`

**Implementation Status:** Working
**File Locations:** `local/agents/deadline_tracker/`, `infra/schema.sql` (deadlines table)
**Test Coverage:** `tests/test_local_agents_refactored.py`

---

#### FR-1.5.2: Follow-Up Urgency Scoring

**Definition:** The Follow-up Advisor agent performs daily scans of stale applications and scores urgency.

**Acceptance Criteria:**
- Two modes: `classify_email` (from status updates) and `daily_check` (scheduled scan)
- Daily check queries: `status IN ('assessment', 'assignment', 'interview') AND last_updated < NOW() - threshold AND follow_up_snoozed IS NULL`
- Thresholds from `config` table (not hardcoded): assessment: 7 days, assignment: 7 days, interview: 5 days
- Urgency levels: `high`, `medium`, `low` (from `local/pipeline/schemas.py` `FollowupPayload.urgency` Literal type)
- Recommended actions: `send_followup`, `check_status`, `withdraw` (from `FollowupPayload.action`)
- Results stored in `followup_recommendations` table
- Status changes reset `follow_up_flagged` to FALSE and `follow_up_snoozed` to NULL

**Implementation Status:** Working
**File Locations:** `local/agents/followup_advisor/graph.py`, `local/agents/followup_advisor/tools.py`, `infra/schema.sql` (followup_recommendations table)
**Test Coverage:** `tests/test_local_agents_refactored.py`

---

#### FR-1.5.3: Recommendation Parser

**Definition:** The Recommendation Parser agent extracts company and role from email recommendations.

**Acceptance Criteria:**
- Extracts `company` (max 100 chars) and `role` (max 200 chars) from recommendation emails
- Validates against `COMPANY_REGEX = r"^[a-zA-Z0-9\s.,&'()\-]+$"` (from `local/pipeline/schemas.py`)
- Sends validated `RecommendationPayload` to cloud ingest endpoint

**Implementation Status:** Working
**File Locations:** `local/agents/recommendation_parser/tools.py`, `local/pipeline/schemas.py`
**Test Coverage:** `tests/test_pipeline_schemas.py`

---

### FR-1.6: Dashboard

#### FR-1.6.1: Four Job Views

**Definition:** The dashboard provides four distinct views of job data.

**Acceptance Criteria:**
- **Chronological view:** All pending jobs sorted by `date_posted DESC`
- **Best-match view (per resume):** Jobs ranked by `overall_fit_score` from `match_reports` table, grouped by `resume_id`
- **Follow-up view:** Jobs filtered by `urgency_level`, `acted_on`, with follow-up recommendations from `followup_recommendations` table
- **Email queue view:** Unresolved items from `labeling_queue` table with classification dropdowns (3 options for email type, 8 for application stage)
- **Job detail view:** JD analysis, match report per resume, application timeline, deadlines

**Implementation Status:** Working
**File Locations:** `api/static/` (HTML/JS/CSS), `api/main.py` (API endpoints)
**Test Coverage:** `tests/test_api.py`

---

#### FR-1.6.2: JWT Authentication

**Definition:** Dashboard access is protected by JWT authentication with HttpOnly cookies.

**Acceptance Criteria:**
- JWT stored in HttpOnly, Secure, SameSite=Strict cookies
- 8-hour token expiry
- bcrypt password hashing
- Rate limiting on `/login`: 5 attempts per minute (via slowapi)
- Only `/health`, `/login`, and `/` (redirect) are unprotected
- All `/api/*` endpoints require `Depends(require_auth)` (JWT) or `Depends(require_hmac_auth)` (HMAC)
- Background tasks (analysis polling) expose NO new HTTP endpoints

**Implementation Status:** Working
**File Locations:** `api/main.py` (require_auth, login endpoint), `api/iam_auth.py` (require_hmac_auth)
**Test Coverage:** `tests/test_api.py`, `tests/test_ingestion_api.py`

---

#### FR-1.6.3: Labeling Queue

**Definition:** Low-confidence email classifications are surfaced in the dashboard for human review. User corrections feed back into the few-shot system.

**Acceptance Criteria:**
- Items from `labeling_queue` table displayed with agent's guessed stage/company/role
- Dashboard UI tab with card interface: each card shows email snippet, agent guess, and confidence
- Label dropdowns (3 options for email type, 8 for application stage) with confirm button
- Queue metrics endpoint (`/api/queue/metrics`) surfaces queue depth and resolution rate
- Resolution triggers: update `labeled_emails`, embed in ChromaDB, mark `resolved = TRUE`
- `confirmed_by` field tracks whether label came from `user` or `auto`

**Implementation Status:** Working
**File Locations:** `api/main.py` (`/api/queue`, `/api/queue/{id}/resolve`, `/api/queue/metrics`), `api/static/` (queue UI tab), `infra/schema.sql` (labeling_queue table)
**Test Coverage:** `tests/test_api.py`

---

### FR-1.7: Orchestration

#### FR-1.7.1: APScheduler Local Scheduling

**Definition:** Local agents are triggered on a schedule by APScheduler running in the main Docker container.

**Acceptance Criteria:**
- Email check: `interval` trigger, every 2 hours
- Daily follow-up: `cron` trigger, 9:05 AM local time
- Both jobs registered in `local/main.py` via `AsyncIOScheduler`

**Implementation Status:** Working (Gmail-dependent for email check)
**File Locations:** `local/main.py`
**Test Coverage:** `tests/test_local_agents_refactored.py`

---

#### FR-1.7.2: Cloud Coordinator Event Routing

**Definition:** The Cloud Coordinator agent routes events to specialist agent chains based on event type.

**Acceptance Criteria:**
- Event routing map:
  - `new_jd` -> `[jd_analyzer, resume_matcher]` (sponsorship screening now handled upstream by JD Ingestion Agent)
  - `chat` -> `[application_chat]`
  - `email_recommendation` -> create job record, enqueue for JD fetch
- Uses Claude Haiku 4.5 for classification of ambiguous events
- Logs every invocation in `orchestration_runs` table: `run_id` (UUID), `event_type`, `agent_chain[]`, `agent_results` (JSONB), `status`, timestamps

**Implementation Status:** Working
**File Locations:** `api/agents/cloud_coordinator/graph.py`, `api/agents/cloud_coordinator/tools.py`
**Test Coverage:** `tests/test_cloud_agents.py`

---

#### FR-1.7.3: Analysis Poller with Circuit Breaker

**Definition:** Background task in ECS that polls RDS for unanalyzed jobs and triggers the Cloud Coordinator.

**Acceptance Criteria:**
- Queries: `SELECT jobs WHERE analysis_status = 'pending' ORDER BY created_at ASC LIMIT 5`
- Status transitions: `pending` -> `analyzing` -> `completed`/`failed`/`skipped`
- Replaces previous approach (LEFT JOIN jd_analyses WHERE ja.id IS NULL + in-memory `_poll_failed_jobs` set)
- Bounded batch size (LIMIT 5) to prevent resource exhaustion
- Catches ALL exceptions (never crashes the main FastAPI process)
- Logs errors via `logger.exception()` for CloudWatch visibility
- Circuit breaker: on first `RuntimeError` with "not enabled", breaks the batch loop early
- Respects `analysis_polling_enabled` config flag from `config` table
- Runs as asyncio background task within the ECS FastAPI process

**Implementation Status:** Working
**File Locations:** `api/main.py` (analysis polling loop)
**Test Coverage:** `tests/test_api.py`

---

### FR-1.8: Data Validation

#### FR-1.8.1: Pydantic Schema Validation at Local-to-Cloud Boundary

**Definition:** All data crossing the local-to-cloud boundary must be validated against Pydantic models to enforce the IDs+enums-only contract.

**Acceptance Criteria:**
- Three payload models in `local/pipeline/schemas.py`:
  - `StatusPayload`: `job_id: int`, `stage: Literal[8 values]`, `deadline: Optional[date]`
  - `RecommendationPayload`: `company: str (max 100)`, `role: str (max 200)`, validated against `COMPANY_REGEX`
  - `FollowupPayload`: `job_id: int`, `urgency: Literal["high", "medium", "low"]`, `action: Literal["send_followup", "check_status", "withdraw"]`
- Only structured, non-PII data crosses the boundary
- HMAC-SHA256 signing on all cross-boundary payloads (`{timestamp}.{payload}`, +/-5 min drift window)

**Implementation Status:** Working
**File Locations:** `local/pipeline/schemas.py`, `local/pipeline/allowlist.py`, `api/iam_auth.py`
**Test Coverage:** `tests/test_pipeline_schemas.py`, `tests/test_pipeline_validator.py`, `tests/test_ingestion_api.py`

---

#### FR-1.8.2: SQL Schema Sync Static Analysis

**Definition:** Static analysis verifies that SQL queries in application code reference columns and constraints that exist in `infra/schema.sql`.

**Acceptance Criteria:**
- Validates that all column references in SQL queries match the schema
- Validates that `ON CONFLICT (col)` references match UNIQUE constraints
- Validates ORDER BY and WHERE clauses reference valid columns
- Runs as part of the test suite

**Implementation Status:** Working
**File Locations:** `tests/test_sql_schema_sync.py`
**Test Coverage:** `tests/test_sql_schema_sync.py`

---

### FR-1.9: Application Chat

#### FR-1.9.1: Contextual Q&A with Answer Memory

**Definition:** The Application Chat agent answers questions about specific jobs using JD analysis, match reports, and prior Q&A history.

**Acceptance Criteria:**
- Uses Claude Sonnet 4.6 for reasoning
- Retrieves context from `jd_analyses`, `match_reports`, and `answer_memory` tables
- Stores Q&A pairs in `answer_memory` table for RAG retrieval in future conversations
- Accessible via `/api/chat` endpoint (JWT-protected)

**Implementation Status:** Working
**File Locations:** `api/agents/application_chat/tools.py`, `api/main.py`, `infra/schema.sql` (answer_memory table)
**Test Coverage:** `tests/test_cloud_agents.py`

---

## Section 2: Non-Functional Requirements

### NFR-2.1: Performance

#### NFR-2.1.1: JD Analysis Latency

**Requirement:** End-to-end JD analysis (boilerplate stripping + structured extraction + sponsorship screening) must complete in under 2 minutes per JD.

**Acceptance Criteria:**
- From `jd_s3_key` detection to `jd_analyses` row insertion < 120 seconds
- Includes one LLM call (JD Analyzer) via Bedrock (sponsorship screening now happens upstream in JD Ingestion Agent)
- Measured via `orchestration_runs` table (`started_at` to `completed_at`)

**Implementation Status:** Working
**File Locations:** `api/agents/jd_analyzer/`, `api/agents/jd_ingestion/`, `api/main.py`

---

#### NFR-2.1.2: Email Classification Latency

**Requirement:** Single email classification (including embedding + ChromaDB retrieval + LLM call) must complete in under 30 seconds.

**Acceptance Criteria:**
- ONNX embedding + ChromaDB similarity search + Phi-3 classification < 30s
- Measured via MLflow agent tracking

**Implementation Status:** Working
**File Locations:** `local/agents/email_classifier/`, `local/agents/shared/embedder.py`

---

#### NFR-2.1.3: Resume Matching Latency

**Requirement:** Full RAG pipeline (recall + resolve + filter + rerank) must complete in under 60 seconds per resume.

**Acceptance Criteria:**
- Bedrock KB recall (50 results) + SQL resolve + SQL filter + ~20 LLM rerank calls < 60s
- Measured via `orchestration_runs` table

**Implementation Status:** Working
**File Locations:** `api/agents/resume_matcher/`

---

#### NFR-2.1.4: Corpus Scale

**Requirement:** The system must support 10K+ job descriptions in the Bedrock KB vector store without degradation.

**Acceptance Criteria:**
- Bedrock KB retrieves top-50 from 10K+ documents within 5 seconds
- Database queries with proper indexes perform well at 10K+ rows
- Adapter watermark filtering prevents unbounded growth

**Implementation Status:** Partial -- not tested at 10K scale (current corpus ~1000)
**File Locations:** `api/agents/resume_matcher/tools.py`, `infra/schema.sql` (indexes)

---

#### NFR-2.1.5: Email Processing Throughput

**Requirement:** The system must process 50+ emails per 2-hour cycle without timing out.

**Acceptance Criteria:**
- Batch email processing with per-email error handling (failed emails don't block the batch)
- ChromaDB retrieval scales with collection size
- Parallel embedding + entity extraction within each email

**Implementation Status:** Partial -- not tested at 50+ email volume
**File Locations:** `local/agents/email_classifier/graph.py`

---

### NFR-2.2: Security

#### NFR-2.2.1: PII Isolation (Network-Level)

**Requirement:** PII-touching components (Gmail, email content, raw resumes) run exclusively in local Docker. PII never enters AWS.

**Acceptance Criteria:**
- Local agents: Email Classifier, Stage Classifier, Recommendation Parser, Deadline Tracker, Follow-up Advisor -- all in Docker
- Cloud agents: Cloud Coordinator, JD Ingestion, JD Analyzer, Resume Matcher, Application Chat -- all on ECS
- `enforce_pii_boundary(data)` called before every cloud-bound write
- Defined in `local/agents/shared/redactor.py`
- Cross-boundary payloads validated against Pydantic schemas (IDs + enums only)

**Implementation Status:** Working
**File Locations:** `local/agents/shared/redactor.py`, `local/pipeline/schemas.py`
**Test Coverage:** `tests/test_pii_boundary.py`

---

#### NFR-2.2.2: Gmail Readonly Enforcement

**Requirement:** The system must use `gmail.readonly` scope exclusively. No send, modify, or delete permissions.

**Acceptance Criteria:**
- OAuth scope is exactly `gmail.readonly`
- `verify_gmail_scope()` check at startup
- Failure to verify scope prevents the email pipeline from running

**Implementation Status:** Working
**File Locations:** `local/gmail/auth.py`

---

#### NFR-2.2.3: SQL Injection Prevention

**Requirement:** All database queries must use parameterized queries. No f-string, `.format()`, or string concatenation in SQL.

**Acceptance Criteria:**
- asyncpg: positional parameters `$1, $2, ...`
- psycopg2: format parameters `%s`
- Verification: `grep -rn "f\".*SELECT\|f\".*INSERT\|f\".*UPDATE\|\.format.*SELECT" api/ lambda/ local/` returns zero matches

**Implementation Status:** Working
**File Locations:** All `*.py` files with SQL queries
**Test Coverage:** `tests/test_sanitize.py`

---

#### NFR-2.2.4: Prompt Injection Defense

**Requirement:** All user-provided text injected into LLM prompts must be sanitized via `sanitize_for_prompt()`.

**Acceptance Criteria:**
- `sanitize_for_prompt()` strips: system-prompt overrides, instruction injections, identity manipulation, code blocks, XML tag injection
- Length-capped: 8K (local Phi-3), 16K (cloud Bedrock)
- Applied on both Ollama and Bedrock paths
- Defined in `local/agents/shared/llm.py` and `api/agents/bedrock_client.py`
- Every call to `invoke_model()` or `llm_generate()` wraps user text

**Implementation Status:** Working
**File Locations:** `local/agents/shared/llm.py`, `api/agents/bedrock_client.py`
**Test Coverage:** `tests/test_sanitize.py`

---

#### NFR-2.2.5: SSRF Protection

**Requirement:** All external URL fetches in the JD Ingestion Agent (ECS) must pass through SSRF validation.

**Acceptance Criteria:**
- `_validate_url()` in `api/agents/jd_ingestion/tools.py` performs DNS resolution and rejects:
  - Non-HTTP schemes
  - Private IPs (`is_private`)
  - Loopback IPs (`is_loopback`)
  - Link-local IPs (`is_link_local`)
  - AWS metadata endpoint (169.254.169.254)
- Redirect chains validated via `_SsrfSafeRedirectHandler` (re-validates each hop)
- IMDSv2 enforced (`http_tokens = "required"`) on NAT instance
- Adapter base class also has `_validate_url()` in `api/agents/jd_ingestion/adapters/base.py`
- Same SSRF validation code, moved from Lambda to ECS as part of JD Ingestion Agent consolidation

**Implementation Status:** Working
**File Locations:** `api/agents/jd_ingestion/tools.py`, `api/agents/jd_ingestion/adapters/base.py`
**Test Coverage:** `tests/test_ssrf.py`

---

#### NFR-2.2.6: Two-Tier Authentication

**Requirement:** Dashboard uses JWT; service-to-service uses HMAC-SHA256.

**Acceptance Criteria:**
- **JWT (dashboard):** HttpOnly, Secure, SameSite=Strict cookies. 8h expiry. bcrypt password.
- **HMAC (service-to-service):** HMAC-SHA256 over `{timestamp}.{payload}`. +/-5 minute drift window (`MAX_TIMESTAMP_DRIFT = 300`). `INGEST_HMAC_KEY` from Secrets Manager.
- JWT protects all `/api/*` endpoints via `Depends(require_auth)`
- HMAC protects all `/api/ingest/*` endpoints via `Depends(require_hmac_auth)`

**Implementation Status:** Working
**File Locations:** `api/main.py`, `api/iam_auth.py`
**Test Coverage:** `tests/test_api.py`, `tests/test_ingestion_api.py`

---

#### NFR-2.2.7: Secrets Management

**Requirement:** Production secrets from AWS Secrets Manager. Local dev from `.env` with fallback.

**Acceptance Criteria:**
- `get_secret()` in `local/agents/shared/secrets.py` checks Secrets Manager first, falls back to `os.environ`
- `.env` file is gitignored and blocked by pre-commit hook
- Secrets stored: DB credentials, Gmail credentials, JWT secret, HMAC key

**Implementation Status:** Working
**File Locations:** `local/agents/shared/secrets.py`

---

#### NFR-2.2.8: Docker Non-Root Execution

**Requirement:** All Docker containers run as a non-root user.

**Acceptance Criteria:**
- `Dockerfile` and `Dockerfile.local` both have `USER appuser` before `CMD`
- No unnecessary packages; `apt-get` cleanup in same `RUN` layer

**Implementation Status:** Working
**File Locations:** `Dockerfile.local`, `api/Dockerfile`

---

#### NFR-2.2.9: SSL/TLS on All RDS Connections

**Requirement:** All database connections use `sslmode="require"` (not `"prefer"`).

**Acceptance Criteria:**
- asyncpg connections: `ssl="require"` parameter
- psycopg2 connections: `sslmode="require"` in connection string
- ALB: HTTPS listener with `ELBSecurityPolicy-TLS13-1-2-2021-06`
- VPC Endpoints: `private_dns_enabled = true`

**Implementation Status:** Working
**File Locations:** `local/agents/shared/db.py`, `lambda/persist/handler.py`, `infra/ecs.tf`

---

#### NFR-2.2.10: IAM Least Privilege

**Requirement:** No `"Action": "*"` or `"Resource": "*"` in IAM policies (except documented EC2 ENI limitation).

**Acceptance Criteria:**
- Every IAM statement names specific actions and scopes to specific resource ARNs
- EC2 ENI exception is documented (AWS limitation for VPC-attached Lambdas)
- Lambda IAM roles removed (Lambda Fetch and Lambda Persist replaced by JD Ingestion Agent on ECS)
- ECS task role expanded with `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `s3:HeadObject` (previously on Lambda roles)
- Remaining roles: ECS Execution, ECS Task, Bedrock KB

**Implementation Status:** Partial -- `anthropic.claude-*` wildcard is broader than needed (Hack #2)
**File Locations:** `infra/iam.tf`

---

### NFR-2.3: Privacy

#### NFR-2.3.1: Federated Learning Architecture

**Requirement:** Email classification learning (ChromaDB few-shot) happens exclusively on the local device. No email content or classification data leaves the local Docker environment.

**Acceptance Criteria:**
- ChromaDB `email_classifications` collection runs in local Docker only
- ONNX embeddings computed locally on CPU
- No email body, subject, or snippet reaches any AWS service
- Only structured IDs and enums cross the boundary

**Implementation Status:** Working
**File Locations:** `local/agents/shared/memory.py`, `local/agents/shared/embedder.py`

---

#### NFR-2.3.2: Resume PII Redaction

**Requirement:** All resumes are redacted via Presidio before leaving the local device.

**Acceptance Criteria:**
- Presidio + spaCy `en_core_web_lg` detects and redacts: PERSON, EMAIL, PHONE, SSN, CREDIT_CARD, LOCATION, PASSPORT, IP_ADDRESS
- Redacted resume stored in S3; original stays local
- `enforce_pii_boundary()` validates redaction before cloud upload

**Implementation Status:** Working
**File Locations:** `local/agents/shared/redactor.py`, `local/resume_service.py`
**Test Coverage:** `tests/test_pii_boundary.py`

---

### NFR-2.4: Reliability

#### NFR-2.4.1: Exception Handling

**Requirement:** All agent pipelines catch exceptions per-item and continue processing the batch.

**Acceptance Criteria:**
- JD Ingestion Agent: per-record try-except (one bad SQS message does not crash the batch)
- Email Classifier: per-email error handling (failed emails go to queue)
- Analysis Poller: catches ALL exceptions, logs via `logger.exception()`
- JD Ingestion Agent: JSON parse errors caught and logged, not crash the handler

**Implementation Status:** Working
**File Locations:** `api/agents/jd_ingestion/graph.py`, `local/agents/email_classifier/graph.py`, `api/main.py`

---

#### NFR-2.4.2: CloudWatch Logging

**Requirement:** All ECS and Lambda logs stream to CloudWatch with 14-day retention.

**Acceptance Criteria:**
- ECS tasks stream stdout/stderr to CloudWatch Log Group
- Lambda functions use standard Python logging (auto-shipped to CloudWatch)
- Log group retention: 14 days
- Error-level logs include full tracebacks via `logger.exception()`

**Implementation Status:** Working
**File Locations:** `infra/ecs.tf` (CloudWatch log group), `infra/lambda.tf`

---

#### NFR-2.4.3: Health Dashboard

**Requirement:** Interactive debug dashboard shows status of all ~30 system components.

**Acceptance Criteria:**
- Accessible at `/static/debug_dashboard.html`
- Shows all components (local + cloud) as a clickable node graph
- Live health status: green/yellow/red per component
- Includes schema sync validation
- Includes cross-boundary ingest monitoring
- CloudWatch error log fetching per component

**Implementation Status:** Working
**File Locations:** `api/debug/health_checks.py`, `api/debug/topology.py`, `api/static/debug_dashboard.html`
**Test Coverage:** `tests/test_debug_health_checks.py`, `tests/test_debug_api.py`

---

#### NFR-2.4.4: Pipeline Metrics

**Requirement:** Flexible time-series metrics table tracks operational health.

**Acceptance Criteria:**
- `pipeline_metrics` table: `source`, `metric_name`, `metric_value`, `recorded_at`
- Tracks: emails processed, validations failed, JDs analyzed, matches completed
- Queryable via `/api/ops/metrics` endpoint

**Implementation Status:** Working
**File Locations:** `infra/schema.sql` (pipeline_metrics table), `api/main.py`

---

### NFR-2.5: Cost Optimization

#### NFR-2.5.1: NAT Instance over NAT Gateway

**Requirement:** Use t3.nano NAT instance (~$3.80/mo) instead of managed NAT Gateway (~$33/mo).

**Acceptance Criteria:**
- t3.nano in public subnet with source/dest check disabled
- CloudWatch auto-recovery for HA
- Routes private-fetch subnet traffic through NAT instance
- Acceptable trade-off for single-user system

**Implementation Status:** Working
**File Locations:** `infra/main.tf`

---

#### NFR-2.5.2: ECS Fargate Pay-Per-Second

**Requirement:** ECS Fargate runs the API at minimal cost with pay-per-second billing.

**Acceptance Criteria:**
- Task definition: 256 CPU (0.25 vCPU), 512 MiB memory (smallest Fargate config)
- Desired count: 1 (single-user system)
- No EC2 capacity providers

**Implementation Status:** Working
**File Locations:** `infra/ecs.tf`

---

#### NFR-2.5.3: Model Cost Optimization

**Requirement:** Use the cheapest adequate model for each task.

**Acceptance Criteria:**
- Parsing/classification tasks: Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- Reasoning tasks (rerank, chat): Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`)
- Local tasks: Phi-3 Mini 3.8B (4-bit quantized) via Ollama (free)
- Embeddings (cloud): Titan Embeddings v2 1024-dim (managed by Bedrock KB)
- Embeddings (local): ONNX all-MiniLM-L6-v2 (free, CPU)

**Implementation Status:** Partial -- Haiku 4.5 may require Marketplace subscription workaround (see Hack #1 in WORK_REPORT)
**File Locations:** `api/agents/bedrock_client.py` (model IDs)

---

## Section 3: Constraints & Architectural Boundaries

### TECH-3.1: Technology Constraints

#### TECH-3.1.1: Python 3.11

**Constraint:** All application code must target Python 3.11.
**Enforcement:** Lambda runtime set to `python3.11` in `infra/lambda.tf`. CI/CD uses `--python-version 3.11`. Docker images based on Python 3.11 or compatible.

---

#### TECH-3.1.2: PostgreSQL 15

**Constraint:** All persistence is PostgreSQL. Never use SQLite.
**Enforcement:** RDS db.t3.micro running PostgreSQL 15. Local Docker runs PostgreSQL 15. Schema in `infra/schema.sql`.

---

#### TECH-3.1.3: httpx Only

**Constraint:** Use `httpx` for all HTTP client operations. Never use the `requests` library.
**Enforcement:** Code review. `requests` is not in requirements files.

---

#### TECH-3.1.4: LangGraph StateGraph Pattern

**Constraint:** All agents use LangGraph `StateGraph` with typed state (`TypedDict`), async nodes, conditional edges, and `.compile()` -> `.ainvoke()` execution.
**Enforcement:** Every agent directory contains `graph.py` with this pattern.

---

#### TECH-3.1.5: Database Driver Split

**Constraint:** `asyncpg` in local services and ECS. Lambda functions have been removed (replaced by JD Ingestion Agent on ECS).
**Enforcement:** `requirements.local.txt` and `requirements.txt` include asyncpg.

---

#### TECH-3.1.6: Environment Variable Configuration

**Constraint:** All configuration from environment variables. Never hardcode URLs, paths, or credentials.
**Enforcement:**
- `ONNX_MODEL_PATH` -- ONNX model file path
- `OLLAMA_BASE_URL` -- Ollama server URL (default: `http://ollama:11434` inside Docker)
- `MLFLOW_TRACKING_URI` -- MLflow server URL (default: `http://mlflow:5000` inside Docker)
- `S3_BUCKET` -- S3 bucket name
- `AWS_DEFAULT_REGION` -- AWS region
- `INGEST_HMAC_KEY` -- HMAC signing key
- `SQS_QUEUE_NAME` -- SQS queue name

---

#### TECH-3.1.7: Model Selection

**Constraint:** Specific models for specific tasks.
**Enforcement:**
- Local LLM: Ollama + Phi-3 Mini 3.8B (4-bit quantized)
- Cloud parsing: Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- Cloud reasoning: Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`)
- Cloud embeddings: Titan Embeddings v2 (1024-dim, managed by Bedrock KB)
- Local embeddings: ONNX all-MiniLM-L6-v2

---

#### TECH-3.1.8: Vector Store Split

**Constraint:** Two vector stores with different privacy levels.
**Enforcement:**
- ChromaDB (local Docker): email classifications only (contains PII)
- Bedrock Knowledge Bases (AWS): stripped JD texts (no PII), managed OpenSearch Serverless

---

### ARCH-3.2: Architectural Boundaries

#### ARCH-3.2.1: PII Boundary Rule

**Constraint:** If a component touches PII, it runs in `local/`. If not, it runs on AWS.
**Enforcement:**
- Local (Docker): Email Classifier, Stage Classifier, Recommendation Parser, Deadline Tracker, Follow-up Advisor, ChromaDB, ONNX Embedder, Presidio
- Cloud (ECS): Cloud Coordinator, JD Ingestion, JD Analyzer, Resume Matcher, Application Chat
- `enforce_pii_boundary()` validates before every cloud-bound write

---

#### ARCH-3.2.2: VPC Privilege Separation (Three Tiers)

**Constraint:** Three distinct network tiers with different capabilities.
**Enforcement:**
- **Public subnets** (10.0.0.0/20, 10.0.16.0/20): ALB, NAT instance
- **Private-fetch** (10.0.10.0/24): Formerly Lambda Fetch (now handled by JD Ingestion Agent on ECS) -- pending Terraform cleanup
- **Private-data** (10.0.20.0/24): RDS -- database + S3 (via VPC Gateway Endpoint), NO internet (Lambda Persist replaced by JD Ingestion Agent on ECS)
- Security groups enforce: RDS accepts connections from ECS SG + Lambda Persist SG only

---

#### ARCH-3.2.3: Agent Communication via LangGraph State

**Constraint:** All agents communicate via LangGraph state dictionaries. No message passing overhead, no shared mutable state.
**Enforcement:**
- Coordinator invokes specialists via direct `await agent_graph.ainvoke()` calls
- No pool objects in state (connection pools are not serializable for LangGraph checkpointing)
- Module-level singleton for database connection pool

---

#### ARCH-3.2.4: Async Event Flow

**Constraint:** Asynchronous event-driven architecture for all inter-service communication. Bedrock model invocations use `asyncio.to_thread()` to avoid blocking the FastAPI event loop.
**Enforcement:**
- SQS decouples job discovery from processing (300s visibility, DLQ after 3 retries)
- EventBridge schedules adapter runs via cron -> SQS -> JD Ingestion Agent (ECS)
- JD Ingestion Agent handles fetch, screen, S3 store, and DB persist in a single graph
- ECS background polling triggers Cloud Coordinator for JD analysis + resume matching
- `async_invoke_model()` and `async_retrieve_from_kb()` in `bedrock_client.py` wrap sync boto3 calls via `asyncio.to_thread()`
- All cloud agents (JD Analyzer, Sponsorship Screener, Resume Matcher, Application Chat) use the async wrappers exclusively
- All sync boto3 S3/SQS calls in async contexts (graph nodes, background tasks, endpoints) wrapped with `asyncio.to_thread()`
- SQS long-poll (`receive_message` with 20s wait) runs in thread to prevent event loop starvation

---

### DATA-3.3: Data Model Constraints

#### DATA-3.3.1: Enum Values

**Constraint:** All enum-like columns have defined valid values.
**Enforcement (from `infra/schema.sql` and `local/pipeline/schemas.py`):**

| Column | Table | Valid Values |
|--------|-------|-------------|
| `status` | jobs | `to_apply`, `waiting_for_referral`, `applied`, `assessment`, `assignment`, `interview`, `offer`, `rejected` |
| `stage` | labeled_emails | Stage 1: `irrelevant`, `status_update`, `recommendation`. Stage 2: `to_apply`, `waiting_for_referral`, `applied`, `assessment`, `assignment`, `interview`, `offer`, `rejected` |
| `referral_status` | jobs | `none`, `requested`, `received` |
| `referral_accepts` | jobs | `unknown`, `yes`, `no` |
| `urgency_level` | followup_recommendations | `high`, `medium`, `low` |
| `recommended_action` | followup_recommendations | `send_followup`, `check_status`, `withdraw` |
| `fit_category` | match_reports | `strong`, `moderate`, `weak` |
| `status` | orchestration_runs | `running`, `completed`, `failed` |
| `confirmed_by` | labeled_emails | `user`, `auto` |
| `analysis_status` | jobs | `pending`, `analyzing`, `completed`, `failed`, `skipped` |
| `source` | jobs | `github`, `email`, `manual`, `the_muse`, `simplify`, `greenhouse`, `lever`, `ashby`, `hn_hiring`, `email_recommendation` |
| `remote_policy` | jd_analyses | `remote`, `hybrid`, `onsite`, `unknown` |
| `role_type` | jd_analyses | `backend`, `frontend`, `fullstack`, `ml`, `devops`, etc. |
| `question_type` | answer_memory | `behavioral`, `technical`, `situational`, etc. |

---

#### DATA-3.3.2: UNIQUE Constraints and Dedup Semantics

**Constraint:** Seven UNIQUE constraints enforce data integrity.
**Enforcement:**

| Constraint | Table | Column(s) | Dedup Semantic |
|-----------|-------|-----------|----------------|
| 1 | jobs | `simplify_id` | External ID from Simplify feed |
| 2 | jobs | `jd_s3_key` | Content-addressed S3 key (SHA-256 hash) |
| 3 | jobs | `ats_url` | Direct ATS application link |
| 4 | jobs | `(company, role, source)` | Composite -- same job from same source |
| 5 | jd_analyses | `job_id` | One analysis per job |
| 6 | match_reports | `(job_id, resume_id)` | One match report per job-resume pair |
| 7 | labeled_emails | `email_id` | One label per Gmail message ID |

All INSERT statements use `ON CONFLICT DO NOTHING` or `ON CONFLICT DO UPDATE` to handle duplicates.

---

#### DATA-3.3.3: UTC Timestamps

**Constraint:** All database timestamps are UTC. Convert to local time only at the display layer.
**Enforcement:** All timestamp columns use `TIMESTAMPTZ` type with `DEFAULT NOW()`.

---

#### DATA-3.3.4: JSONB for Flexible Fields

**Constraint:** Use JSONB for heterogeneous or variable-structure data.
**Enforcement:**
- `raw_json` (jobs) -- original source API response
- `confidence_scores` (jd_analyses) -- per-field confidence values
- `match_candidates` (labeling_queue) -- potential job matches
- `agent_results` (orchestration_runs) -- per-agent results

---

#### DATA-3.3.5: TEXT[] Array Columns

**Constraint:** Use PostgreSQL native `TEXT[]` arrays for list fields. Do NOT use `json.dumps()` to serialize lists into TEXT columns.
**Enforcement:**
- `required_skills`, `preferred_skills`, `tech_stack`, `deal_breakers` (jd_analyses) -- all TEXT[]
- `skill_gaps` (match_reports) -- TEXT[]
- `agent_chain` (orchestration_runs) -- TEXT[]
- asyncpg expects Python lists for TEXT[] columns, not JSON strings

---

#### DATA-3.3.6: INT4RANGE for Experience

**Constraint:** Use PostgreSQL `INT4RANGE` type for experience ranges.
**Enforcement:** `experience_range` column in `jd_analyses` table. Constructed via `asyncpg.Range(min, max)` in Python, not string format.

---

### OPS-3.4: Operational Constraints

#### OPS-3.4.1: EventBridge Cron Expressions

**Constraint:** All scheduled adapter triggers use EventBridge cron expressions targeting SQS.
**Enforcement (from `infra/eventbridge.tf`):**
- Daily adapters (the_muse, adzuna): `cron(0 6 * * ? *)` -- 6 AM UTC
- Daily Simplify: `cron(0 6 * * ? *)` -- 6 AM UTC
- Monthly HN: `cron(0 9 1 * ? *)` -- 1st of month, 9 AM UTC
- All targets push to SQS queue so JD Ingestion Agent (ECS) picks them up

---

#### OPS-3.4.2: Batch Sizes and Timeouts

**Constraint:** Bounded processing to prevent resource exhaustion.
**Enforcement:**
- Analysis Poller: `LIMIT 5` per polling cycle
- SQS visibility timeout: 300 seconds
- SQS message retention: 4 days (345600 seconds)
- Lambda Fetch timeout: 60 seconds
- Lambda Persist timeout: 30 seconds

---

#### OPS-3.4.3: Lambda Configuration (DEPRECATED)

**Constraint:** Lambda Fetch and Lambda Persist have been replaced by the JD Ingestion Agent on ECS. Lambda infrastructure pending Terraform deletion.
**Former configuration (from `infra/lambda.tf`):**
- Lambda Fetch: Python 3.11, 128 MB, 60s timeout, private-fetch subnet, SQS trigger
- Lambda Persist: Python 3.11, 128 MB, 30s timeout, private-data subnet, S3 trigger
- Both replaced by JD Ingestion Agent (FR-1.1.4)

---

#### OPS-3.4.4: ECS Configuration

**Constraint:** ECS runs as minimal Fargate configuration.
**Enforcement (from `infra/ecs.tf`):**
- CPU: 256 (0.25 vCPU)
- Memory: 512 MiB
- Desired count: 1
- Network mode: awsvpc
- ALB health check: 30s interval, 2 healthy / 3 unhealthy threshold

---

#### OPS-3.4.5: S3 Content Validation

**Constraint:** JD Ingestion Agent validates S3 object size and format before processing (formerly Lambda Persist).
**Enforcement:**
- Reject S3 objects > 1 MB (`MAX_CONTENT_SIZE = 1_048_576`)
- JSON parse errors caught and logged, not crash the handler
- Both `.txt` and `.json` formats under `jds/` prefix handled

---

#### OPS-3.4.6: SQS Dead Letter Queue

**Constraint:** Failed messages are sent to DLQ after 3 retries.
**Enforcement:** SQS configured with DLQ. Messages that fail 3 times are moved to DLQ for inspection.

---

#### OPS-3.4.7: CI/CD with OIDC Federation

**Constraint:** GitHub Actions deploys using OIDC federation -- no static AWS keys in CI.
**Enforcement:** `.github/workflows/deploy.yml` uses `aws-actions/configure-aws-credentials` with OIDC. Pipeline: test -> build (`linux/amd64`) -> push to ECR -> ECS force-new-deployment. Lambda packaging uses `--platform manylinux2014_x86_64 --python-version 3.11`.

---

## Section 4: Acceptance Criteria & Verification

### Critical Path Table

| # | Feature | Status | Verification Method |
|---|---------|--------|-------------------|
| 1 | SQS -> JD Ingestion Agent (fetch -> screen -> S3 -> RDS) | Working | End-to-end pipeline test |
| 2 | Analysis Poller -> JD Analyzer -> Resume Matcher | Working | `orchestration_runs` table inspection |
| 3 | Email Classifier -> Stage Classifier -> Follow-up Advisor | Working (Gmail-dependent) | Smoke test with mock emails |
| 4 | Resume upload -> Presidio redact -> S3 -> Bedrock KB -> Match reports | Working | Dashboard verification |
| 5 | Dashboard auth (JWT) + all protected endpoints | Working | `tests/test_api.py` |
| 6 | HMAC-authenticated ingestion pipeline | Working | `tests/test_ingestion_api.py` |
| 7 | Debug dashboard with ~30 component health status | Working | Manual verification at `/static/debug_dashboard.html` |
| 8 | Pydantic validation at local-to-cloud boundary | Working | `tests/test_pipeline_schemas.py`, `tests/test_pipeline_validator.py` |

### Data Quality Verification

| Check | Method | File |
|-------|--------|------|
| SQL queries reference valid columns | Static analysis against schema.sql | `tests/test_sql_schema_sync.py` |
| Schema migrations are consistent | Schema comparison test | `tests/test_schema_migration.py` |
| ON CONFLICT clauses match UNIQUE constraints | Static analysis | `tests/test_sql_schema_sync.py` |
| Pydantic models match API expectations | Schema validation tests | `tests/test_pipeline_schemas.py` |
| PII does not cross boundary | Redactor unit tests | `tests/test_pii_boundary.py` |

### Security Verification Checklist

| # | Check | Command/Method | Expected Result |
|---|-------|---------------|-----------------|
| 1 | Secrets scan | `pre-commit run detect-secrets --all-files` | No findings |
| 2 | SQL injection | `grep -rn "f\".*SELECT\|f\".*INSERT" api/ lambda/ local/` | Zero matches |
| 3 | Prompt injection | Review all `invoke_model()` calls | All wrap with `sanitize_for_prompt()` |
| 4 | PII boundary | Review all INSERT/UPDATE in agent tools | All preceded by `enforce_pii_boundary()` |
| 5 | SSRF | Review all URL fetches in api/agents/jd_ingestion/ | All pass through `_validate_url()` |
| 6 | Auth coverage | Review all endpoints in api/main.py | Only `/health`, `/login`, `/` unprotected |
| 7 | SSL/TLS | Review connection strings | `sslmode="require"` everywhere |
| 8 | IAM | Review infra/iam.tf | No `Action: *` or `Resource: *` |
| 9 | Docker | Review Dockerfiles | `USER appuser` before CMD |
| 10 | Terraform | `terraform validate` | "Success! The configuration is valid." |
| 11 | Tests | `pytest tests/ -v` | 0 failures |
| 12 | TODOs | `grep -rn "TODO\|FIXME\|HACK" api/ lambda/ local/ infra/` | All resolved or justified |

---

## Section 5: Outstanding Items

Tracked in `LOCAL_ISSUES.md` (not committed to the repository).

---

## Section 6: Implicit Requirements

### Data Loss Prevention

| Mechanism | Implementation | File Location |
|-----------|---------------|---------------|
| Database transactions | asyncpg `async with pool.acquire() as conn` + `await conn.execute()` | `api/main.py`, agent tools |
| ON CONFLICT handling | All INSERTs use `ON CONFLICT DO NOTHING` or `DO UPDATE` | All SQL-writing code |
| S3 content-addressable storage | SHA-256 hash keys prevent accidental overwrites | `api/agents/jd_ingestion/tools.py` |
| RDS automated snapshots | AWS RDS default backup retention | `infra/main.tf` (RDS config) |
| SQS message retention | 4 days (345600s) for retry after transient failures | `infra/data.tf` |

### Auditability

| Audit Trail | What It Tracks | File Location |
|-------------|---------------|---------------|
| `orchestration_runs` table | Every agent invocation: run_id, event_type, agent_chain, results, status, timestamps | `infra/schema.sql` |
| `labeled_emails.confirmed_by` | Whether classification came from `user` (human review) or `auto` (confidence >= 0.85) | `infra/schema.sql` |
| `pipeline_metrics` table | Flexible time-series: source, metric_name, metric_value, recorded_at | `infra/schema.sql` |
| CloudWatch Logs | ECS + Lambda stdout/stderr with 14-day retention | `infra/ecs.tf`, `infra/lambda.tf` |
| MLflow experiment tracking | Per-agent metrics (duration, confidence, accuracy), parameters (email_id, event_type) | `local/agents/shared/tracking.py` |

### Idempotency

| Mechanism | Scope | Implementation |
|-----------|-------|---------------|
| `ON CONFLICT DO NOTHING` | All job inserts | `api/agents/jd_ingestion/tools.py`, `api/main.py` |
| `ON CONFLICT DO UPDATE` | Analysis re-runs, match re-runs, config updates | `api/agents/jd_analyzer/tools.py`, `api/agents/resume_matcher/tools.py` |
| S3 HeadObject dedup | Before writing JD content to S3 | `api/agents/jd_ingestion/tools.py` |
| Content hash addressing | SHA-256 of content -> S3 key | `api/agents/jd_ingestion/tools.py` |
| `run_id` (UUID) tracking | Per-orchestration run dedup | `orchestration_runs` table |
| Watermark filtering | Per-adapter `since` param prevents re-fetching | Adapter implementations |

### Graceful Degradation

| Failure | Behavior | Recovery |
|---------|----------|----------|
| Ollama down | Local agents skip LLM calls, return low-confidence defaults | Queue all emails for human review |
| Bedrock down | Circuit breaker in Analysis Poller breaks batch early | Retry on next polling cycle |
| Gmail down | APScheduler email check logs error, skips cycle | Retry on next 2-hour interval |
| RDS down | Health check fails, ALB marks ECS task unhealthy | ALB stops routing until recovery |
| ChromaDB down | Email classifier operates without few-shot context (cold-start mode) | Reduced accuracy, queue more for human review |
| MLflow down | Agents run without experiment tracking | `tracking.py` handles MLflow unavailability gracefully |
| S3 HeadObject fails (404) | Proceeds with put_object (normal path — object doesn't exist yet) | `api/agents/jd_ingestion/tools.py` |
| S3 HeadObject fails (non-404) | Re-raises error (permission denied, throttling, etc.) — surfaces misconfiguration early | `api/agents/jd_ingestion/tools.py` |

---

## Summary Table

| Category | Count | Working | Partial | Not Impl. | Notes |
|----------|-------|---------|---------|-----------|-------|
| FR-1.1: Job Discovery & Ingestion | 4 | 4 | 0 | 0 | 8 adapters active, 2 blacklisted, unified JD Ingestion Agent |
| FR-1.2: Email Processing | 4 | 4 | 0 | 0 | Gmail-dependent features untested in CI |
| FR-1.3: JD Analysis | 3 | 3 | 0 | 0 | |
| FR-1.4: Resume Matching | 3 | 2 | 1 | 0 | KB sync trigger not automated |
| FR-1.5: Application Tracking | 3 | 3 | 0 | 0 | |
| FR-1.6: Dashboard | 3 | 3 | 0 | 0 | |
| FR-1.7: Orchestration | 3 | 3 | 0 | 0 | |
| FR-1.8: Data Validation | 2 | 2 | 0 | 0 | |
| FR-1.9: Application Chat | 1 | 1 | 0 | 0 | |
| NFR-2.1: Performance | 5 | 3 | 2 | 0 | 10K scale + 50 email throughput untested |
| NFR-2.2: Security | 10 | 9 | 1 | 0 | IAM wildcard broader than needed |
| NFR-2.3: Privacy | 2 | 2 | 0 | 0 | |
| NFR-2.4: Reliability | 4 | 4 | 0 | 0 | |
| NFR-2.5: Cost | 3 | 2 | 1 | 0 | Haiku 4.5 Marketplace issue |
| TECH-3.1: Technology | 8 | 8 | 0 | 0 | |
| ARCH-3.2: Architecture | 4 | 4 | 0 | 0 | |
| DATA-3.3: Data Model | 6 | 6 | 0 | 0 | |
| OPS-3.4: Operational | 7 | 7 | 0 | 0 | |
| **TOTAL** | **75** | **70** | **5** | **0** | |

**Database Schema:** 13 tables (jobs, labeled_emails, labeling_queue, embedding_cache, config, jd_analyses, resumes, match_reports, followup_recommendations, orchestration_runs, answer_memory, deadlines, pipeline_metrics)

**Test Files:** 21 test files in `tests/`

**Agents:** 10 total (5 local: email_classifier, stage_classifier, recommendation_parser, deadline_tracker, followup_advisor; 5 cloud: cloud_coordinator, jd_ingestion, jd_analyzer, resume_matcher, application_chat) + 1 background task (analysis_poller)
