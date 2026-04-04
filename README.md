# Job Search Intelligence Platform

> Privacy-first multi-agent system that automates job discovery, matching, and application tracking across 8 TOS-compliant sources — enforcing PII boundaries at the network level, not just in code.

## Architecture

```
┌─────────────────────────────────────────────┐     ┌──────────────────────────────────────────────────┐
│           LOCAL (Docker, on-device)          │     │                AWS CLOUD (no PII)                │
│                                              │     │                                                  │
│  Gmail ──► Email Classifier (Phi-3)          │     │  ┌─ ECS Fargate (FastAPI) ──────────────────┐    │
│            Stage Classifier (Phi-3)          │     │  │  Cloud Coordinator (Haiku)               │    │
│            Recommendation Parser (Phi-3)     │     │  │  JD Analyzer (Haiku)                     │    │
│            Deadline Tracker (Phi-3)          │     │  │  Sponsorship Screener (Haiku)             │    │
│            Followup Advisor (Phi-3)          │     │  │  Resume Matcher (Sonnet + Titan v2)       │    │
│                                              │     │  │  Application Chat (Sonnet)                │    │
│                                              │     │  │  Analysis Poller (background task)        │    │
│  ChromaDB    ONNX Embedder    Presidio       │     │  └──────────────────────────────────────────-┘    │
│                                              │     │                                                  │
│  ┌─ Validation Pipeline ──────────────────┐  │     │  JD Ingestion Agent ── NAT ──► Internet      │
│  │  Format check → Presidio PII → Entity  │──┼────►│    (fetch→screen→store→persist→analyze)                │
│  │  match → HMAC sign → send              │  │     │  Bedrock KB (Titan v2, 1024-dim)                 │
│  └────────────────────────────────────────┘  │     │  S3 · SQS · Secrets Manager                      │
│                                              │     │                                                  │
│  Only outbound: Gmail (readonly) + cloud API │     │  Dashboard (React) ◄── JWT auth                  │
└─────────────────────────────────────────────┘     └──────────────────────────────────────────────────┘
```

## System Design

### Architecture Patterns

| Concept | Implementation |
|---------|---------------|
| **Edge-Cloud Hybrid** | PII-touching agents (email, resume) run on-device in Docker; non-PII agents (JD analysis, matching) run on AWS. Network boundary — not just a function call — separates the two. |
| **Multi-Agent Orchestration** | 10 LangGraph `StateGraph` agents with typed state (`TypedDict`), async nodes, conditional edges, and `.compile()` → `.ainvoke()` execution. Cloud Coordinator routes events to agent chains; local agents are invoked directly from the scheduler. |
| **Event-Driven Architecture** | SQS decouples job discovery from processing (300s visibility, DLQ after 3 retries). EventBridge schedules daily adapter scrapes (the_muse), daily Simplify, and monthly HN Who's Hiring via cron → SQS. ECS polls SQS and routes messages to the JD Ingestion Agent (LangGraph with conditional routing): fetch → screen sponsorship → store S3 + persist RDS → analyze → match. Sponsorship screening happens before S3 storage to prevent KB pollution. |
| **Pluggable Adapter Pattern** | 8 source adapters implement a `SourceAdapter` base class with `fetch() → List[NormalizedJob]`. A registry maps source names to adapters at runtime — new sources require zero changes to the ingestion agent. Only TOS-compliant, free, public APIs are enabled; non-compliant adapters are blacklisted in the registry with documented reasons. |
| **Coordinator / Router** | Cloud Coordinator dispatches by event type: `new_jd → [jd_analyzer, sponsorship_screener, resume_matcher]`, `chat → [application_chat]`. Local scheduler chains Email Classifier → Stage Classifier → Deadline Tracker via dispatch helpers. |

### Data Pipeline & Storage

| Concept | Implementation |
|---------|---------------|
| **Four-Stage RAG Pipeline** | *Recall*: Bedrock KB vector search (Titan v2, 1024-dim) retrieves top-50 JDs. *Resolve*: batch SQL lookup maps `s3_uri` → `jd_s3_key` → `job_id`, dropping orphaned KB docs. *Filter*: SQL removes deal-breakers (sponsorship, clearance, experience range) by job_id. *Rerank*: Claude Sonnet scores each candidate with structured JSON output. Supports targeted mode (specific job_id) for new_jd events. |
| **Dual Vector Stores** | ChromaDB (local) stores email embeddings with PII for few-shot retrieval. Bedrock KB (cloud) indexes redacted JD texts in managed OpenSearch Serverless. Privacy boundary preserved in the vector layer. |
| **Content-Addressable Storage** | `SHA-256(job_content)` → S3 key `jds/{hash}.json`. S3 HeadObject dedup skips already-stored content before writing. Per-adapter watermark filtering (`since` param) prevents re-fetching old jobs. `UNIQUE` constraints on `jd_s3_key`, `simplify_id`, `ats_url`, `(company, role, source)` enforce dedup at the database level. |
| **Idempotent Writes** | All inserts use `ON CONFLICT DO NOTHING` or `DO UPDATE`. JD Ingestion Agent, ingestion endpoints, and agent writes are safe to retry without creating duplicates. |
| **Flexible Schema** | `raw_json JSONB` stores heterogeneous source metadata. `experience_range INT4RANGE` enables range-contains queries. `confidence_scores JSONB` per field. Array columns (`required_skills[]`, `tech_stack[]`) for structured extraction. |
| **Embedding Cache** | `embedding_cache` table keyed by `SHA-256(input_text)`. Avoids recomputing embeddings for identical content across agent runs. |

### Security & Privacy (Defense in Depth)

| Concept | Implementation |
|---------|---------------|
| **Privacy-by-Design** | PII boundary enforced at the VPC subnet level. Local Docker container has only two outbound paths: Gmail API (readonly) and cloud ingestion endpoint. Email bodies, names, and contact info never enter AWS. |
| **VPC Privilege Separation** | Two private subnets with different route tables. `private-fetch` (10.0.128.0/20): NAT route to internet for ECS tasks including JD Ingestion Agent. `private-data` (10.0.144.0/20): no internet route, RDS + S3 via VPC endpoints only. |
| **SSRF Protection** | JD Ingestion Agent validates every URL: DNS resolution → reject private/loopback/link-local IPs → reject non-HTTP schemes. Custom `RedirectHandler` re-validates each redirect hop. IMDSv2 enforced (`http_tokens = "required"`) on NAT instance. |
| **Two-Tier Authentication** | Dashboard: JWT in HttpOnly/Secure/SameSite=Strict cookies (8h expiry, rate-limited login). Service-to-service: HMAC-SHA256 over `{timestamp}.{payload}` with ±5 min drift window for replay protection. |
| **Prompt Injection Defense** | `sanitize_for_prompt()` strips system-prompt overrides, instruction injections, identity manipulation, code blocks, and XML tag injection from all LLM inputs. Length-capped (8K local, 16K cloud) to prevent context stuffing. Applied on both Ollama and Bedrock paths. |
| **PII Redaction** | Microsoft Presidio (spaCy `en_core_web_lg` NER) detects PERSON, EMAIL, PHONE, SSN, CREDIT_CARD, LOCATION, PASSPORT, IP_ADDRESS. Resumes are redacted before S3 upload. `enforce_pii_boundary()` runs before every cloud-bound write. |

### Infrastructure & Cost Optimization

| Concept | Implementation |
|---------|---------------|
| **Infrastructure as Code** | Terraform with `terraform-aws-modules/vpc`. Security groups, IAM roles, VPC endpoints, ECS cluster, EventBridge rules, Bedrock KB — all version-controlled and reproducible. |
| **ECS Fargate** | ECS Fargate (512 CPU, 1024 MiB) runs the API and JD Ingestion Agent — no instance management, pay-per-second. JD Ingestion Agent polls SQS and handles the full pipeline (fetch, sponsorship screening, S3 storage, RDS persistence, analysis, matching) as a single LangGraph agent with conditional routing. |
| **NAT Instance over NAT Gateway** | `t3.nano` NAT instance (~$3.80/mo) replaces managed NAT Gateway (~$33/mo). Source/dest check disabled, CloudWatch auto-recovery for HA. Acceptable trade-off for a single-user system. |
| **VPC Endpoints** | S3 Gateway Endpoint (free) and Secrets Manager Interface Endpoint give `private-data` subnet access to AWS services without internet routing. |
| **CI/CD with OIDC Federation** | GitHub Actions: test → build (`linux/amd64`) → push to ECR → ECS force-new-deployment. OIDC federation with `aws-actions/configure-aws-credentials` — no static AWS keys stored in CI. |
| **Container Hardening** | Multi-stage Docker build on `python:3.13-slim`. Non-root `appuser`, read-only root filesystem, 64 MiB `tmpfs` at `/tmp`. ALB drops invalid HTTP headers (request smuggling prevention). |
| **Deployment Audit** | 5-phase checklist: local verification, infrastructure validation, CI/CD checks, post-deploy health, and end-to-end smoke tests. 15-point security audit covers secrets, SQL injection, prompt injection, PII boundaries, SSRF, auth, IAM, and background task safety. |

### ML / AI Patterns

| Concept | Implementation |
|---------|---------------|
| **Few-Shot Learning with Confidence Gating** | Email/Stage Classifiers retrieve 5 similar labeled examples from ChromaDB, inject as few-shot context. Confidence ≥ 0.85 → auto-store label (grows training set). Confidence < 0.85 → queue for human review. System bootstraps from zero data. |
| **Human-in-the-Loop** | `labeling_queue` table surfaces low-confidence classifications to the dashboard. User corrections feed back into ChromaDB and `labeled_emails`, improving future classifications. |
| **Dual Model Serving** | Local: ONNX Runtime (`all-MiniLM-L6-v2`, CPU) for embeddings + Ollama Phi-3 Mini (4-bit quantized) for classification. Cloud: Bedrock Titan v2 for embeddings + Claude Haiku 4.5 (parsing) / Sonnet 4.6 (reasoning) via cross-region inference profiles. Model choice follows the PII boundary. |
| **Cold Start Optimization** | Lazy-loaded spaCy models, module-level Boto3 client caching, asyncpg connection pool (2-10 conns). |
| **Experiment Tracking** | MLflow logs per-agent metrics (duration, confidence, accuracy) and parameters (email_id, event_type, run_id). Graceful degradation — agents run even if MLflow is unavailable. |

### Observability

| Concept | Implementation |
|---------|---------------|
| **Orchestration Audit Trail** | `orchestration_runs` table logs every agent invocation: `run_id` (UUID), `event_type`, `agent_chain[]`, per-agent `results` (JSONB), `status`, timestamps. |
| **Pipeline Metrics** | `pipeline_metrics` table: flexible time-series (`source`, `metric_name`, `metric_value`, `recorded_at`). Tracks emails processed, validations failed, JDs analyzed. |
| **Health Checks** | ALB → `/health` (30s interval, 2 healthy / 3 unhealthy threshold). Docker Compose services use readiness probes. CloudWatch Logs with 14-day retention for ECS. |
| **Debug Dashboard** | Interactive architecture diagram at `/static/debug_dashboard.html`. Shows all ~30 components (local + cloud) as a clickable node graph with live health status (green/yellow/red). Includes schema sync validation, cross-boundary ingest monitoring, and Phoenix integration for LLM trace deep-dives. |

## Agent Inventory

### Cloud (Bedrock on ECS Fargate)

| Agent | Model | Purpose |
|-------|-------|---------|
| Cloud Coordinator | Claude Haiku 4.5 | Event routing + agent chaining |
| JD Analyzer | Claude Haiku 4.5 | Strip boilerplate, extract structured fields |
| Sponsorship Screener | Claude Haiku 4.5 | Nuanced sponsorship/clearance analysis |
| Resume Matcher | Claude Sonnet 4.6 + Titan v2 | Three-stage RAG: recall → filter → rerank |
| Application Chat | Claude Sonnet 4.6 | Contextual Q&A with answer memory |
| Analysis Poller | — | Background task: polls RDS for unanalyzed jobs, triggers Cloud Coordinator |

### Local (Ollama Phi-3 on Docker)

| Agent | Purpose |
|-------|---------|
| Email Classifier | 3-class classification with RAG few-shot |
| Stage Classifier | 8-stage application tracking from emails |
| Recommendation Parser | Extract {company, role} from email recommendations |
| Deadline Tracker | Extract concrete dates from emails |
| Followup Advisor | Daily stale job scan with LLM urgency scoring |

## Source Adapters (8 active, 2 blacklisted)

All active adapters use free, public, TOS-compliant APIs. Non-compliant sources are blacklisted in `adapter_registry.py`.

| Tier | Sources |
|------|---------|
| 1 (daily) | The Muse — EventBridge daily 6am UTC |
| 2 (daily) | Simplify — EventBridge daily 6am UTC (published GitHub JSON feed) |
| 3 (monthly) | HN Who's Hiring — EventBridge 1st of month 9am UTC (Algolia public API) |
| 4 (on-demand) | Greenhouse, Lever, Ashby — official public ATS board APIs, require company slug via SQS |
| 5 (email) | LinkedIn, Handshake, Lensa, Jobright — via email recommendation pipeline |

**Blacklisted** (removed from registry): RemoteOK (blocks automated access, HTTP 403), JSearch (third-party RapidAPI wrapper, unclear Google licensing). Adzuna and USAJobs adapters exist in code but are commented out (require API key registration).

## Tech Stack

Python 3.11 · LangGraph · Bedrock (Claude + Titan) · Ollama (Phi-3) · FastAPI · asyncpg · Terraform · Docker · ChromaDB · Presidio · ONNX Runtime · MLflow · GitHub Actions · SQS · EventBridge · S3 · ECS Fargate

## Getting Started

**Full setup guide:** See [SETUP.md](SETUP.md) for step-by-step instructions covering local development, Gmail OAuth, AWS infrastructure, and CI/CD.

**Quick start (local only):**
```bash
cp .env.example .env          # Edit with your values
docker compose up -d           # Starts all 6 services
pytest tests/ -v               # Run test suite
```

Ollama auto-pulls the Phi-3 model on first boot (2-5 min).
Gmail is optional — email agents are disabled if credentials aren't configured.

**Cloud deployment:** Requires AWS account setup (S3, ECR, Secrets Manager, Bedrock KB, model access). See [SETUP.md](SETUP.md) Step 3.

## License

MIT
