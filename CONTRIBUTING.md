# CONTRIBUTING.md — Onboarding & Development Guide
**Last updated: 2026-04-02**

## Reading Order

Start here, then read in this order:

1. **This file** (CONTRIBUTING.md) — How to get running, workflow overview
2. **REQUIREMENTS.md** — Source of truth for what the system does (84 requirements)
3. **DEPENDENCIES.md** — How everything connects (service, data flow, schema, infra maps)
4. **ARCHITECTURE.md** — Technical design (agents, RAG pipeline, infrastructure)
5. **CLAUDE.md** — Code conventions, security checklist, development rules
6. **SECURITY.md** — Threat model, OWASP control mappings
7. **AWS_STATE.md** — What's deployed, internet exposure, AWS safety rules
8. **SOURCES.yaml** — External data sources and TOS compliance status

---

## First-Time Setup

**If this is your first time:** Follow [SETUP.md](SETUP.md) for complete setup instructions
(local stack, Gmail OAuth, AWS infrastructure, CI/CD).

## Prerequisites

- Docker + Docker Compose
- Python 3.11+ (project targets 3.11 specifically)
- Terraform (for infrastructure changes)
- AWS CLI configured (for cloud deployment and debugging)
- pre-commit (`pip install pre-commit && pre-commit install`)

---

## Quick Start (after setup is complete)

```bash
# 1. Start local services (Ollama auto-pulls phi3:mini on first boot)
docker compose up -d

# 2. Verify services are healthy (Ollama may take 2-5 min on first boot)
docker compose ps

# 3. Run the test suite
pytest tests/ -v

# 4. Verify pre-commit hooks
pre-commit run --all-files

# 6. (Optional) Open debug dashboard
# http://localhost:8002/static/debug_dashboard.html
```

---

## Working with Claude Code

Claude Code reads `CLAUDE.md` automatically as its primary instruction file every session.

### What Claude Code Hooks Enforce

The project has 8 hooks configured in `.claude/settings.json`:

| When | What | Blocking? |
|------|------|-----------|
| Every user request | Analyzes intent, tells Claude which docs to read | No |
| Read .env/.tfstate/credentials | Blocks access to sensitive files | Yes |
| Run dangerous AWS commands | Blocks terraform destroy, SG changes, IAM changes | Yes |
| git commit | Runs automated security audit (5 checks) | Yes |
| git commit | Prompt asks Claude to verify auth/PII/README | Yes |
| git push | Runs terraform validate + test verification | Yes |
| Edit agent tools.py | Prints schema type mapping reminders | No |
| Edit adapter files | Reminds to update SOURCES.yaml | No |

### Development Workflow (step by step)

1. **Understand request** — Claude reads REQUIREMENTS.md, identifies affected requirements
2. **Load guardrails** — Claude reads relevant docs (DEPENDENCIES.md, AWS_STATE.md, SOURCES.yaml, etc.)
3. **Implement** — Edit code. Hooks fire on each edit (schema warnings, adapter reminders)
4. **Test** — `pytest tests/ -v`
5. **Pre-commit** — `pre-commit run --all-files`
6. **Security audit** — Review CLAUDE.md 16-point checklist
7. **Commit** — Hooks run automated audit + judgment review. Blocked if any fail.
8. **Push** — Hooks run terraform validate + tests. Blocked if any fail.
9. **Post-implementation** — Update REQUIREMENTS.md status if changed, create ADRs if needed.

---

## Directory Map

```
Root documents:
  REQUIREMENTS.md        <- Source of truth: 84 project requirements
  DEPENDENCIES.md        <- Complete dependency map (7 sections)
  AWS_STATE.md           <- Infrastructure state, internet exposure, safety rules
  SECURITY.md            <- Controls matrix, OWASP mappings
  RUNBOOK.md             <- Operational procedures, troubleshooting playbooks
  SOURCES.yaml           <- External source registry (machine-readable, CI-validated)
  CONTRIBUTING.md        <- This file (onboarding guide)
  CLAUDE.md              <- Claude Code conventions + 16-point security checklist
  ARCHITECTURE.md        <- Technical architecture design
  CONTEXT.md             <- Full design rationale (34KB)
  README.md              <- Public-facing project overview

Infrastructure:
  infra/schema.sql       <- Database schema (13 tables, source of truth)
  infra/SCHEMA_TYPES.md  <- PostgreSQL <-> Python type mappings
  infra/migrations/      <- SQL migration files
  infra/*.tf             <- Terraform IaC (10 files)

Code:
  api/                   <- Cloud: FastAPI + Bedrock agents (ECS Fargate)
    api/main.py          <- 26 API endpoints + analysis poller
    api/agents/          <- 5 cloud agents (coordinator, jd_analyzer, resume_matcher, sponsorship_screener, application_chat)
    api/agents/bedrock_client.py <- HAIKU/SONNET model constants, invoke_model, sanitize_for_prompt
    api/debug/           <- Health checks, schema sync, cloud topology
    api/static/          <- Dashboard HTML/JS/CSS

  local/                 <- Local: Phi-3 agents + pipeline (Docker)
    local/main.py        <- APScheduler entry point (email check every 2h)
    local/agents/        <- 5 local agents (email_classifier, stage_classifier, deadline_tracker, recommendation_parser, followup_advisor)
    local/agents/shared/ <- Shared modules: db, llm, memory, embedder, redactor, secrets, tracking, dispatch
    local/pipeline/      <- Validation pipeline: schemas.py -> validator.py -> sender.py
    local/gmail/         <- Gmail OAuth flow
    local/debug/         <- Local health checks, cloud proxy

  api/agents/jd_ingestion/ <- JD Ingestion Agent (LangGraph on ECS): fetch, screen, store, analyze

  tests/                 <- Test suite (20 files)
  docs/adr/              <- Architecture Decision Records (local-only, gitignored)

Hooks:
  .claude/settings.json  <- Claude Code hook configuration (8 hooks)
  .claude/hooks/         <- Hook scripts (6 shell scripts)

Config:
  .env.example           <- Environment variable template
  docker-compose.yml     <- Local services (6 containers)
  Dockerfile             <- Cloud API container (Python 3.13, non-root)
  Dockerfile.local       <- Local dev container (Python 3.11, non-root)
  .pre-commit-config.yaml <- Pre-commit hooks (detect-secrets, ruff, block-secrets)
  .github/workflows/     <- CI/CD (test + deploy on push to main)
  pytest.ini             <- Test configuration
```

---

## Common Gotchas

### PostgreSQL Type Mappings
The #1 source of bugs. See `infra/SCHEMA_TYPES.md` for the complete table.
- **TEXT[] columns** (9 total): pass Python `list[str]` directly. NEVER `json.dumps()`.
- **INT4RANGE** (1 column): use `asyncpg.Range(lo, hi)`. NEVER string `"[lo,hi)"`.
- **TEXT[] concatenation**: `array_cat(COALESCE(col, '{}'::text[]), $N::text[])`. NEVER `|| $N::jsonb`.

### Bedrock Model IDs
Check `SOURCES.yaml` `aws_services.bedrock.models` for current model IDs. The HAIKU and SONNET constants in `api/agents/bedrock_client.py` must match.

### Pre-commit Blocks
These files are blocked from commits: `.env`, `token.json`, `credentials.json`, `*.pem`, `*.key`, `*.tfstate`, `pii_audit.jsonl`.

### VPC Subnet Routing
- **public subnets** (ALB, NAT): route 0.0.0.0/0 to Internet Gateway
- **private-fetch** (ECS Fargate): route 0.0.0.0/0 to NAT instance (has internet)
- **private-data** (RDS): NO internet route. Uses VPC endpoints for S3/Secrets Manager.

### Network Security
NEVER open NAT/RDS security groups to the internet, even temporarily. See `AWS_STATE.md` Section 4.

---

## Running Tests

```bash
# Full test suite
pytest tests/ -v

# Just schema validation
pytest tests/test_sql_schema_sync.py -v

# Just source registry validation
pytest tests/test_source_registry.py -v

# Smoke tests (require Docker Compose running)
pytest -m smoke

# Quick pre-commit shortcut
pytest tests/ -v && pre-commit run --all-files && cd infra && terraform validate
```
