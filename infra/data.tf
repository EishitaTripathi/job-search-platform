# Data sources — references to resources created manually in AWS console.
# These let Terraform reference existing resources without trying to recreate them.

data "aws_caller_identity" "current" {}

data "aws_s3_bucket" "jd_storage" {
  bucket = var.s3_bucket_name
}

resource "aws_sqs_queue" "jd_scrape" {
  name                       = "${var.project_name}-jd-scrape-queue"
  visibility_timeout_seconds = 300
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20     # long polling

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jd_scrape_dlq.arn
    maxReceiveCount     = 3
  })

  tags = local.common_tags
}

data "aws_secretsmanager_secret" "app" {
  name = "${var.project_name}/production"
}

data "aws_ecr_repository" "app" {
  name = var.project_name
}
