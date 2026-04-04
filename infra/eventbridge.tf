# EventBridge — scheduled rules for automated pipeline triggers.
#
# Monthly HN Who's Hiring: fires on the 1st of each month at 9am UTC.
# Daily job board adapters: fire at 6am UTC for API-based sources.
# Daily Simplify: fires 6am UTC (GitHub JSON feed).
#
# All targets push to the existing SQS queue so JD Ingestion Agent picks them up.
# Adapters that require company-specific board URLs (greenhouse, lever, ashby)
# are skipped — they need manual SQS messages with specific params.

# =============================================================================
# Monthly — HN Who's Hiring (1st of each month, 9am UTC)
# =============================================================================

resource "aws_cloudwatch_event_rule" "monthly_hn" {
  name                = "${var.project_name}-monthly-hn"
  description         = "Monthly 1st - trigger HN Who's Hiring parser"
  schedule_expression = "cron(0 9 1 * ? *)"

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "monthly_hn_sqs" {
  rule      = aws_cloudwatch_event_rule.monthly_hn.name
  target_id = "hn-hiring-sqs"
  arn       = aws_sqs_queue.jd_scrape.arn
  input     = jsonencode({ source = "hn_hiring", params = {} })
}

# =============================================================================
# Daily — API-based job board adapters (6am UTC)
# =============================================================================

locals {
  daily_adapters = {
    # adzuna   = { query = "software engineer", country = "us" }  # Requires ADZUNA_APP_ID + ADZUNA_APP_KEY
    # jsearch  = { query = "software engineer" }                   # Requires RAPIDAPI_KEY
    # usajobs  = { keyword = "software engineer" }                 # Requires USAJOBS_API_KEY + USAJOBS_EMAIL
    the_muse = { category = "Engineering" }
  }
}

resource "aws_cloudwatch_event_rule" "daily_adapters" {
  for_each = local.daily_adapters

  name                = "${var.project_name}-daily-${each.key}"
  description         = "Daily 6am UTC - fetch jobs from ${each.key}"
  schedule_expression = "cron(0 6 * * ? *)"

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "daily_adapter_sqs" {
  for_each = local.daily_adapters

  rule      = aws_cloudwatch_event_rule.daily_adapters[each.key].name
  target_id = "${each.key}-sqs"
  arn       = aws_sqs_queue.jd_scrape.arn
  input     = jsonencode({ source = each.key, params = each.value })
}

# =============================================================================
# Daily — Simplify (6am UTC, GitHub JSON feed)
# =============================================================================

resource "aws_cloudwatch_event_rule" "weekly_simplify" {
  name                = "${var.project_name}-daily-simplify"
  description         = "Daily 6am UTC - fetch Simplify GitHub listings"
  schedule_expression = "cron(0 6 * * ? *)"

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "weekly_simplify_sqs" {
  rule      = aws_cloudwatch_event_rule.weekly_simplify.name
  target_id = "simplify-sqs"
  arn       = aws_sqs_queue.jd_scrape.arn
  input     = jsonencode({ source = "simplify", params = {} })
}

# =============================================================================
# SQS Queue Policy — allow all EventBridge rules to send messages
# =============================================================================

resource "aws_sqs_queue_policy" "eventbridge_to_sqs" {
  queue_url = aws_sqs_queue.jd_scrape.url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEventBridgeSend"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.jd_scrape.arn
      Condition = {
        ArnEquals = {
          "aws:SourceArn" = concat(
            [aws_cloudwatch_event_rule.monthly_hn.arn],
            [for k, v in local.daily_adapters : aws_cloudwatch_event_rule.daily_adapters[k].arn],
            [aws_cloudwatch_event_rule.weekly_simplify.arn],
          )
        }
      }
    }]
  })
}
