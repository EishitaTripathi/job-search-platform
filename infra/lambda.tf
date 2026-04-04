# Lambda functions REMOVED — JD ingestion now handled by ECS JD Ingestion Agent.
#
# The JD Ingestion Agent (api/agents/jd_ingestion/) replaces:
# - Lambda Fetch (SQS → adapter HTTP → S3)
# - Lambda Persist (S3 → RDS upsert)
# - Sponsorship Screener (moved into ingestion pipeline, screens BEFORE S3 storage)
#
# SQS queue is consumed directly by ECS via background polling task in api/main.py.
# S3 bucket notification is removed (no Lambda Persist to trigger).
# EventBridge rules still send to SQS (unchanged in eventbridge.tf).

# =============================================================================
# SQS Dead-Letter Queue — catches messages that fail processing 3 times.
# Redrive policy is configured on the main queue in data.tf.
# =============================================================================

resource "aws_sqs_queue" "jd_scrape_dlq" {
  name                      = "${var.project_name}-jd-scrape-dlq"
  message_retention_seconds = 1209600 # 14 days — enough time to investigate failures

  tags = local.common_tags
}
