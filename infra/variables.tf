variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-2"
}

variable "s3_bucket_name" {
  description = "S3 bucket for JD storage. Create manually before terraform apply. See SETUP.md."
  type        = string

  validation {
    condition     = var.s3_bucket_name != ""
    error_message = "s3_bucket_name is required. Create an S3 bucket and set this in terraform.tfvars."
  }
}

variable "project_name" {
  description = "Project name used for resource naming and tagging"
  type        = string
  default     = "job-search-platform"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC (10.0.0.0/16 = 65,536 IPs)"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets (ALB, NAT instance)"
  type        = list(string)
  default     = ["10.0.0.0/20", "10.0.16.0/20"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets. Index 0 = private-fetch (NAT route, internet, no DB). Index 1 = private-data (no internet, DB access)."
  type        = list(string)
  default     = ["10.0.128.0/20", "10.0.144.0/20"]
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN for HTTPS on ALB. Empty string = HTTP only (dev mode)."
  type        = string
  default     = ""
}

variable "alb_access_logs_bucket" {
  description = "S3 bucket name for ALB access logs. Empty string = logging disabled."
  type        = string
  default     = ""
}

variable "bedrock_kb_id" {
  description = "Bedrock Knowledge Base ID. Create manually in console with Titan Embed v2. See SETUP.md."
  type        = string

  validation {
    condition     = var.bedrock_kb_id != ""
    error_message = "bedrock_kb_id is required. Create a Bedrock Knowledge Base and set this in terraform.tfvars."
  }
}
