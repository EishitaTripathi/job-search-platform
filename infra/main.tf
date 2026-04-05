terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # SETUP: Create this S3 bucket manually before running terraform init:
  #   aws s3 mb s3://YOUR-PROJECT-NAME-tfstate --region us-east-2
  # Then update the bucket name below to match.
  # See SETUP.md "Step 3: AWS Infrastructure" for details.
  backend "s3" {
    bucket = "job-search-platform-tfstate"
    key    = "terraform.tfstate"
    region = "us-east-2"
  }
}

provider "aws" {
  region = var.aws_region
}

# -----------------------------------------------------------------------------
# Data sources
# -----------------------------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  common_tags = {
    Project   = var.project_name
    ManagedBy = "terraform"
  }
}

# -----------------------------------------------------------------------------
# VPC — using terraform-aws-modules/vpc (industry standard, ~190M downloads)
#
# Creates: VPC, 2 public subnets, 2 private subnets, Internet Gateway,
#          route tables, route table associations.
#
# NAT Gateway disabled — using a t3.micro NAT instance instead (free tier,
# $33/mo). Lambda runs ~30 min/day; paying for always-on managed NAT is
# wasteful for a single-user project. NAT instance defined below.
# -----------------------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = var.project_name
  cidr = var.vpc_cidr

  azs             = slice(data.aws_availability_zones.available.names, 0, 2)
  public_subnets  = var.public_subnet_cidrs
  private_subnets = var.private_subnet_cidrs

  enable_nat_gateway   = false # Using NAT instance instead (see below)
  enable_dns_hostnames = true  # Required: RDS SSL certs bind to DNS names, not IPs
  enable_dns_support   = true

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# NAT Instance — t3.nano running Amazon Linux 2023 NAT AMI
#
# Cost: free tier eligible (t3.micro) vs $33/mo for NAT Gateway. Acceptable
# tradeoff for a single-user project. Production would use managed NAT Gateway for HA.
#
# How it works: source/dest check disabled so the instance can forward
# packets from private subnet resources to the internet via IGW.
# CloudWatch auto-recovery restarts the instance if it fails status checks.
# -----------------------------------------------------------------------------

# Look up the latest Amazon Linux 2 AMI for use as a NAT instance.
# amzn-ami-vpc-nat was deprecated in us-east-2, so we use Amazon Linux 2
# and configure iptables NAT via user_data (see aws_instance.nat below).
data "aws_ami" "nat" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn2-ami-hvm-*-x86_64-ebs"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

locals {
  nat_ami_id = data.aws_ami.nat.id
}

resource "aws_security_group" "nat" {
  name        = "${var.project_name}-nat-instance-sg"
  description = " NAT instance - inbound from private-fetch only"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${var.project_name}-nat-instance-sg" })
}

resource "aws_security_group_rule" "nat_ingress_from_private_fetch" {
  security_group_id = aws_security_group.nat.id
  type              = "ingress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = [var.private_subnet_cidrs[0]] # private-fetch only, not private-data
  description       = "All traffic from private-fetch subnet only"
}

resource "aws_security_group_rule" "nat_egress_all" {
  security_group_id = aws_security_group.nat.id
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "All outbound to internet via IGW"
}

resource "aws_instance" "nat" {
  ami                         = local.nat_ami_id
  instance_type               = "t3.micro"
  subnet_id                   = module.vpc.public_subnets[0]
  vpc_security_group_ids      = [aws_security_group.nat.id]
  source_dest_check           = false # Required for NAT — instance forwards packets it didn't originate
  associate_public_ip_address = true

  # Configure iptables NAT forwarding — required because we use a generic
  # Amazon Linux 2 AMI (the dedicated amzn-ami-vpc-nat was deprecated).
  user_data = <<-EOF
    #!/bin/bash
    sysctl -w net.ipv4.ip_forward=1
    echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.d/nat.conf
    iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
    iptables -A FORWARD -i eth0 -o eth0 -m state --state RELATED,ESTABLISHED -j ACCEPT
    iptables -A FORWARD -j ACCEPT
  EOF

  # H2 fix: enforce IMDSv2 — blocks SSRF-based credential theft via metadata endpoint
  metadata_options {
    http_tokens   = "required" # IMDSv2 only (token-based, not vulnerable to SSRF)
    http_endpoint = "enabled"
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-nat-instance" })
}

# -----------------------------------------------------------------------------
# Private subnet routing — privilege separation
#
# private-fetch (10.0.10.0/24): 0.0.0.0/0 → NAT instance (Lambda Fetch — internet, no DB)
# private-data  (10.0.20.0/24): no internet route (Lambda Persist + RDS — DB, no internet)
#
# The VPC module creates one route table per private subnet when
# enable_nat_gateway = false. Index 0 = private-fetch, index 1 = private-data.
# We only add NAT route to index 0.
# -----------------------------------------------------------------------------

resource "aws_route" "private_fetch_nat" {
  route_table_id         = module.vpc.private_route_table_ids[0]
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_instance.nat.primary_network_interface_id
}

# private-data subnet (index 1) intentionally has NO 0.0.0.0/0 route.
# Lambda Persist and RDS can reach S3 via VPC Gateway Endpoint (added in Commit 3)
# but cannot reach the internet. This is the privilege separation boundary.

# -----------------------------------------------------------------------------
# Security Groups — raw resources because these encode our specific
# architecture (ALB → ECS → RDS chain). No generic module can template this.
#
# Rules are separate aws_security_group_rule resources (not inline) to avoid
# circular dependency: ALB egress references ECS SG, ECS ingress references
# ALB SG. Terraform cannot resolve this with inline rules.
# -----------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb-sg"
  description = "LB - inbound HTTP/HTTPS from internet, outbound to ECS only"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${var.project_name}-alb-sg" })
}

resource "aws_security_group" "ecs" {
  name        = "${var.project_name}-ecs-sg"
  description = "ECS Fargate - inbound from ALB, outbound all"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${var.project_name}-ecs-sg" })
}

# Lambda security groups REMOVED — JD ingestion now in ECS.

resource "aws_security_group" "rds" {
  name        = "${var.project_name}-rds-sg"
  description = "RDS PostgreSQL - inbound 5432 from ECS and Lambda Persist only"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${var.project_name}-rds-sg" })
}

# --- ALB rules ---

resource "aws_security_group_rule" "alb_ingress_http" {
  security_group_id = aws_security_group.alb.id
  type              = "ingress"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "HTTP from internet"
}

resource "aws_security_group_rule" "alb_ingress_https" {
  security_group_id = aws_security_group.alb.id
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "HTTPS from internet"
}

resource "aws_security_group_rule" "alb_egress_to_ecs" {
  security_group_id        = aws_security_group.alb.id
  type                     = "egress"
  from_port                = 0
  to_port                  = 0
  protocol                 = "-1"
  source_security_group_id = aws_security_group.ecs.id
  description              = "All traffic to ECS tasks only"
}

# --- ECS rules ---

resource "aws_security_group_rule" "ecs_ingress_from_alb" {
  security_group_id        = aws_security_group.ecs.id
  type                     = "ingress"
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  description              = "HTTP 8080 from ALB (non-root container port)"
}

resource "aws_security_group_rule" "ecs_egress_all" {
  security_group_id = aws_security_group.ecs.id
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "All outbound - RDS, S3, SQS, Secrets Manager via NAT instance"
}

# Lambda Fetch + Persist security group rules REMOVED — JD ingestion now in ECS.

data "aws_ec2_managed_prefix_list" "s3" {
  filter {
    name   = "prefix-list-name"
    values = ["com.amazonaws.${var.aws_region}.s3"]
  }
}

# --- RDS rules ---

resource "aws_security_group_rule" "rds_ingress_from_ecs" {
  security_group_id        = aws_security_group.rds.id
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ecs.id
  description              = "PostgreSQL from ECS tasks"
}
