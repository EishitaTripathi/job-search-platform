# DEPENDENCIES.md -- Complete Project Dependency Map
**Last compiled: 2026-04-02**

> When something breaks, this doc tells you the blast radius.
> When you change a component, this doc tells you what else needs updating.

---

## Table of Contents

1. [Service-to-Service Dependencies](#1-service-to-service-dependencies)
2. [Complete Data Flow Traces](#2-complete-data-flow-traces)
3. [Agent I/O Contracts](#3-agent-io-contracts)
4. [Schema Access Map](#4-schema-access-map)
5. [Module Import Dependencies](#5-module-import-dependencies)
6. [Infrastructure Dependencies](#6-infrastructure-dependencies)
7. [Configuration Dependencies](#7-configuration-dependencies)

---

## 1. Service-to-Service Dependencies

### Docker Compose Service Graph (Local Dev)

```
postgres (port 5433->5432)
  ^-- app (depends_on: postgres[healthy], ollama[healthy], chromadb[started])
  ^-- debug (depends_on: postgres[healthy])

ollama (port 11434)
  ^-- app

chromadb (port 8000)
  ^-- app

mlflow (port 5001->5000)
  (no depends_on, but app connects at runtime via MLFLOW_TRACKING_URI)
```

- **app** (port 8001): APScheduler + resume upload service. Depends on `postgres` (healthy), `ollama` (healthy), `chromadb` (started). Connects to `mlflow` at runtime.
- **debug** (port 8002): Debug dashboard. Depends on `postgres` (healthy). Also mounts `api/` read-only for code inspection and reads AWS credentials for cloud status checks.
- **postgres**: Initialized with `infra/schema.sql` mounted to `/docker-entrypoint-initdb.d/01_schema.sql`.
- **ollama**: Phi-3 Mini LLM server. Model must be pulled manually on first run.
- **chromadb**: Vector store for email classification few-shot retrieval.
- **mlflow**: Experiment tracking. Uses SQLite backend (independent of postgres).

### Local-to-Cloud Boundary (Validation Pipeline)

The local pipeline sends validated, PII-free payloads to the cloud API via HMAC-signed HTTP POST requests.

**Chain:** `local/pipeline/validator.py` -> `local/pipeline/sender.py` -> HMAC POST -> `api/main.py` ingest endpoints

**5 Ingest Endpoints (all require HMAC auth via `api/iam_auth.py:require_hmac_auth`):**

| Endpoint | Method | Handler Location | Purpose |
|---|---|---|---|
| `/api/ingest/status` | POST | `api/main.py:614` | Status updates (stage changes, deadlines) |
| `/api/ingest/recommendation` | POST | `api/main.py:644` | New job recommendations (creates job + enqueues SQS fetch) |
| `/api/ingest/followup` | POST | `api/main.py:670` | Follow-up actions for stale jobs |
| `/api/ingest/resume` | POST | `api/main.py:692` | Resume metadata records |
| `/api/ingest/resume/{s3_key}` | DELETE | `api/main.py:711` | Delete resume record from cloud RDS |

### EventBridge -> SQS -> JD Ingestion Agent (ECS) Chain

```
EventBridge Scheduled Rules (infra/eventbridge.tf)
  |-- monthly_hn:      cron(0 9 1 * ? *)  -> SQS {source: "hn_hiring"}
  |-- daily_the_muse:  cron(0 6 * * ? *)  -> SQS {source: "the_muse", params: {category: "Engineering"}}
  |-- daily_simplify:  cron(0 6 * * ? *)  -> SQS {source: "simplify"}
  v
SQS Queue: job-search-platform-jd-scrape-queue (data.tf:10-22)
  |-- visibility_timeout: 300s, retention: 4 days, long polling: 20s
  |-- DLQ: job-search-platform-jd-scrape-dlq (3 retries, 14-day retention)
  v
ECS polls SQS -> JD Ingestion Agent (api/agents/jd_ingestion/) -- private-fetch subnet, NAT route
  |-- LangGraph with conditional routing
  |-- fetch: adapter_registry -> fetch jobs from source APIs
  |-- screen sponsorship: Bedrock Haiku filters before storage (prevents KB pollution)
  |-- if qualified:
  |     store to S3 put_object (jds/*.json)
  |     persist to RDS jobs table (ON CONFLICT jd_s3_key DO NOTHING)
  |     analyze (JD Analyzer) + match (Resume Matcher)
  v
RDS PostgreSQL (jobs table)
```

### ECS -> RDS/S3/Bedrock Chain (Cloud Coordinator Dispatch)

```
ECS Fargate Task (api/main.py) -- private-fetch subnet
  |-- Analysis Poller (_poll_unanalyzed_jobs, line 113)
  |   |-- Reads jobs + jd_analyses from RDS (find unanalyzed)
  |   |-- Reads JD text from S3 (via jd_s3_key)
  |   |-- Calls run_cloud_coordinator("new_jd", ...)
  |   v
  |-- Cloud Coordinator (api/agents/cloud_coordinator/graph.py)
  |   |-- JD Analyzer -> Bedrock Haiku (strip boilerplate + extract fields) -> RDS (jd_analyses)
  |   |-- Sponsorship Screener -> Bedrock Haiku (detect exclusion) -> RDS (jd_analyses.deal_breakers)
  |   |-- Resume Matcher -> S3 (read resumes) -> Bedrock KB (Titan v2 recall) -> Bedrock Sonnet (rerank) -> RDS (match_reports + jobs.match_score)
  |   |-- Application Chat -> RDS (context) -> Bedrock KB (retrieval) -> Bedrock Sonnet (answer) -> RDS (answer_memory)
  v
Dashboard: api/static/index.html served via ALB
```

### SQS Enqueue Path (Recommendation -> JD Fetch)

```
/api/ingest/recommendation (api/main.py:644)
  |-- INSERT INTO jobs (company, role, source='email_recommendation')
  |-- _enqueue_jd_fetch(job_id, company, role) -> SQS send_message
  v
JD Ingestion Agent (search mode): searches adapters for matching JD
```

---

## 2. Complete Data Flow Traces

### Trace 1: Email -> Dashboard

**Trigger:** APScheduler `email_check` job (every 2 hours, `local/main.py:165`)

```
Step 1: Gmail API fetch
  File: local/gmail/auth.py:60 (fetch_unread_emails)
  Action: Gmail API -> users().messages().list(q="is:unread") -> users().messages().get(format="full")
  Output: list[{email_id, subject, snippet, body}]

Step 2: Email Classifier (LangGraph)
  File: local/agents/email_classifier/graph.py:46 (classify_node)
  Reads FROM: ChromaDB email_classifications collection (few-shot examples)
              ONNX all-MiniLM-L6-v2 model (local/agents/shared/embedder.py:29)
              Ollama Phi-3 (local/agents/shared/llm.py:37)
  Action: RAG few-shot retrieval -> embed query -> query ChromaDB -> build prompt -> Phi-3 classify
  Output: {label, company, role, urls, confidence}

Step 3: Route by classification (local/agents/email_classifier/graph.py:62)
  If confidence >= 0.85: auto-store to ChromaDB + labeled_emails table (tools.py:131)
  If confidence < 0.85: enqueue to labeling_queue table (tools.py:174)

Step 4a: If label == "status_update" -> dispatch_status_update (local/agents/shared/dispatch.py:15)

  Step 4a.1: Stage Classifier (LangGraph)
    File: local/agents/stage_classifier/graph.py:48 (classify_node)
    Reads FROM: ChromaDB stage_classifications collection
                ONNX embedder, Ollama Phi-3
    Action: RAG few-shot -> classify into 8 stages
    Output: {stage, confidence}

  Step 4a.2: Route by confidence (graph.py:66)
    If confidence >= 0.85: store to ChromaDB stage_classifications + labeled_emails (tools.py:141)
    If confidence < 0.85: update labeling_queue.guessed_stage (tools.py:180)

  Step 4a.3: Job ID lookup (tools.py:200)
    File: local/agents/stage_classifier/tools.py:200 (lookup_job_id)
    Reads FROM: jobs table (ILIKE fuzzy match on company + role)

  Step 4a.4: Send StatusPayload through pipeline (tools.py:220)
    File: local/pipeline/validator.py:28 (validate_status)
    Action: Pydantic validation -> verify job_id exists in jobs table -> log to pipeline_metrics
    File: local/pipeline/sender.py:28 (send_to_cloud)
    Action: HMAC sign -> POST /api/ingest/status -> retry 3x with backoff

  Step 4a.5: (Conditional) If stage in {assessment, assignment, interview} -> Deadline Tracker
    File: local/agents/shared/dispatch.py:48
    File: local/agents/deadline_tracker/graph.py:31 (parse_deadlines_node)
    Reads FROM: Ollama Phi-3
    Action: Extract dates from email body -> validate ISO format
    File: local/agents/deadline_tracker/tools.py:76 (send_deadlines_to_pipeline)
    Action: For each deadline -> validate_status -> send_to_cloud("status", ...) with deadline date

Step 4b: If label == "recommendation" -> dispatch_recommendation (local/agents/shared/dispatch.py:62)

  Step 4b.1: Recommendation Parser (LangGraph)
    File: local/agents/recommendation_parser/graph.py:33 (extract_entities_node)
    Reads FROM: Ollama Phi-3
    Action: Extract {company, role} pairs from email body

  Step 4b.2: Validate and send (graph.py:48)
    File: local/agents/recommendation_parser/tools.py:74 (validate_and_send_recommendations)
    Action: For each pair -> validate_recommendation (Pydantic + Presidio PII check + company allowlist)
            -> send_to_cloud("recommendation", ...)

Step 5: Cloud API receives payload
  File: api/main.py:614 (/api/ingest/status) or api/main.py:644 (/api/ingest/recommendation)
  Action: HMAC verify -> UPDATE jobs / INSERT INTO jobs + enqueue SQS fetch
  Writes TO: RDS jobs table, deadlines table

Step 6: Dashboard reads from RDS
  File: api/main.py:330 (GET /api/jobs) -> SELECT from jobs, match_reports
  File: api/main.py:402 (GET /api/jobs/{id}) -> SELECT from jobs, jd_analyses, match_reports, followup_recommendations
```

### Trace 2: Job Discovery -> Match Report

**Trigger:** EventBridge schedule -> SQS message

```
Step 1: EventBridge fires scheduled rule
  File: infra/eventbridge.tf (e.g., daily_simplify at cron(0 6 * * ? *))
  Action: Send message to SQS queue: {source: "simplify", params: {}}

Step 2: ECS polls SQS -> JD Ingestion Agent
  File: api/agents/jd_ingestion/ (LangGraph with conditional routing)
  Action: Parse SQS message body -> route to adapter mode (source present)

Step 3: Fetch -> Screen -> Store -> Analyze -> Match (single agent pipeline)
  File: api/agents/jd_ingestion/ (LangGraph nodes)
  Action:
    fetch: Instantiate adapter (e.g., SimplifyAdapter) -> adapter.fetch(params)
           -> watermark filter (since param) -> S3 dedup (head_object)
    screen sponsorship: Bedrock Haiku evaluates sponsorship/clearance signals
           -> if disqualified: skip (prevents KB pollution)
    store to S3: Content hash -> s3_key = jds/{hash}.json -> s3.put_object
    persist to RDS: INSERT INTO jobs ON CONFLICT (jd_s3_key) DO NOTHING
    analyze: JD Analyzer extracts structured fields
    match: Resume Matcher runs RAG pipeline

Step 4: Analysis Poller detects unanalyzed job (fallback for missed jobs)
  File: api/main.py:113 (_poll_unanalyzed_jobs)
  Action: SELECT jobs LEFT JOIN jd_analyses WHERE jd_s3_key IS NOT NULL AND ja.id IS NULL
          pg_try_advisory_lock(42) for single-task exclusion
          Read JD text from S3 (jd_s3_key)
          If .json: extract description from raw_json

Step 5: Cloud Coordinator dispatches agent chain
  File: api/agents/cloud_coordinator/graph.py:41 (node_route_event)
  Action: event_type="new_jd" -> agent_chain=["jd_analyzer", "sponsorship_screener", "resume_matcher"]

  Step 5.1: JD Analyzer
    File: api/agents/jd_analyzer/graph.py:29 (node_strip_boilerplate)
    Action: invoke_model(HAIKU, system, sanitize_for_prompt(text)) -> strip boilerplate
    File: api/agents/jd_analyzer/graph.py:36 (node_extract_fields)
    Action: invoke_model(HAIKU, system, sanitize_for_prompt(text)) -> extract JSON fields
    File: api/agents/jd_analyzer/tools.py:100 (store_jd_analysis)
    Writes TO: jd_analyses table (INSERT ON CONFLICT (job_id) DO UPDATE)

  Step 5.2: Sponsorship Screener
    File: api/agents/sponsorship_screener/tools.py:40 (analyze_sponsorship)
    Action: invoke_model(HAIKU, SPONSORSHIP_SYSTEM_PROMPT, sanitize_for_prompt(jd_text))
    File: api/agents/sponsorship_screener/tools.py:73 (update_deal_breakers)
    Writes TO: jd_analyses.deal_breakers (UPDATE array_cat if unavailable)

  Step 5.3: Resume Matcher
    File: api/agents/cloud_coordinator/graph.py:100-148 (node_dispatch, resume_matcher branch)
    Action: SELECT resumes -> read each resume from S3

    Step 5.3.1: Recall
      File: api/agents/resume_matcher/tools.py:102 (recall)
      Action: retrieve_from_kb(query, top_k=50) -> Bedrock KB (Titan v2 embeddings)

    Step 5.3.2: Resolve job IDs
      File: api/agents/resume_matcher/tools.py:39 (resolve_job_ids)
      Action: Parse s3_uri -> extract jd_s3_key -> SELECT id FROM jobs WHERE jd_s3_key = ANY($1)

    Step 5.3.3: Structured filter
      File: api/agents/resume_matcher/tools.py:117 (structured_filter)
      Action: SELECT jd_analyses + jobs -> filter on deal_breakers (no_sponsorship) + experience_range

    Step 5.3.4: Rerank
      File: api/agents/resume_matcher/tools.py:209 (rerank)
      Action: For each candidate -> invoke_model(SONNET, system, sanitize_for_prompt(resume+jd))
              Parse JSON: {overall_fit_score, fit_category, gaps, strengths, reasoning}

    Step 5.3.5: Store reports
      File: api/agents/resume_matcher/tools.py:277 (store_reports)
      Writes TO: match_reports (INSERT ON CONFLICT (resume_id, job_id) DO UPDATE)

    Step 5.3.6: Update best score
      File: api/agents/cloud_coordinator/graph.py:143-148
      Writes TO: jobs.match_score (UPDATE with best score across all resumes)

Step 6: Dashboard displays results
  File: api/main.py:402 (GET /api/jobs/{job_id})
  Reads FROM: jobs, jd_analyses, match_reports (JOIN resumes), followup_recommendations
```

---

## 3. Agent I/O Contracts

| Agent | Reads FROM | Writes TO |
|---|---|---|
| **Email Classifier** (local) | Gmail API (emails), ChromaDB `email_classifications` (few-shot), ONNX embedder, Ollama Phi-3 | ChromaDB `email_classifications` (auto-store), `labeled_emails` table, `labeling_queue` table |
| **Stage Classifier** (local) | ChromaDB `stage_classifications` (few-shot), ONNX embedder, Ollama Phi-3, `jobs` table (lookup) | ChromaDB `stage_classifications` (auto-store), `labeled_emails` table, `labeling_queue` table (update guessed_stage), cloud `/api/ingest/status` via pipeline |
| **Deadline Tracker** (local) | Ollama Phi-3 (date extraction from email body) | Cloud `/api/ingest/status` via pipeline (with deadline field) |
| **Recommendation Parser** (local) | Ollama Phi-3 (entity extraction from email body) | Cloud `/api/ingest/recommendation` via pipeline |
| **Follow-up Advisor** (local) | `jobs` table (stale jobs query), Ollama Phi-3 (urgency assessment) | `followup_recommendations` table (local DB), cloud `/api/ingest/followup` via pipeline |
| **JD Ingestion Agent** (ECS) | SQS queue (messages), external URLs (HTTP GET), adapter APIs (Simplify, The Muse, Greenhouse, Lever, Ashby, HN Hiring), Bedrock Haiku (sponsorship screening) | S3 `jds/` prefix (.json files), RDS `jobs` table (INSERT/upsert), RDS `jd_analyses` table, RDS `match_reports` table |
| **Cloud Coordinator** (ECS) | RDS `jobs` table (unanalyzed jobs), S3 (JD text), `resumes` table (list) | RDS `orchestration_runs` table (create/update), delegates to child agents |
| **JD Analyzer** (ECS) | Bedrock Haiku (boilerplate strip + field extraction), asyncpg conn (passed in) | RDS `jd_analyses` table (INSERT ON CONFLICT DO UPDATE) |
| **Sponsorship Screener** (ECS) | Bedrock Haiku (sponsorship signal detection), asyncpg conn (passed in) | RDS `jd_analyses.deal_breakers` (UPDATE array_cat) |
| **Resume Matcher** (ECS) | Bedrock KB/Titan v2 (recall), RDS `jd_analyses` (targeted recall + filter), RDS `jobs` (resolve IDs, filter status), Bedrock Sonnet (rerank) | RDS `match_reports` table (INSERT ON CONFLICT DO UPDATE) |
| **Application Chat** (ECS) | RDS `jd_analyses` (context), RDS `match_reports` + `resumes` (context), RDS `answer_memory` (history), RDS `jobs` (company/role), Bedrock KB (retrieval), Bedrock Sonnet (answer) | RDS `answer_memory` table (INSERT) |

---

## 4. Schema Access Map

### Writers (INSERT / UPDATE / DELETE)

| Table | Writers (file path : line) |
|---|---|
| **jobs** | `api/agents/jd_ingestion/` (INSERT ON CONFLICT jd_s3_key DO NOTHING), `api/main.py:471` (UPDATE status), `api/main.py:625` (UPDATE status via ingest), `api/main.py:651` (INSERT ON CONFLICT company,role,source DO NOTHING), `api/main.py:862` (DELETE via TRUNCATE cascade), `api/agents/cloud_coordinator/graph.py:144` (UPDATE match_score), `api/agents/cloud_coordinator/graph.py:159` (UPDATE status), `api/agents/cloud_coordinator/graph.py:169` (INSERT ON CONFLICT DO NOTHING) |
| **labeled_emails** | `local/agents/email_classifier/tools.py:159` (INSERT ON CONFLICT email_id DO NOTHING), `local/agents/stage_classifier/tools.py:165` (INSERT ON CONFLICT email_id DO UPDATE stage), `api/main.py:521` (INSERT ON CONFLICT email_id DO UPDATE -- resolve queue) |
| **labeling_queue** | `local/agents/email_classifier/tools.py:193` (INSERT ON CONFLICT email_id DO NOTHING), `local/agents/stage_classifier/tools.py:184` (UPDATE guessed_stage), `api/main.py:515` (UPDATE resolved=TRUE), `local/pipeline/allowlist.py:198` (INSERT ON CONFLICT DO NOTHING -- unknown company queue) |
| **embedding_cache** | No Python writers found (table exists for future use) |
| **config** | `api/main.py:792` (INSERT ON CONFLICT key DO UPDATE -- blocklist_companies), `api/main.py:799` (INSERT ON CONFLICT key DO UPDATE -- blocklist_titles) |
| **jd_analyses** | `api/agents/jd_analyzer/tools.py:105` (INSERT ON CONFLICT job_id DO UPDATE), `api/agents/sponsorship_screener/tools.py:87` (UPDATE deal_breakers array_cat) |
| **resumes** | `local/resume_service.py:169` (INSERT RETURNING), `local/resume_service.py:271` (INSERT RETURNING -- legacy), `local/resume_service.py:293` (DELETE), `api/main.py:697` (INSERT ON CONFLICT DO NOTHING) |
| **match_reports** | `api/agents/resume_matcher/tools.py:290` (INSERT ON CONFLICT job_id,resume_id DO UPDATE) |
| **followup_recommendations** | `local/agents/followup_advisor/tools.py:110` (INSERT), `api/main.py:678` (INSERT via ingest), `api/main.py:571` (UPDATE acted_on=TRUE) |
| **orchestration_runs** | `local/agents/shared/tracking.py:75` (INSERT), `local/agents/shared/tracking.py:103` (UPDATE status/results), `api/agents/cloud_coordinator/tools.py:17` (INSERT), `api/agents/cloud_coordinator/tools.py:37` (UPDATE status/results) |
| **answer_memory** | `api/agents/application_chat/tools.py:117` (INSERT) |
| **deadlines** | `api/main.py:632` (INSERT via ingest/status with deadline) |
| **pipeline_metrics** | `local/pipeline/validator.py:93` (INSERT) |

### Readers (SELECT)

| Table | Readers (file path : line) |
|---|---|
| **jobs** | `api/main.py:344-398` (list_jobs with filters), `api/main.py:406` (get_job), `api/main.py:618` (verify exists for ingest), `api/main.py:674` (verify exists for followup ingest), `api/main.py:728` (chat job lookup), `api/main.py:145-150` (poll unanalyzed), `api/main.py:862` (TRUNCATE), `api/agents/cloud_coordinator/graph.py:103` (SELECT resumes -- actually reads resumes), `api/agents/application_chat/tools.py:86` (SELECT company,role), `api/agents/resume_matcher/tools.py:76` (SELECT id,jd_s3_key for resolve), `api/agents/resume_matcher/tools.py:137` (SELECT jd_analyses+jobs for filter), `local/agents/stage_classifier/tools.py:206` (SELECT id for job lookup), `local/agents/followup_advisor/tools.py:58` (SELECT stale jobs), `local/pipeline/validator.py:37` (SELECT EXISTS for status), `local/pipeline/validator.py:79` (SELECT EXISTS for followup), `local/pipeline/allowlist.py:174` (SELECT DISTINCT company) |
| **labeled_emails** | No explicit SELECT found (read indirectly via ChromaDB sync) |
| **labeling_queue** | `api/main.py:488` (list unresolved), `api/main.py:508` (get by id for resolve) |
| **embedding_cache** | No readers found |
| **config** | `api/main.py:132` (analysis_polling_enabled flag), `api/main.py:347-356` (blocklist_companies, blocklist_titles), `api/main.py:773-784` (GET blocklist), `local/pipeline/allowlist.py:166` (tech_company_allowlist) |
| **jd_analyses** | `api/main.py:410` (get_job detail), `api/agents/resume_matcher/graph.py:44` (targeted recall -- raw_jd_text), `api/agents/resume_matcher/tools.py:137` (structured_filter -- deal_breakers, experience_range), `api/agents/application_chat/tools.py:31` (context -- all fields) |
| **resumes** | `api/main.py:587` (list_resumes), `api/agents/cloud_coordinator/graph.py:103` (SELECT for matching), `api/agents/application_chat/tools.py:49` (match context -- via join), `local/resume_service.py:204` (SELECT s3_key), `local/resume_service.py:289` (SELECT s3_key for delete), `local/resume_service.py:324` (list_resumes) |
| **match_reports** | `api/main.py:363-377` (list_jobs with match sort), `api/main.py:412-419` (get_job detail), `api/agents/application_chat/tools.py:48` (chat context) |
| **followup_recommendations** | `api/main.py:423` (get_job detail), `api/main.py:547-563` (list_followups) |
| **orchestration_runs** | `api/main.py:601` (list_runs) |
| **answer_memory** | `api/agents/application_chat/tools.py:69` (previous Q&A for context) |
| **deadlines** | `api/main.py:818-826` (list_deadlines) |
| **pipeline_metrics** | `api/main.py:833-840` (ops_metrics aggregation) |

### Foreign Key Cascade Chain

```
DELETE FROM jobs WHERE id = X
  CASCADE -> jd_analyses      (jd_analyses.job_id REFERENCES jobs ON DELETE CASCADE)
  CASCADE -> match_reports     (match_reports.job_id REFERENCES jobs ON DELETE CASCADE)
  CASCADE -> followup_recommendations  (followup_recommendations.job_id REFERENCES jobs ON DELETE CASCADE)
  CASCADE -> deadlines         (deadlines.job_id REFERENCES jobs ON DELETE CASCADE)

DELETE FROM resumes WHERE id = X
  CASCADE -> match_reports     (match_reports.resume_id REFERENCES resumes ON DELETE CASCADE)

DELETE FROM jd_analyses WHERE id = X
  SET NULL -> match_reports.jd_analysis_id  (ON DELETE SET NULL)
```

**Admin reset** (`api/main.py:846-864`): TRUNCATE match_reports, jd_analyses, followup_recommendations, orchestration_runs, pipeline_metrics, deadlines, answer_memory CASCADE; then DELETE FROM jobs. Preserves resumes and config.

---

## 5. Module Import Dependencies

### local/agents/shared/ -- 8 Modules

#### db.py
- **Exports:** `get_pool()`, `close_pool()`, `acquire()` (async context manager)
- **External deps:** `asyncpg`, `os` (DATABASE_URL env var)
- **Imported by:**
  - `local/main.py:24` (get_pool, close_pool)
  - `local/agents/email_classifier/tools.py:13` (acquire)
  - `local/agents/stage_classifier/tools.py:16` (acquire)
  - `local/agents/followup_advisor/tools.py:15` (acquire)
  - `local/agents/shared/tracking.py:23` (acquire)
  - `local/pipeline/validator.py:8` (acquire)
  - `local/pipeline/allowlist.py:12` (acquire)
  - `local/resume_service.py:29` (acquire)

#### llm.py
- **Exports:** `llm_generate()`, `sanitize_for_prompt()`, `OLLAMA_BASE_URL`, `MODEL`
- **External deps:** `httpx`, `os` (OLLAMA_BASE_URL env var), `re`
- **Imported by:**
  - `local/agents/email_classifier/tools.py:11` (llm_generate, sanitize_for_prompt)
  - `local/agents/stage_classifier/tools.py:14` (llm_generate, sanitize_for_prompt)
  - `local/agents/deadline_tracker/tools.py:12` (llm_generate, sanitize_for_prompt)
  - `local/agents/recommendation_parser/tools.py:12` (llm_generate, sanitize_for_prompt)
  - `local/agents/followup_advisor/tools.py:14` (llm_generate, sanitize_for_prompt)

#### memory.py
- **Exports:** `get_chroma_client()`, `get_email_collection()`, `get_stage_collection()`
- **External deps:** `chromadb` (HttpClient), `os` (CHROMADB_HOST, CHROMADB_PORT env vars)
- **Imported by:**
  - `local/agents/email_classifier/tools.py:12` (get_email_collection)
  - `local/agents/stage_classifier/tools.py:15` (get_stage_collection)

#### embedder.py
- **Exports:** `LocalEmbedder` (class with `embed()`, `embed_batch()`)
- **External deps:** `onnxruntime`, `tokenizers`, `numpy`, `os` (ONNX_MODEL_PATH env var)
- **Imported by:**
  - `local/agents/email_classifier/tools.py:10` (LocalEmbedder)
  - `local/agents/stage_classifier/tools.py:13` (LocalEmbedder)

#### redactor.py
- **Exports:** `PiiRedactor` (class with `redact()`, `contains_pii()`), `enforce_pii_boundary()`
- **External deps:** `presidio_analyzer`, `presidio_anonymizer`
- **Imported by:**
  - `local/agents/email_classifier/tools.py:14` (enforce_pii_boundary)
  - `local/agents/stage_classifier/tools.py:17` (enforce_pii_boundary)
  - `local/agents/followup_advisor/tools.py:16` (enforce_pii_boundary)
  - `local/agents/shared/tracking.py:24` (enforce_pii_boundary)
  - `local/pipeline/validator.py:9` (PiiRedactor)
  - `local/resume_service.py:28` (PiiRedactor)

#### secrets.py
- **Exports:** `get_secret()`
- **External deps:** `os`, `json`, `boto3` (lazy import for production)
- **Imported by:** Not directly imported by agents (secrets flow through env vars in Docker Compose). Used conceptually for production Secrets Manager access.

#### tracking.py
- **Exports:** `track_agent_run()` (context manager), `create_orchestration_run()`, `update_orchestration_run()`
- **External deps:** `mlflow`, `uuid`, `json`, `time`
- **Internal deps:** `local/agents/shared/db.acquire`, `local/agents/shared/redactor.enforce_pii_boundary`
- **Imported by:**
  - `local/main.py:19-22` (create_orchestration_run, update_orchestration_run)
  - `local/agents/email_classifier/graph.py:24` (track_agent_run)
  - `local/agents/stage_classifier/graph.py:21` (track_agent_run)
  - `local/agents/deadline_tracker/graph.py:17` (track_agent_run)
  - `local/agents/recommendation_parser/graph.py:16` (track_agent_run)
  - `local/agents/followup_advisor/graph.py:14` (track_agent_run)

#### dispatch.py
- **Exports:** `dispatch_status_update()`, `dispatch_recommendation()`
- **Internal deps:** Lazy imports of `stage_classifier.graph`, `deadline_tracker.graph`, `recommendation_parser.graph`
- **Imported by:**
  - `local/main.py:18` (dispatch_status_update, dispatch_recommendation)

### api/agents/bedrock_client.py

- **Exports:** `invoke_model()`, `retrieve_from_kb()`, `sanitize_for_prompt()`, `HAIKU`, `SONNET`, `BEDROCK_KB_ID`
- **External deps:** `boto3` (bedrock-runtime, bedrock-agent-runtime), `json`, `re`, `os` (AWS_DEFAULT_REGION, BEDROCK_KB_ID env vars)
- **Imported by:**
  - `api/agents/jd_analyzer/tools.py:12` (HAIKU, invoke_model, sanitize_for_prompt)
  - `api/agents/sponsorship_screener/tools.py:13` (HAIKU, invoke_model, sanitize_for_prompt)
  - `api/agents/resume_matcher/tools.py:12` (SONNET, invoke_model, retrieve_from_kb, sanitize_for_prompt)
  - `api/agents/application_chat/tools.py:9` (SONNET, invoke_model, retrieve_from_kb, sanitize_for_prompt)

### local/pipeline/ -- Validation Chain

```
local/pipeline/schemas.py
  Exports: StatusPayload, RecommendationPayload, FollowupPayload
  Imported by: local/pipeline/validator.py:11-14

local/pipeline/validator.py
  Exports: validate_status(), validate_recommendation(), validate_followup()
  Depends on: schemas.py, local/agents/shared/db.acquire, local/agents/shared/redactor.PiiRedactor, local/pipeline/allowlist
  Imported by:
    - local/agents/stage_classifier/tools.py:18
    - local/agents/deadline_tracker/tools.py:13
    - local/agents/recommendation_parser/tools.py:13
    - local/agents/followup_advisor/tools.py:17

local/pipeline/sender.py
  Exports: send_to_cloud(), send_to_cloud_delete()
  Depends on: httpx, os (CLOUD_API_URL, INGEST_HMAC_KEY env vars)
  Imported by:
    - local/agents/stage_classifier/tools.py:19
    - local/agents/deadline_tracker/tools.py:14
    - local/agents/recommendation_parser/tools.py:14
    - local/agents/followup_advisor/tools.py:18
    - local/resume_service.py:181 (lazy import)
    - local/resume_service.py:311 (lazy import, delete)

local/pipeline/allowlist.py
  Exports: is_company_allowed(), queue_unknown_company(), invalidate_cache()
  Depends on: local/agents/shared/db.acquire
  Imported by: local/pipeline/validator.py:10
```

### api/iam_auth.py

- **Exports:** `require_hmac_auth()` (FastAPI dependency)
- **External deps:** `hmac`, `hashlib`, `os` (INGEST_HMAC_KEY), `time`, `fastapi`
- **Imported by:** `api/main.py:37`

---

## 6. Infrastructure Dependencies

### Terraform Resource Dependency Chain

```
VPC (module.vpc)
  |-- Public subnets [0,1] (10.0.0.0/20, 10.0.16.0/20)
  |   |-- Internet Gateway (auto-created by VPC module)
  |   |-- ALB (aws_lb.app) -- requires 2 AZs
  |   |   |-- Target Group (aws_lb_target_group.app) -- health check /health:8080
  |   |   |-- HTTPS Listener (conditional on acm_certificate_arn)
  |   |   |-- HTTP Listener (redirect or forward)
  |   |-- NAT Instance (aws_instance.nat) -- t3.micro, source_dest_check=false
  |
  |-- Private subnets [0,1] (10.0.128.0/20, 10.0.144.0/20)
      |-- [0] private-fetch: route 0.0.0.0/0 -> NAT instance ENI
      |   |-- ECS Fargate Service (aws_ecs_service.app) -- 512 CPU, 1024 MiB
      |
      |-- [1] private-data: NO internet route
          |-- RDS (referenced via SG, not managed by this Terraform)
          |-- VPC Endpoints:
              |-- Secrets Manager Interface (aws_vpc_endpoint.secretsmanager)
              |-- CloudWatch Logs Interface (aws_vpc_endpoint.logs)
              |-- S3 Gateway (created in console, referenced via prefix list)
```

### VPC Networking: Subnet Routing

| Subnet | CIDR | Routes To | Resources |
|---|---|---|---|
| public-a | 10.0.0.0/20 | 0.0.0.0/0 -> IGW | ALB, NAT Instance |
| public-b | 10.0.16.0/20 | 0.0.0.0/0 -> IGW | ALB (2nd AZ for HA) |
| private-fetch | 10.0.128.0/20 | 0.0.0.0/0 -> NAT Instance ENI | ECS Fargate (512 CPU, 1024 MiB) |
| private-data | 10.0.144.0/20 | No internet route (S3 via Gateway Endpoint, SM/CW via Interface Endpoints) | RDS |

### VPC Endpoints

| Endpoint | Type | Subnet | Purpose | Cost |
|---|---|---|---|---|
| S3 | Gateway | All route tables | ECS + RDS subnet read/write S3 | Free |
| Secrets Manager | Interface | private-data [1] | RDS subnet services fetch DB creds | ~$7.20/mo |
| CloudWatch Logs | Interface | private-data [1] | RDS subnet ships logs | ~$7.20/mo |

### Security Group Cross-References (6 SGs)

| SG Name | Inbound From | Outbound To |
|---|---|---|
| **alb-sg** | 0.0.0.0/0 port 80 (HTTP), 0.0.0.0/0 port 443 (HTTPS) | ecs-sg (all traffic) |
| **ecs-sg** | alb-sg port 8080 (TCP) | 0.0.0.0/0 (all -- RDS, S3, SQS, Bedrock via NAT) |
| **rds-sg** | ecs-sg port 5432 | (none specified) |
| **nat-sg** | private-fetch CIDR 10.0.128.0/20 (all traffic) | 0.0.0.0/0 (all -- forwards to IGW) |
| **vpce-sg** | ecs-sg port 443 | (none specified) |

**Key isolation:** ECS runs in private-fetch subnet with NAT route for external API access. RDS is in private-data subnet with no internet route. SQS consume permissions are on the ECS IAM role.

---

## 7. Configuration Dependencies

### Environment Variables Per Service

#### app (Docker Compose -- local agents + resume service)
| Variable | Source | Used By |
|---|---|---|
| `DATABASE_URL` | docker-compose.yml:92 | `local/agents/shared/db.py:23` |
| `OLLAMA_BASE_URL` | docker-compose.yml:93 | `local/agents/shared/llm.py:15` |
| `CHROMADB_HOST` | docker-compose.yml:94 | `local/agents/shared/memory.py:27` |
| `CHROMADB_PORT` | docker-compose.yml:95 | `local/agents/shared/memory.py:28` |
| `MLFLOW_TRACKING_URI` | docker-compose.yml:96 | `local/agents/shared/tracking.py:31` |
| `ONNX_MODEL_PATH` | docker-compose.yml:97 | `local/agents/shared/embedder.py:23` |
| `GMAIL_CREDENTIALS_PATH` | docker-compose.yml:98 | `local/gmail/auth.py:34` |
| `GMAIL_TOKEN_PATH` | docker-compose.yml:99 | `local/gmail/auth.py:32` |
| `CLOUD_API_URL` | docker-compose.yml:100 | `local/pipeline/sender.py:14` |
| `INGEST_HMAC_KEY` | docker-compose.yml:101 | `local/pipeline/sender.py:15` |
| `S3_BUCKET` | docker-compose.yml:102 | `local/resume_service.py:35` |
| `AWS_DEFAULT_REGION` | docker-compose.yml:103 | `local/resume_service.py:36` |

#### debug (Docker Compose -- debug dashboard)
| Variable | Source | Used By |
|---|---|---|
| `CLOUD_API_URL` | docker-compose.yml:132 | Cloud API health checks |
| `APP_PASSWORD` | docker-compose.yml:133 | Debug dashboard auth |
| `AWS_DEFAULT_REGION` | docker-compose.yml:134 | AWS resource status checks |
| `S3_BUCKET` | docker-compose.yml:135 | S3 object listing |
| `SQS_QUEUE_NAME` | docker-compose.yml:136 | SQS depth monitoring |
| `BEDROCK_KB_ID` | docker-compose.yml:137 | KB status check |
| `DATABASE_URL` | docker-compose.yml:140 | Local DB queries |
| Plus all OLLAMA, CHROMADB, MLFLOW, ONNX, GMAIL vars | docker-compose.yml:141-147 | Local health checks |

#### ECS Task (AWS -- Cloud API + JD Ingestion Agent)
| Variable | Source | Used By |
|---|---|---|
| `PORT` | infra/ecs.tf:152 (hardcoded "8080") | uvicorn startup |
| `BEDROCK_KB_ID` | infra/ecs.tf:153 (var.bedrock_kb_id) | `api/agents/bedrock_client.py:20` |
| `SQS_QUEUE_NAME` | infra/ecs.tf:154 (aws_sqs_queue.jd_scrape.name) | `api/main.py:46` |
| `AWS_DEFAULT_REGION` | infra/ecs.tf:155 (var.aws_region) | `api/main.py:47`, `api/agents/bedrock_client.py:21` |
| `S3_BUCKET` | infra/ecs.tf:156 (data.aws_s3_bucket.jd_storage.id) | `api/main.py:100` |
| `SECURE_COOKIES` | infra/ecs.tf:157 (conditional on ACM cert) | `api/main.py:90` |
| `DATABASE_URL` | infra/ecs.tf:161 (secret from Secrets Manager) | `api/main.py:92` |
| `JWT_SECRET` | infra/ecs.tf:165 (secret from Secrets Manager) | `api/main.py:86` |
| `INGEST_HMAC_KEY` | infra/ecs.tf:169 (secret from Secrets Manager) | `api/iam_auth.py:14` |
| `APP_PASSWORD` | infra/ecs.tf:173 (secret from Secrets Manager) | `api/main.py:289` |

### Secrets Flow

```
Local Development:
  .env file (gitignored)
    -> docker-compose.yml ${VAR:-default} interpolation
    -> container environment variables
    -> os.environ.get() in Python code

Production (AWS):
  Secrets Manager: job-search-platform/production
    Contains: DATABASE_URL, JWT_SECRET, INGEST_HMAC_KEY, APP_PASSWORD, DB_HOST, DB_NAME, DB_USER, DB_PASSWORD
    |
    |-- ECS: injected as env vars at task launch (ecs.tf:160-176, via execution role)
    |-- Local sync: INGEST_HMAC_KEY read from .env (sender.py:15)
```

### Docker Compose Wiring

**Ports:**
| Service | Host:Container | Protocol |
|---|---|---|
| postgres | 5433:5432 | PostgreSQL |
| ollama | 11434:11434 | HTTP (Ollama API) |
| chromadb | 8000:8000 | HTTP (ChromaDB API) |
| mlflow | 5001:5000 | HTTP (MLflow UI) |
| app | 8001:8001 | HTTP (Resume service) |
| debug | 8002:8002 | HTTP (Debug dashboard) |

**Volumes:**
| Volume | Type | Mount Path | Purpose |
|---|---|---|---|
| pgdata | named | /var/lib/postgresql/data | PostgreSQL data persistence |
| ollama_data | named | /root/.ollama | Ollama model cache |
| chroma_data | named | /chroma/chroma | ChromaDB vector persistence |
| mlflow_data | named | /mlflow | MLflow experiments + artifacts |
| ./local | bind | /app/local | Live code reload (app + debug) |
| ./scripts | bind (ro) | /app/scripts | Test scripts (app only) |
| ./credentials | bind (ro) | /app/credentials | Gmail OAuth creds (app + debug) |
| ~/.aws | bind (ro) | /aws-creds | AWS credentials for S3/SQS (app + debug) |
| ./api | bind (ro) | /app/api | API code inspection (debug only) |
| ./infra/schema.sql | bind (ro) | /app/infra/schema.sql | Schema reference (debug only) |
| ./infra/schema.sql | bind (ro) | /docker-entrypoint-initdb.d/01_schema.sql | DB initialization (postgres) |

**Healthchecks:**
| Service | Check | Interval | Retries |
|---|---|---|---|
| postgres | `pg_isready -U jobsearch` | 5s | 5 |
| ollama | `ollama list` | 10s (start_period 15s) | 5 |
| chromadb | (none -- service_started only) | -- | -- |

**depends_on:**
| Service | Depends On | Condition |
|---|---|---|
| app | postgres | service_healthy |
| app | ollama | service_healthy |
| app | chromadb | service_started |
| debug | postgres | service_healthy |
