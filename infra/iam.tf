# IAM roles — least-privilege for each compute layer.
#
# Pattern: each service gets an assume-role trust policy + a scoped permissions policy.
# ECS has two roles: execution (pull image, inject secrets) and task (runtime API calls).
# Lambda gets one role each with VPC ENI permissions (required for VPC-attached Lambdas).

# =============================================================================
# ECS Task Execution Role — used BY ECS to pull images and inject secrets.
# This is NOT what the container code uses at runtime (that's the task role).
# =============================================================================

resource "aws_iam_role" "ecs_execution" {
  name = "${var.project_name}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = local.common_tags
}

# Base policy: ECR pull + CloudWatch Logs (AWS managed)
resource "aws_iam_role_policy_attachment" "ecs_execution_base" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Secrets Manager access — ECS injects DB_URL and JWT_SECRET as env vars at task launch
resource "aws_iam_policy" "ecs_secrets" {
  name = "${var.project_name}-ecs-secrets"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [data.aws_secretsmanager_secret.app.arn]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_secrets" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = aws_iam_policy.ecs_secrets.arn
}

# =============================================================================
# ECS Task Role — used BY the running container code at runtime.
# FastAPI needs: SQS (enqueue scrape jobs), S3 (read/write JDs + resumes),
# Secrets Manager (DB creds at runtime).
# =============================================================================

resource "aws_iam_role" "ecs_task" {
  name = "${var.project_name}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_policy" "ecs_task" {
  name = "${var.project_name}-ecs-task"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SQSFullAccess"
        Effect   = "Allow"
        Action   = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = [aws_sqs_queue.jd_scrape.arn]
      },
      {
        Sid      = "S3ReadWriteJDs"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:HeadObject"]
        Resource = ["${data.aws_s3_bucket.jd_storage.arn}/*"]
      },
      {
        Sid      = "SecretsManagerRead"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [data.aws_secretsmanager_secret.app.arn]
      },
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-*",
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6*",
          "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
          "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-haiku-4-5-*",
          "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-6*"
        ]
      },
      {
        Sid      = "BedrockKBRetrieve"
        Effect   = "Allow"
        Action   = ["bedrock:Retrieve"]
        Resource = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${var.bedrock_kb_id}"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = aws_iam_policy.ecs_task.arn
}

# Lambda Fetch + Lambda Persist IAM roles REMOVED.
# JD ingestion now handled by ECS task role (above).
# See api/agents/jd_ingestion/ for the replacement agent.

# =============================================================================
# Sync Service — IAM user policy for local machine to call cloud ingestion API.
# The local validated pipeline uses AWS credentials to HMAC-sign requests to
# POST /api/ingest/* endpoints on the cloud API (ECS/ALB).
# Scoped to: read HMAC key from Secrets Manager only.
# =============================================================================

resource "aws_iam_policy" "sync_service" {
  name = "${var.project_name}-sync-service"
  description = "Allows local sync service to call cloud ingestion API via SigV4"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeIngestionAPI"
        Effect   = "Allow"
        Action   = ["execute-api:Invoke"]
        Resource = [
          "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/api/ingest/*"
        ]
      },
      {
        Sid      = "ReadHMACKey"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [data.aws_secretsmanager_secret.app.arn]
      }
    ]
  })
}
