# SETUP.md — Complete Setup Guide

This guide walks you through setting up the entire platform from scratch.
Follow the steps in order. Each step builds on the previous one.

---

## Prerequisites

Install these before starting:

- **Docker** + **Docker Compose** (v2+)
- **Python 3.11+** (for running tests locally)
- **Terraform** (>= 1.5) — for AWS infrastructure
- **AWS CLI v2** — configured with your credentials (`aws configure`)
- **Git** + **pre-commit** (`pip install pre-commit && pre-commit install`)

Optional (for Gmail email features):
- **Google Cloud Console access** — for Gmail API OAuth setup

---

## Step 1: Local Development Stack

This gets you a working local environment with PostgreSQL, Ollama, ChromaDB, and MLflow.

### 1.1 Clone and configure

```bash
git clone https://github.com/EishitaTripathi/job-search-platform.git
cd job-search-platform

# Create your .env file from the template
cp .env.example .env
```

Edit `.env` and fill in at minimum:
```
APP_PASSWORD=your-dashboard-password
JWT_SECRET=<run: openssl rand -hex 32>
```

The `DATABASE_URL` default works with Docker Compose. Leave AWS vars empty for now.

### 1.2 Start the local stack

```bash
docker compose up -d
```

This starts 6 services:
- **PostgreSQL** (port 5433) — auto-loads `infra/schema.sql` on first boot
- **Ollama** (port 11434) — auto-pulls phi3:mini model on first boot (may take 2-5 min)
- **ChromaDB** (port 8000) — vector store for few-shot retrieval
- **MLflow** (port 5001) — experiment tracking
- **App** (port 8001) — local agents + resume upload service
- **Debug Dashboard** (port 8002) — system observability

### 1.3 Verify everything started

```bash
# Check all services are healthy
docker compose ps

# Expected: all services show "healthy" or "running"
# Ollama may show "starting" for 2-5 minutes while pulling the model
```

Wait for Ollama to finish pulling (health check verifies phi3 is available):
```bash
# Watch until healthy
docker compose logs ollama -f
# Look for: "success" or "pulling manifest"
```

### 1.4 Run the test suite

```bash
# Install local Python dependencies (for running tests outside Docker)
pip install -r requirements.local.txt

# Run tests
pytest tests/ -v
```

### 1.5 What works without AWS

Without AWS configured, you get:
- Local PostgreSQL with full schema
- Ollama for local LLM inference
- ChromaDB for vector storage
- MLflow for experiment tracking
- Resume upload service (stores locally, skips S3)
- Debug dashboard (local components only, cloud components show "unknown")

What does NOT work without AWS:
- Cloud pipeline (JD fetch, analysis, resume matching)
- Cloud dashboard (no job data from RDS)
- Debug dashboard cloud components

---

## Step 2: Gmail OAuth (Optional)

Skip this step if you don't need email classification. The local stack runs
fine without it — email-dependent agents are automatically disabled.

### 2.1 Create a Google Cloud project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (e.g., "Job Search Platform")
3. In the left sidebar: **APIs & Services > Library**
4. Search for "Gmail API" and click **Enable**

### 2.2 Create OAuth credentials

1. Go to **APIs & Services > Credentials**
2. Click **+ CREATE CREDENTIALS > OAuth client ID**
3. If prompted, configure the consent screen:
   - User type: **External** (or Internal if using Google Workspace)
   - App name: "Job Search Platform"
   - Scopes: add `https://www.googleapis.com/auth/gmail.readonly`
   - Test users: add your Gmail address
4. Application type: **Desktop app**
5. Click **Create**
6. Download the JSON file

### 2.3 Save credentials

```bash
# Create credentials directory
mkdir -p credentials

# Move the downloaded file
mv ~/Downloads/client_secret_*.json credentials/credentials.json
```

### 2.4 Run the OAuth flow

This opens a browser for you to authorize the app:

```bash
# Run from the project root (needs a browser)
python -c "from local.gmail.auth import get_gmail_service; get_gmail_service()"
```

This creates `credentials/token.json`. Both files are gitignored.

### 2.5 Verify

```bash
# Restart the app service to pick up Gmail credentials
docker compose restart app

# Check logs — should say "Gmail configured"
docker compose logs app | grep -i gmail
```

---

## Step 3: AWS Infrastructure

This deploys the cloud pipeline: ECS, S3, SQS, Bedrock, RDS.

### 3.1 Prerequisites

You need an AWS account with:
- Admin access (or at minimum: ECS, S3, SQS, RDS, Bedrock, IAM, VPC, ECR, Secrets Manager, EventBridge, CloudWatch)
- AWS CLI configured: `aws configure` (sets region to `us-east-2`)

### 3.2 Create pre-required AWS resources

Terraform references these via `data` sources, so they must exist before `terraform apply`.

**3.2.1 S3 bucket for Terraform state:**
```bash
aws s3 mb s3://YOUR-PROJECT-NAME-tfstate --region us-east-2
```
Then update `infra/main.tf` backend block with your bucket name.

**3.2.2 S3 bucket for JD storage:**
```bash
aws s3 mb s3://YOUR-PROJECT-NAME-jds-YOUR-ACCOUNT-ID --region us-east-2
```

**3.2.3 ECR repository:**
```bash
aws ecr create-repository \
  --repository-name job-search-platform \
  --region us-east-2
```

**3.2.4 Secrets Manager secret:**
```bash
aws secretsmanager create-secret \
  --name "job-search-platform/production" \
  --region us-east-2 \
  --secret-string '{
    "DATABASE_URL": "postgresql://USER:PASS@RDS-ENDPOINT:5432/jobsearch",  # pragma: allowlist secret
    "JWT_SECRET": "GENERATE-WITH-openssl-rand-hex-32",  # pragma: allowlist secret
    "INGEST_HMAC_KEY": "GENERATE-WITH-openssl-rand-hex-32",  # pragma: allowlist secret
    "APP_PASSWORD": "YOUR-DASHBOARD-PASSWORD"  # pragma: allowlist secret
  }'
```

Note: `DATABASE_URL` will need the actual RDS endpoint after terraform creates RDS. You'll update this secret after the first `terraform apply`.

**3.2.5 Bedrock Knowledge Base:**
1. Go to **AWS Console > Amazon Bedrock > Knowledge Bases**
2. Click **Create knowledge base**
3. Name: `job-search-platform-kb`
4. Data source: S3 — point to your JD storage bucket, prefix `jds/`
5. Embedding model: **Titan Embeddings v2** (`amazon.titan-embed-text-v2:0`)
6. Vector store: let Bedrock create a managed OpenSearch Serverless collection
7. Click **Create** and copy the **Knowledge Base ID**

**3.2.6 Enable Bedrock model access:**
1. Go to **AWS Console > Amazon Bedrock > Model access**
2. Click **Modify model access**
3. Enable:
   - **Anthropic Claude Haiku 4.5** (may require Marketplace subscription)
   - **Anthropic Claude Sonnet 4.6**
   - **Amazon Titan Embeddings v2** (usually auto-enabled)
4. Wait for status to show "Access granted"

### 3.3 Configure Terraform variables

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:
```hcl
s3_bucket_name = "YOUR-JD-STORAGE-BUCKET-NAME"
bedrock_kb_id  = "YOUR-KNOWLEDGE-BASE-ID"
```

### 3.4 Deploy infrastructure

```bash
cd infra

# Initialize (downloads providers, connects to state backend)
terraform init

# Preview what will be created
terraform plan

# Deploy (review the plan, then type "yes")
terraform apply
```

This creates: VPC, NAT instance, ALB, ECS cluster, SQS queue, RDS, security groups, IAM roles, VPC endpoints, EventBridge rules.

### 3.5 Update Secrets Manager with RDS endpoint

After `terraform apply`, get the RDS endpoint:
```bash
terraform output rds_endpoint
```

Update the Secrets Manager secret with the real DATABASE_URL:
```bash
aws secretsmanager update-secret \
  --secret-id "job-search-platform/production" \
  --secret-string '{
    "DATABASE_URL": "postgresql://jobsearch:PASSWORD@ACTUAL-RDS-ENDPOINT:5432/jobsearch?sslmode=require",  # pragma: allowlist secret
    "JWT_SECRET": "YOUR-JWT-SECRET",  # pragma: allowlist secret
    "INGEST_HMAC_KEY": "YOUR-HMAC-KEY",  # pragma: allowlist secret
    "APP_PASSWORD": "YOUR-DASHBOARD-PASSWORD"  # pragma: allowlist secret
  }'
```

### 3.6 Load schema into RDS

```bash
# Get RDS endpoint from terraform output
RDS_ENDPOINT=$(terraform output -raw rds_endpoint)

# Load schema (requires psql and network access to RDS — use a bastion or VPN)
psql "postgresql://jobsearch:PASSWORD@${RDS_ENDPOINT}:5432/jobsearch?sslmode=require" \  # pragma: allowlist secret
  -f schema.sql
```

### 3.7 Update .env with cloud values

Back in the project root, update `.env`:
```bash
CLOUD_API_URL=http://YOUR-ALB-DNS-NAME   # from: terraform output alb_dns_name
INGEST_HMAC_KEY=YOUR-HMAC-KEY            # must match Secrets Manager value
S3_BUCKET=YOUR-JD-STORAGE-BUCKET
BEDROCK_KB_ID=YOUR-KNOWLEDGE-BASE-ID
```

Restart the local stack to pick up cloud integration:
```bash
docker compose restart app debug
```

---

## Step 4: CI/CD Setup (GitHub Actions)

### 4.1 Push initial code

```bash
# Push to your GitHub repo
git remote set-url origin https://github.com/YOUR-USERNAME/job-search-platform.git
git push -u origin main
```

### 4.2 Add GitHub repository secrets

Go to your repo: **Settings > Secrets and variables > Actions**

Add these secrets:
- `AWS_ACCESS_KEY_ID` — IAM user access key
- `AWS_SECRET_ACCESS_KEY` — IAM user secret key

The IAM user needs these permissions:
- `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:PutImage`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`
- `ecs:UpdateService`, `ecs:DescribeServices`

### 4.3 Verify

Push a commit to `main` and check the Actions tab. The workflow:
1. Runs `pytest` on all tests
2. Builds Docker image for ECS
3. Pushes image to ECR
4. Forces ECS redeployment with the new image

---

## Step 5: Verify End-to-End

### 5.1 Local health check
```bash
curl http://localhost:8002/api/debug/summary
# Should show green/yellow/red counts for all components
```

### 5.2 Cloud health check
```bash
# Open debug dashboard
open http://localhost:8002/static/debug_dashboard.html
# Green = working, Yellow = degraded, Red = failed
```

### 5.3 Test the pipeline manually

```bash
# Upload a resume via the local service
curl -X POST http://localhost:8001/upload \
  -F "file=@/path/to/your/resume.pdf" \
  -F "name=My Resume"

# Trigger a JD fetch (The Muse)
aws sqs send-message \
  --queue-url $(aws sqs get-queue-url --queue-name job-search-platform-jd-scrape-queue --query QueueUrl --output text) \
  --message-body '{"source": "the_muse", "params": {"category": "Engineering"}}'

# Wait 2-3 minutes for pipeline to process, then check dashboard
open http://YOUR-ALB-DNS-NAME
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `docker compose up` fails on Ollama | Wait longer — first pull takes 2-5 min. Check: `docker compose logs ollama` |
| Tests fail with `ModuleNotFoundError` | Install deps: `pip install -r requirements.local.txt` |
| `terraform init` says bucket not found | Create tfstate bucket first (Step 3.2.1) and update `infra/main.tf` backend |
| `terraform apply` fails on S3/ECR/Secrets | These must be created manually first (Step 3.2) |
| Bedrock `ResourceNotFoundException` | Enable model access in Bedrock console (Step 3.2.6) |
| Gmail scheduler disabled | Follow Step 2 to set up OAuth credentials |
| Debug dashboard shows all red | AWS credentials not configured or cloud stack not deployed |

For detailed troubleshooting playbooks, see `RUNBOOK.md` (internal doc).
