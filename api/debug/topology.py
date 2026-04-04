"""Static topology data for the debug dashboard graph.

Defines all nodes, edges, and groups that represent the platform architecture.
The frontend renders this as an interactive directed graph.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Local nodes (group="local") — health checks run from debug container
# ---------------------------------------------------------------------------
_LOCAL_NODES: list[dict[str, Any]] = [
    {
        "id": "gmail",
        "label": "Gmail API",
        "group": "local",
        "category": "data_source",
        "health_check": "gmail",
        "description": "Fetches unread emails every 2h via APScheduler",
        "why": (
            "Email is the primary signal source — companies send status updates "
            "and job recommendations. The pipeline starts here."
        ),
    },
    {
        "id": "apscheduler",
        "label": "APScheduler",
        "group": "local",
        "category": "scheduler",
        "health_check": "apscheduler",
        "description": "Triggers email_check every 2h, daily_followup at 9:05 UTC",
        "why": (
            "Automation backbone — runs the pipeline on schedule without manual triggers."
        ),
    },
    {
        "id": "email_classifier",
        "label": "Email Classifier",
        "group": "local",
        "category": "local_agent",
        "health_check": "email_classifier",
        "description": (
            "3-way classification (irrelevant / status_update / recommendation) "
            "using Phi-3 + RAG few-shot"
        ),
        "why": (
            "First filter — routes emails to correct downstream agents, avoiding "
            "manual triage. Few-shot learning from ChromaDB improves over time."
        ),
    },
    {
        "id": "stage_classifier",
        "label": "Stage Classifier",
        "group": "local",
        "category": "local_agent",
        "health_check": None,
        "description": "Classifies status_update emails into 8 application stages",
        "why": (
            "Tracks application lifecycle — determines if a company moved you "
            "forward (applied → interview → offer)."
        ),
    },
    {
        "id": "deadline_tracker",
        "label": "Deadline Tracker",
        "group": "local",
        "category": "local_agent",
        "health_check": None,
        "description": "Extracts deadline dates from assessment/assignment/interview emails",
        "why": (
            "Prevents missed deadlines — surfaces upcoming due dates automatically."
        ),
    },
    {
        "id": "recommendation_parser",
        "label": "Recommendation Parser",
        "group": "local",
        "category": "local_agent",
        "health_check": None,
        "description": "Extracts company/role pairs from recommendation emails",
        "why": ("Discovers new job opportunities from network recommendations."),
    },
    {
        "id": "followup_advisor",
        "label": "Follow-up Advisor",
        "group": "local",
        "category": "local_agent",
        "health_check": None,
        "description": "Daily check for stale applications, generates follow-up suggestions",
        "why": (
            "Prevents applications from going cold — flags jobs where no update "
            "received in 7+ days."
        ),
    },
    {
        "id": "ollama",
        "label": "Ollama (Phi-3 Mini)",
        "group": "local",
        "category": "infrastructure",
        "health_check": "ollama",
        "description": "Local LLM for email classification and entity extraction",
        "why": (
            "Privacy-preserving AI inference — processes PII-containing emails "
            "locally, never sends to cloud."
        ),
    },
    {
        "id": "chromadb",
        "label": "ChromaDB",
        "group": "local",
        "category": "data_store",
        "health_check": "chromadb",
        "description": "Vector store for email classification few-shot examples",
        "why": (
            "RAG backbone — retrieves similar labeled emails to inject as context, "
            "improving classification accuracy."
        ),
    },
    {
        "id": "local_postgres",
        "label": "Local PostgreSQL",
        "group": "local",
        "category": "data_store",
        "health_check": "local_postgres",
        "description": "Local dev database mirroring production RDS schema",
        "why": (
            "Development parity — identical schema allows testing locally before deploying."
        ),
    },
    {
        "id": "onnx_embedder",
        "label": "ONNX Embedder",
        "group": "local",
        "category": "infrastructure",
        "health_check": "onnx_embedder",
        "description": "all-MiniLM-L6-v2 embeddings (384-dim)",
        "why": (
            "Privacy-preserving embeddings — generates vectors locally without "
            "sending text to cloud services."
        ),
    },
    {
        "id": "mlflow",
        "label": "MLflow",
        "group": "local",
        "category": "infrastructure",
        "health_check": "mlflow",
        "description": "Experiment tracking for agent performance metrics",
        "why": (
            "Observability — tracks classification accuracy, latency, confidence. "
            "Gracefully degrades if unavailable."
        ),
    },
    {
        "id": "validation_pipeline",
        "label": "Validation Pipeline",
        "group": "local",
        "category": "boundary",
        "health_check": None,
        "description": "Format check + PII detection (Presidio) before cloud send",
        "why": (
            "Triple-layer safety net — prevents malformed or PII-leaking data "
            "from reaching cloud."
        ),
    },
    {
        "id": "pipeline_sender",
        "label": "Pipeline Sender",
        "group": "local",
        "category": "boundary",
        "health_check": None,
        "description": "HMAC-authenticated HTTP POST to cloud ingest API",
        "why": (
            "Secure transport layer — signs every payload cryptographically, only "
            "authorized local instances can write to cloud."
        ),
    },
]

# ---------------------------------------------------------------------------
# Cloud nodes (group="cloud")
# ---------------------------------------------------------------------------
_CLOUD_NODES: list[dict[str, Any]] = [
    {
        "id": "cloud_ingest_api",
        "label": "Cloud Ingest API",
        "group": "cloud",
        "category": "boundary",
        "health_check": None,
        "description": "HMAC-authenticated endpoints receiving local pipeline data",
        "why": (
            "The only authorized entry point for local data into cloud DB. Verifies "
            "HMAC signatures to prevent unauthorized writes."
        ),
    },
    {
        "id": "eventbridge",
        "label": "EventBridge Rules",
        "group": "cloud",
        "category": "scheduler",
        "health_check": "eventbridge",
        "description": "Cron schedules: daily The Muse, weekly Simplify, monthly HN Who's Hiring",
        "why": (
            "Automated job discovery — sources new opportunities from external "
            "job boards on schedule."
        ),
    },
    {
        "id": "sqs",
        "label": "SQS Queue",
        "group": "cloud",
        "category": "infrastructure",
        "health_check": "sqs",
        "description": "Message queue buffering JD fetch requests",
        "why": (
            "Decouples job discovery from fetching — buffers requests so Lambda "
            "processes them independently."
        ),
    },
    {
        "id": "jd_ingestion",
        "label": "JD Ingestion Agent",
        "group": "cloud",
        "category": "cloud_agent",
        "health_check": None,
        "description": (
            "LangGraph agent: fetch JD → screen sponsorship → store to S3/RDS → "
            "analyze → match. Screens BEFORE storage (no KB pollution)."
        ),
        "why": (
            "Unified ingestion pipeline with conditional routing. Replaces Lambda "
            "Fetch + Lambda Persist + Sponsorship Screener. Disqualified JDs never "
            "reach S3 or Bedrock KB."
        ),
    },
    {
        "id": "s3",
        "label": "S3 Bucket",
        "group": "cloud",
        "category": "data_store",
        "health_check": "s3",
        "description": "Stores raw JD texts and redacted resumes",
        "why": (
            "Durable object storage — feeds Bedrock Knowledge Base indexing and "
            "Lambda processing."
        ),
    },
    {
        "id": "rds",
        "label": "RDS PostgreSQL",
        "group": "cloud",
        "category": "data_store",
        "health_check": "rds",
        "description": "Production database — jobs, analyses, matches, follow-ups (13 tables)",
        "why": (
            "Single source of truth for all structured data. Both local pipeline "
            "(via ingest) and cloud agents read/write here."
        ),
    },
    {
        "id": "analysis_poller",
        "label": "Analysis Poller",
        "group": "cloud",
        "category": "infrastructure",
        "health_check": "analysis_poller",
        "description": "Background task finding unanalyzed JDs (every 60s)",
        "why": (
            "Bridge between data ingestion and AI analysis — triggers cloud agent "
            "pipeline for new JDs."
        ),
    },
    {
        "id": "cloud_coordinator",
        "label": "Cloud Coordinator",
        "group": "cloud",
        "category": "cloud_agent",
        "health_check": None,
        "description": "Routes events to agent chains (new_jd → Analyzer → Screener → Matcher)",
        "why": (
            "Orchestration layer — determines which agents run based on event type, "
            "executes them sequentially."
        ),
    },
    {
        "id": "jd_analyzer",
        "label": "JD Analyzer",
        "group": "cloud",
        "category": "cloud_agent",
        "health_check": None,
        "description": "Claude Haiku strips boilerplate, extracts structured fields from JDs",
        "why": (
            "Converts free-text JDs into queryable structured data (skills, experience, "
            "deal-breakers) for matching."
        ),
    },
    {
        "id": "resume_matcher",
        "label": "Resume Matcher",
        "group": "cloud",
        "category": "cloud_agent",
        "health_check": None,
        "description": "Bedrock KB recall → structured filter → Claude Sonnet rerank",
        "why": (
            "Best-fit ranking — finds the most relevant jobs for each resume variant "
            "using 4-stage RAG pipeline."
        ),
    },
    {
        "id": "application_chat",
        "label": "Application Chat",
        "group": "cloud",
        "category": "cloud_agent",
        "health_check": None,
        "description": "Claude Sonnet Q&A about specific job applications",
        "why": (
            "Decision support — answers questions using JD analysis, match data, "
            "and conversation memory."
        ),
    },
    {
        "id": "bedrock_kb",
        "label": "Bedrock Knowledge Base",
        "group": "cloud",
        "category": "data_store",
        "health_check": "bedrock_kb",
        "description": "Managed RAG with Titan v2 embeddings + OpenSearch Serverless",
        "why": (
            "Scalable semantic search — auto-indexes JDs from S3 for resume "
            "matching recall."
        ),
    },
    {
        "id": "ecs",
        "label": "ECS Fargate",
        "group": "cloud",
        "category": "infrastructure",
        "health_check": None,
        "description": "Runs FastAPI API + cloud agents (0.25 vCPU, 512MB)",
        "why": (
            "Compute layer — hosts the main API and all cloud agent inference on "
            "managed infrastructure."
        ),
    },
    {
        "id": "user_dashboard",
        "label": "User Dashboard",
        "group": "cloud",
        "category": "infrastructure",
        "health_check": None,
        "description": "Vanilla JS SPA: jobs, follow-ups, deadlines, chat",
        "why": (
            "Primary user interface — where the user reviews matched jobs, acts on "
            "follow-ups, and asks questions."
        ),
    },
]

NODES: list[dict[str, Any]] = _LOCAL_NODES + _CLOUD_NODES

# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------
EDGES: list[dict[str, Any]] = [
    # --- Local pipeline ---
    {
        "source": "gmail",
        "target": "email_classifier",
        "label": "unread emails (every 2h)",
    },
    {"source": "apscheduler", "target": "gmail", "label": "trigger: email_check"},
    {
        "source": "apscheduler",
        "target": "followup_advisor",
        "label": "trigger: daily_followup (9:05 UTC)",
    },
    {
        "source": "email_classifier",
        "target": "stage_classifier",
        "label": "status_update emails",
    },
    {
        "source": "email_classifier",
        "target": "recommendation_parser",
        "label": "recommendation emails",
    },
    {
        "source": "stage_classifier",
        "target": "deadline_tracker",
        "label": "assessment/assignment/interview emails",
    },
    {
        "source": "stage_classifier",
        "target": "validation_pipeline",
        "label": "StatusPayload",
    },
    {
        "source": "deadline_tracker",
        "target": "validation_pipeline",
        "label": "StatusPayload + deadlines",
    },
    {
        "source": "recommendation_parser",
        "target": "validation_pipeline",
        "label": "RecommendationPayload",
    },
    {
        "source": "followup_advisor",
        "target": "validation_pipeline",
        "label": "FollowupPayload",
    },
    {"source": "email_classifier", "target": "ollama", "label": "Phi-3 inference"},
    {"source": "stage_classifier", "target": "ollama", "label": "Phi-3 inference"},
    {"source": "deadline_tracker", "target": "ollama", "label": "Phi-3 inference"},
    {"source": "recommendation_parser", "target": "ollama", "label": "Phi-3 inference"},
    {"source": "followup_advisor", "target": "ollama", "label": "Phi-3 inference"},
    {"source": "email_classifier", "target": "chromadb", "label": "few-shot retrieval"},
    {"source": "stage_classifier", "target": "chromadb", "label": "few-shot retrieval"},
    {
        "source": "email_classifier",
        "target": "onnx_embedder",
        "label": "text → 384-dim vector",
    },
    {
        "source": "stage_classifier",
        "target": "onnx_embedder",
        "label": "text → 384-dim vector",
    },
    {
        "source": "validation_pipeline",
        "target": "pipeline_sender",
        "label": "validated payloads",
    },
    # --- Cross-boundary ---
    {
        "source": "pipeline_sender",
        "target": "cloud_ingest_api",
        "label": "PII-sanitized payloads (HMAC-signed)",
        "cross_boundary": True,
    },
    # --- Cloud pipeline ---
    {
        "source": "cloud_ingest_api",
        "target": "rds",
        "label": "status/recommendation/followup records",
    },
    {"source": "eventbridge", "target": "sqs", "label": "cron schedule triggers"},
    {
        "source": "sqs",
        "target": "jd_ingestion",
        "label": "SQS messages (ECS polls directly)",
    },
    {
        "source": "jd_ingestion",
        "target": "s3",
        "label": "qualified JDs only (post-screening)",
    },
    {"source": "jd_ingestion", "target": "rds", "label": "upsert job records"},
    {
        "source": "jd_ingestion",
        "target": "jd_analyzer",
        "label": "analyze qualified JD",
    },
    {
        "source": "jd_ingestion",
        "target": "resume_matcher",
        "label": "match if resumes exist",
    },
    {"source": "analysis_poller", "target": "rds", "label": "query pending jobs"},
    {
        "source": "analysis_poller",
        "target": "cloud_coordinator",
        "label": "job_id + JD text",
    },
    {"source": "cloud_coordinator", "target": "jd_analyzer", "label": "new_jd event"},
    {"source": "jd_analyzer", "target": "rds", "label": "jd_analyses records"},
    {"source": "resume_matcher", "target": "rds", "label": "match_reports"},
    {
        "source": "resume_matcher",
        "target": "bedrock_kb",
        "label": "KB retrieve (top-50 candidates)",
    },
    {
        "source": "cloud_coordinator",
        "target": "application_chat",
        "label": "chat event",
    },
    {"source": "application_chat", "target": "rds", "label": "answer_memory records"},
    {
        "source": "application_chat",
        "target": "bedrock_kb",
        "label": "context retrieval",
    },
    {"source": "rds", "target": "user_dashboard", "label": "API queries"},
    {
        "source": "cloud_ingest_api",
        "target": "sqs",
        "label": "enqueue JD fetch for recommendations",
    },
]

# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
GROUPS: list[dict[str, str]] = [
    {"id": "local", "label": "Local (Docker Compose) \u2014 PII Zone"},
    {"id": "cloud", "label": "Cloud (AWS) \u2014 Sanitized Data Only"},
]


def get_topology() -> dict[str, Any]:
    """Return the complete topology structure for the debug dashboard."""
    return {
        "nodes": NODES,
        "edges": EDGES,
        "groups": GROUPS,
    }
