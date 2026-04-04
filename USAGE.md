# Usage Guide

How to use the platform after completing [SETUP.md](SETUP.md).

---

## Services & Ports

After `docker compose up`, these services are running:

| Port | Service | URL | Purpose |
|------|---------|-----|---------|
| 5433 | PostgreSQL | `localhost:5433` | Local database (mirrors production RDS) |
| 11434 | Ollama | `localhost:11434` | Phi-3 Mini 3.8B for email classification |
| 8000 | ChromaDB | `localhost:8000` | Vector store for few-shot learning |
| 5001 | MLflow | `localhost:5001` | Experiment tracking dashboard |
| 8001 | Resume Service | `localhost:8001` | Resume upload with PII redaction |
| 8002 | Debug Dashboard | `localhost:8002` | System health monitoring |
| Cloud ALB | Dashboard + API | Your ALB DNS | Main UI (requires AWS deployment) |

---

## Getting Started

### 1. Log in to the dashboard

Open your cloud ALB URL in a browser. Enter the password you set as `APP_PASSWORD` in `.env`. There is no username — this is a single-user system. The session lasts 8 hours.

### 2. Upload a resume

Go to the **Resumes** tab and upload a resume file.

- **Supported formats:** `.pdf`, `.txt`, `.docx` (max 10 MB)
- **PII stripping:** The system detects names, emails, phone numbers, SSNs, and URLs using Microsoft Presidio
- **Preview:** You see original vs. redacted text side-by-side before approving
- **Storage:** The approved redacted version is stored locally and on S3. PII never leaves your machine.

After upload, the resume is matched against all existing jobs automatically.

### 3. Wait for jobs (or trigger manually)

Jobs enter the system three ways:

**Automatic (EventBridge schedules):**
- The Muse: daily at 6am UTC
- Simplify: daily at 6am UTC
- HN Who's Hiring: 1st of each month at 9am UTC

**Manual (SQS command):**
```bash
# Fetch from The Muse
aws sqs send-message \
  --queue-url $(aws sqs get-queue-url --queue-name job-search-platform-jd-scrape-queue --query QueueUrl --output text) \
  --message-body '{"source": "the_muse", "params": {"category": "Engineering"}}'

# Fetch from Simplify
aws sqs send-message \
  --queue-url $(aws sqs get-queue-url --queue-name job-search-platform-jd-scrape-queue --query QueueUrl --output text) \
  --message-body '{"source": "simplify", "params": {}}'

# Search a specific company (Greenhouse ATS)
aws sqs send-message \
  --queue-url $(aws sqs get-queue-url --queue-name job-search-platform-jd-scrape-queue --query QueueUrl --output text) \
  --message-body '{"source": "greenhouse", "params": {"company": "anthropic"}}'
```

**Email (requires Gmail OAuth):** Job recommendations extracted from emails are automatically fetched and ingested.

Each job goes through: fetch → sponsorship screen → S3 store → JD analysis → resume matching. This takes 2-3 minutes per job.

---

## Dashboard Tabs

### Jobs

Browse all discovered jobs. Filter by status, date range, or sort by match score (requires a resume). Click any job card to see its detail view.

**Controls:**
- **Sort:** Newest First or Best Match (per resume)
- **Status filter:** To Apply, Applied, Assessment, Interview, Offer, Rejected, etc.
- **Date filter:** Last 24h, Last Week, Last 2 Weeks, Last Month
- **Blocklist:** Add company names or role keywords to hide irrelevant jobs

### Job Detail (click a job)

Shows everything the system knows about a job:
- **Analysis:** Required skills, preferred skills, tech stack, role type, experience range, deal breakers, remote policy
- **Match Report:** Overall fit score (0-1), fit category, skill gaps, strengths, reasoning (per resume)
- **Chat:** Ask questions about this specific job (see Chat section below)
- **Deadlines:** Any extracted dates (assessment due, interview scheduled)

### Follow-ups

Application follow-up recommendations sorted by urgency (high / medium / low). Actions: send follow-up, check status, withdraw. Mark each as acted when done.

### Deadlines

Upcoming application deadlines extracted from emails by the Deadline Tracker agent. Shows date, type (assessment, interview, assignment), and associated job.

### Chat

Select a job from the dropdown and ask questions. The chat agent has access to:
- JD analysis (skills, requirements, deal breakers)
- Match reports (fit score, gaps, strengths)
- Previous Q&A for this job (answer memory)
- Knowledge base context (related JDs)

Examples: "What skills am I missing?", "Is this role remote?", "How should I prepare for the interview?"

Requires cloud deployment with Bedrock model access (Claude Sonnet).

### Pipeline Ops

Audit trail of all orchestration runs — every agent invocation with status, duration, and results. Operational metrics for monitoring pipeline health.

### Resumes

List of all uploaded resumes with upload dates. Upload new resumes here.

### Review Queue

Emails classified with low confidence (< 85%) appear here for manual review. You see the email subject, snippet, and the agent's best guess. Correct the label to improve future classifications — corrections are stored in ChromaDB and used as few-shot examples.

The badge on the tab shows how many items are waiting for review.

---

## Email Pipeline

Requires Gmail OAuth setup (SETUP.md Step 2). Once configured:

1. **Every 2 hours:** The scheduler checks Gmail for unread emails
2. **Classification:** Each email is classified as `irrelevant`, `status_update`, or `recommendation`
3. **High confidence (>= 85%):** Auto-stored and routed:
   - `status_update` → Stage Classifier → Deadline Tracker → sent to cloud
   - `recommendation` → Recommendation Parser → cloud JD fetch via SQS
   - `irrelevant` → skipped
4. **Low confidence (< 85%):** Queued in Review Queue for your manual correction
5. **Daily at 9:05am UTC:** Follow-up Advisor checks for stale applications

Your corrections improve the classifier over time through ChromaDB few-shot learning.

---

## Debug Dashboard

Open `http://localhost:8002/static/debug_dashboard.html`.

Shows ~30 platform components as an interactive node graph with live health status:
- **Green:** Healthy
- **Yellow:** Degraded or warning
- **Red:** Down or erroring

Click any component to see error details and recent logs.

**When to use:**
- After initial setup to verify all services are healthy
- When jobs aren't appearing (check SQS, EventBridge, S3 components)
- When email features aren't working (check Gmail, Ollama, ChromaDB components)
- When match scores are missing (check Bedrock, Knowledge Base components)

---

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| No jobs showing | Debug dashboard → SQS + EventBridge | Trigger manual SQS fetch (see above) |
| No match scores | Need resume uploaded + Bedrock access | Upload resume, verify AWS credentials |
| Email features disabled | `Gmail not configured` in logs | Complete Gmail OAuth (SETUP.md Step 2) |
| Chat not responding | Requires cloud + Bedrock | Deploy to AWS, enable Bedrock model access |
| Dashboard login fails | `APP_PASSWORD` not set | Set in `.env` file, restart |
| Resume upload fails | Check file format and size | Must be .pdf/.txt/.docx, max 10 MB |
| Review Queue empty | No emails processed yet | Configure Gmail, wait for 2-hour check cycle |
| Stale jobs | EventBridge not firing | Check AWS EventBridge rules in console |
