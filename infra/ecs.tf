# ECS Fargate — ALB, cluster, task definition, service.
#
# Traffic flow: Internet → ALB (public subnets) → ECS task (private subnet)
# ALB requires 2 AZs for HA. ECS tasks run in private subnets with no public IP.
# ECS service registers tasks with the ALB target group automatically.

# =============================================================================
# ALB — Layer 7 load balancer in public subnets
#
# H1 fix: HTTPS listener with ACM cert when available, HTTP redirects to HTTPS.
# M1 fix: Access logging to S3 for forensic/abuse trail.
# M2 fix: Drop invalid HTTP headers to prevent request smuggling.
# =============================================================================

resource "aws_lb" "app" {
  name               = "${var.project_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = module.vpc.public_subnets

  drop_invalid_header_fields = true # M2: blocks header injection / request smuggling

  dynamic "access_logs" {
    for_each = var.alb_access_logs_bucket != "" ? [1] : []
    content {
      bucket  = var.alb_access_logs_bucket
      prefix  = "alb"
      enabled = true
    }
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-alb" })
}

resource "aws_lb_target_group" "app" {
  name        = "${var.project_name}-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = module.vpc.vpc_id
  target_type = "ip" # Fargate uses awsvpc mode — tasks get dynamic ENIs, not EC2 instance IDs

  health_check {
    path                = "/health"
    port                = "8080"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = local.common_tags
}

# H1 fix: when ACM cert is provided, HTTPS listener forwards traffic and HTTP redirects.
# When no cert, HTTP forwards directly (dev/testing only).

resource "aws_lb_listener" "https" {
  count             = var.acm_certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06" # TLS 1.3 preferred, 1.2 minimum
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# HTTP listener: redirect to HTTPS when cert exists, forward when no cert (dev)
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = var.acm_certificate_arn != "" ? "redirect" : "forward"

    dynamic "redirect" {
      for_each = var.acm_certificate_arn != "" ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }

    # Forward only when no HTTPS cert (dev mode)
    target_group_arn = var.acm_certificate_arn == "" ? aws_lb_target_group.app.arn : null
  }
}

# =============================================================================
# ECS Cluster — Fargate only, no EC2 capacity providers
# =============================================================================

resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-retried"

  setting {
    name  = "containerInsights"
    value = "enabled" # Minimal cost for single-task cluster, needed for anomaly detection
  }

  tags = local.common_tags
}

# =============================================================================
# CloudWatch Log Group — ECS tasks stream stdout/stderr here
# =============================================================================

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 14

  tags = local.common_tags
}

# =============================================================================
# Task Definition — container config, resource limits, IAM roles, secrets
#
# 256 CPU (0.25 vCPU) + 512 MiB memory is the smallest Fargate config.
# Sufficient for a FastAPI app serving a single-user dashboard.
#
# Two IAM roles:
# - execution_role: used by ECS agent to pull ECR image + inject secrets
# - task_role: used by the running container for AWS API calls (S3, SQS, etc.)
# =============================================================================

resource "aws_ecs_task_definition" "app" {
  family                   = var.project_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512   # Increased: now runs SQS consumer + adapter HTTP + sponsorship screening
  memory                   = 1024  # Increased: JD Ingestion Agent replaces Lambda Fetch + Persist
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name  = var.project_name
    image = "${data.aws_ecr_repository.app.repository_url}:latest"

    portMappings = [{
      containerPort = 8080
      protocol      = "tcp"
    }]

    environment = [
      { name = "PORT", value = "8080" },
      { name = "BEDROCK_KB_ID", value = var.bedrock_kb_id },
      { name = "SQS_QUEUE_NAME", value = aws_sqs_queue.jd_scrape.name },
      { name = "AWS_DEFAULT_REGION", value = var.aws_region },
      { name = "S3_BUCKET", value = data.aws_s3_bucket.jd_storage.id },
      { name = "SECURE_COOKIES", value = var.acm_certificate_arn != "" ? "true" : "false" }
    ]

    secrets = [
      {
        name      = "DATABASE_URL"
        valueFrom = "${data.aws_secretsmanager_secret.app.arn}:DATABASE_URL::"
      },
      {
        name      = "JWT_SECRET"
        valueFrom = "${data.aws_secretsmanager_secret.app.arn}:JWT_SECRET::"
      },
      {
        name      = "INGEST_HMAC_KEY"
        valueFrom = "${data.aws_secretsmanager_secret.app.arn}:INGEST_HMAC_KEY::"
      },
      {
        name      = "APP_PASSWORD"
        valueFrom = "${data.aws_secretsmanager_secret.app.arn}:APP_PASSWORD::"
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    readonlyRootFilesystem = true # M3: prevent filesystem writes in compromised container
    essential              = true

    # tmpfs mount for FastAPI temp files (uvicorn needs /tmp)
    linuxParameters = {
      tmpfs = [{
        containerPath = "/tmp"
        size          = 64 # MiB
      }]
    }
  }])

  tags = local.common_tags
}

# =============================================================================
# ECS Service — wires cluster + task definition + ALB target group
#
# desired_count = 1: single task for a portfolio project. Production would use
# auto-scaling with min 2 across AZs.
#
# The service automatically registers/deregisters task IPs with the target
# group as tasks start/stop — no manual target registration needed.
# =============================================================================

resource "aws_ecs_service" "app" {
  name            = var.project_name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [module.vpc.private_subnets[0]] # private-fetch subnet (has NAT for outbound)
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = var.project_name
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.http]

  tags = local.common_tags
}
