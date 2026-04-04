# --- Networking ---

output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs (ALB placement)"
  value       = module.vpc.public_subnets
}

output "private_subnet_ids" {
  description = "Private subnet IDs (RDS, Lambda, ECS placement)"
  value       = module.vpc.private_subnets
}

output "nat_instance_public_ip" {
  description = "NAT instance public IP (for debugging outbound traffic)"
  value       = aws_instance.nat.public_ip
}

# --- Security Groups ---

output "alb_sg_id" {
  description = "ALB security group ID"
  value       = aws_security_group.alb.id
}

output "ecs_sg_id" {
  description = "ECS Fargate security group ID"
  value       = aws_security_group.ecs.id
}

output "rds_sg_id" {
  description = "RDS PostgreSQL security group ID"
  value       = aws_security_group.rds.id
}

output "nat_sg_id" {
  description = "NAT instance security group ID"
  value       = aws_security_group.nat.id
}

# --- Compute ---

output "alb_dns_name" {
  description = "ALB DNS name - the public URL for the dashboard"
  value       = aws_lb.app.dns_name
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

# --- Bedrock ---

output "bedrock_data_source_id" {
  description = "Bedrock KB data source ID for JDs"
  value       = aws_bedrockagent_data_source.jds.data_source_id
}

# --- SQS ---

output "dlq_arn" {
  description = "Dead-letter queue ARN for failed JD scrape messages"
  value       = aws_sqs_queue.jd_scrape_dlq.arn
}
