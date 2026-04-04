# DASHBOARD.md — Debug Dashboard System Map
**Last updated: 2026-04-02**

> This document maps the debug dashboard to the system it monitors.
> When system components change (new agent, renamed component, new table),
> the dashboard must be updated too. `tests/test_dashboard_sync.py` enforces this.

---

## Overview

The debug dashboard is a 30-component observability system that runs locally
(port 8002) and monitors both the local Docker stack and the cloud AWS infrastructure.

**Entry point:** `local/debug_dashboard.py` (FastAPI on port 8002)
**Topology:** `api/debug/topology.py` (30 nodes, 37 edges, 2 groups)
**Cloud health checks:** `api/debug/health_checks.py` (10 check functions)
**Local health checks:** `local/debug/local_checks.py` (9 check functions)
**Schema sync:** `api/debug/schema_sync.py` (runtime schema drift detection)
**Frontend:** `api/static/debug_dashboard.html`, `debug_app.js`, `debug_style.css`

---

## Dashboard API Endpoints

| Endpoint | Method | Returns |
|----------|--------|---------|
| `/api/debug/health` | GET | All 30 component statuses (runs all checks) |
| `/api/debug/topology` | GET | Topology graph (nodes, edges, groups) |
| `/api/debug/component/{id}` | GET | Single component: health + expected/actual + details |
| `/api/debug/summary` | GET | Aggregate: X green, Y yellow, Z red |
| `/api/debug/errors` | GET | Recent CloudWatch error logs |

---

## Component-to-System Mapping

### Local Components (14 nodes)

| Dashboard Node | System Component | Health Check | Source Files |
|---|---|---|---|
| `gmail` | Gmail API (readonly scope) | `check_gmail()` | `local/gmail/auth.py` |
| `apscheduler` | APScheduler (2h email, daily followup) | `check_scheduler()` | `local/main.py` |
| `email_classifier` | Email Classifier agent | `check_email_pipeline()` | `local/agents/email_classifier/` |
| `stage_classifier` | Stage Classifier agent | None (indirect) | `local/agents/stage_classifier/` |
| `deadline_tracker` | Deadline Tracker agent | None (indirect) | `local/agents/deadline_tracker/` |
| `recommendation_parser` | Recommendation Parser agent | None (indirect) | `local/agents/recommendation_parser/` |
| `followup_advisor` | Follow-up Advisor agent | None (indirect) | `local/agents/followup_advisor/` |
| `ollama` | Ollama LLM (Phi-3 Mini) | `check_ollama()` | Docker Compose `ollama` service |
| `chromadb` | ChromaDB vector store | `check_chromadb()` | Docker Compose `chromadb` service |
| `local_postgres` | Local PostgreSQL (dev DB) | `check_local_postgres()` | Docker Compose `postgres` service |
| `onnx_embedder` | ONNX all-MiniLM-L6-v2 | `check_onnx()` | `local/agents/shared/embedder.py` |
| `mlflow` | MLflow experiment tracking | `check_mlflow()` | Docker Compose `mlflow` service |
| `validation_pipeline` | Pydantic validation + PII check | None (boundary) | `local/pipeline/validator.py` |
| `pipeline_sender` | HMAC-signed HTTP sender | None (boundary) | `local/pipeline/sender.py` |

### Cloud Components (16 nodes)

| Dashboard Node | System Component | Health Check | Source Files |
|---|---|---|---|
| `cloud_ingest_api` | FastAPI ingest endpoints | None (boundary) | `api/main.py` (ingest routes) |
| `eventbridge` | EventBridge cron rules | `check_eventbridge()` | `infra/eventbridge.tf` |
| `sqs` | SQS jd-scrape queue + DLQ | `check_sqs()` | `infra/data.tf` |
| `jd_ingestion` | JD Ingestion Agent (SQS-triggered on ECS) | `check_jd_ingestion()` | `api/agents/jd_ingestion/` |
| `s3` | S3 JD storage bucket | `check_s3()` | `infra/data.tf` |
| `rds` | RDS PostgreSQL (production) | `check_rds()` | `infra/schema.sql` (13 tables) |
| `analysis_poller` | Background analysis task | `check_analysis_poller()` | `api/main.py` (polling loop) |
| `cloud_coordinator` | Cloud Coordinator agent | None (agent) | `api/agents/cloud_coordinator/` |
| `jd_analyzer` | JD Analyzer agent | None (agent) | `api/agents/jd_analyzer/` |
| `sponsorship_screener` | Sponsorship Screener agent | None (agent) | `api/agents/sponsorship_screener/` |
| `resume_matcher` | Resume Matcher agent | None (agent) | `api/agents/resume_matcher/` |
| `application_chat` | Application Chat agent | None (agent) | `api/agents/application_chat/` |
| `bedrock_kb` | Bedrock Knowledge Base | `check_bedrock_kb()` | `infra/bedrock.tf` |
| `ecs` | ECS Fargate cluster | None (infra) | `infra/ecs.tf` |
| `user_dashboard` | Frontend SPA | None (UI) | `api/static/` |

---

## Data Flow Edges (37 total)

The dashboard tracks 37 edges representing data flow between components.
See `api/debug/topology.py` EDGES list for the complete set.

Key flows:
- **Local pipeline:** gmail → email_classifier → stage_classifier/recommendation_parser → validation_pipeline → pipeline_sender → cloud_ingest_api
- **Cloud pipeline:** eventbridge → sqs → jd_ingestion (fetch → screen sponsorship → store S3 + persist RDS → analyze → match) → rds → analysis_poller → cloud_coordinator
- **Cross-boundary:** pipeline_sender → cloud_ingest_api (HMAC-signed, PII-sanitized)

---

## When to Update the Dashboard

**If you change ANY of these, update the dashboard:**

| System Change | Dashboard Files to Update |
|---|---|
| Add/remove an agent | `api/debug/topology.py` (add/remove node + edges) |
| Add/remove a table in schema.sql | `api/debug/health_checks.py` (update RDS check expected tables) |
| Rename a cloud agent | `api/debug/health_checks.py` (update check function), `api/debug/topology.py` (update node) |
| Add/remove an EventBridge rule | `api/debug/health_checks.py` (update rule count), `api/debug/topology.py` (update description) |
| Add/remove an SQS queue | `api/debug/health_checks.py` (update queue checks) |
| Change Docker Compose services | `local/debug/local_checks.py` (add/remove service check) |
| Add a new external source adapter | `api/debug/topology.py` (update EventBridge node description) |
| Change agent chain order | `api/debug/topology.py` (update edges) |
| Add/remove a health check endpoint | `local/debug_dashboard.py` (update routing) |

**`tests/test_dashboard_sync.py` enforces:**
- Every agent directory has a topology node
- Topology node count matches expected
- Cloud agent names in health checks match topology nodes

---

## Health Check Status Codes

| Status | Meaning | Dashboard Color |
|--------|---------|----------------|
| `green` | Fully operational, all expected-vs-actual match | Green |
| `yellow` | Operational with warnings (stale data, degraded) | Yellow |
| `red` | Failed or unreachable | Red |
| `unknown` | Check not implemented or skipped | Gray |

---

## Running the Dashboard

```bash
# Start with Docker Compose (includes debug service on port 8002)
docker-compose up -d

# Open in browser
open http://localhost:8002/static/debug_dashboard.html

# Or query API directly
curl http://localhost:8002/api/debug/health | python3 -m json.tool
curl http://localhost:8002/api/debug/summary
curl http://localhost:8002/api/debug/component/rds
```
