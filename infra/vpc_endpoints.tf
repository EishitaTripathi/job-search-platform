# VPC Endpoints — allow private-data subnet resources to reach AWS services
# without internet access.
#
# S3 Gateway Endpoint: already created in console (free, no hourly charge).
# Secrets Manager Interface Endpoint: required because Lambda Persist is in
# private-data subnet (no NAT route) but needs DB creds from Secrets Manager.
#
# Interface endpoints cost ~$7.20/mo per AZ. We deploy in 1 AZ to minimize cost.

# S3 Gateway Endpoint (reference only — already created in console)
# data "aws_vpc_endpoint" "s3" {
#   vpc_id       = module.vpc.vpc_id
#   service_name = "com.amazonaws.${var.aws_region}.s3"
# }

# Secrets Manager Interface Endpoint — Lambda Persist fetches DB creds here
resource "aws_security_group" "vpc_endpoint" {
  name        = "${var.project_name}-vpce-sg"
  description = "VPC Interface Endpoints - HTTPS from Lambda Persist and ECS"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${var.project_name}-vpce-sg" })
}

# Lambda Persist VPC endpoint rule removed — JD ingestion now in ECS.

resource "aws_security_group_rule" "vpce_ingress_from_ecs" {
  security_group_id        = aws_security_group.vpc_endpoint.id
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ecs.id
  description              = "HTTPS from ECS tasks"
}

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true # SDK calls secretsmanager.us-east-2.amazonaws.com resolve to VPC endpoint IP

  subnet_ids         = [module.vpc.private_subnets[1]] # private-data subnet only
  security_group_ids = [aws_security_group.vpc_endpoint.id]

  tags = merge(local.common_tags, { Name = "${var.project_name}-secretsmanager-vpce" })
}

# CloudWatch Logs endpoint — Lambda Persist needs to ship logs without internet
resource "aws_vpc_endpoint" "logs" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.logs"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true

  subnet_ids         = [module.vpc.private_subnets[1]]
  security_group_ids = [aws_security_group.vpc_endpoint.id]

  tags = merge(local.common_tags, { Name = "${var.project_name}-logs-vpce" })
}
