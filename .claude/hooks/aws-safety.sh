#!/bin/bash
# aws-safety.sh — PreToolUse hook on Bash
# Blocks dangerous AWS CLI commands and Terraform operations.
# See AWS_STATE.md Sections 4-5 for the full rules.
#
# Exit 0 = allow (or warn), Exit 2 = block

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

if [ -z "$COMMAND" ]; then
    exit 0
fi

# --- HARD BLOCKS (exit 2) ---

# Force flags — NEVER use --force, -f (with git add/push/rm), --force-with-lease, --no-verify
if echo "$COMMAND" | grep -qiE "git (push|reset|checkout|clean|branch).*--force|git push.*--force-with-lease|git add -f |git add --force|git rm -f |--no-verify"; then
    echo "BLOCKED: Force commands are forbidden. No --force, -f, --force-with-lease, or --no-verify." >&2
    echo "If a file is gitignored, it should stay untracked. If it needs to be committed, remove it from .gitignore first." >&2
    exit 2
fi

# Terraform destroy
if echo "$COMMAND" | grep -qi "terraform destroy"; then
    echo "BLOCKED: terraform destroy is forbidden. See AWS_STATE.md Section 5." >&2
    exit 2
fi

# Terraform apply without review
if echo "$COMMAND" | grep -qi "terraform apply.*-auto-approve"; then
    echo "BLOCKED: terraform apply -auto-approve is forbidden. Always review the plan." >&2
    exit 2
fi

# Security group ingress modifications
if echo "$COMMAND" | grep -qi "aws ec2 authorize-security-group-ingress"; then
    echo "BLOCKED: Opening security groups violates network isolation. See AWS_STATE.md Section 4. NEVER open NAT/RDS/Lambda SGs to internet." >&2
    exit 2
fi

# Security group egress modifications
if echo "$COMMAND" | grep -qi "aws ec2 revoke-security-group-egress"; then
    echo "BLOCKED: Removing egress rules can break VPC endpoint routing. See AWS_STATE.md Section 4." >&2
    exit 2
fi

# EC2 instance attribute modification (NAT instance protection)
if echo "$COMMAND" | grep -qi "aws ec2 modify-instance-attribute"; then
    echo "BLOCKED: NAT instance configuration is security-critical. See AWS_STATE.md Section 4." >&2
    exit 2
fi

# IAM policy changes (require ADR)
if echo "$COMMAND" | grep -qiE "aws iam (attach-.*-policy|put-.*-policy|create-policy)"; then
    echo "BLOCKED: IAM changes require an ADR. See docs/adr/ and AWS_STATE.md Section 5." >&2
    exit 2
fi

# RDS public access
if echo "$COMMAND" | grep -qi "aws rds modify-db-instance.*--publicly-accessible"; then
    echo "BLOCKED: Making RDS public is FORBIDDEN. See AWS_STATE.md Section 4." >&2
    exit 2
fi

# Recursive S3 deletion
if echo "$COMMAND" | grep -qi "aws s3 rm.*--recursive"; then
    echo "BLOCKED: Recursive S3 deletion risks data loss. See AWS_STATE.md Section 5." >&2
    exit 2
fi

# ECS service shutdown
if echo "$COMMAND" | grep -qi "aws ecs update-service.*--desired-count 0"; then
    echo "BLOCKED: This shuts down the ECS service. See AWS_STATE.md Section 5." >&2
    exit 2
fi

# Lambda deletion
if echo "$COMMAND" | grep -qi "aws lambda delete-function"; then
    echo "BLOCKED: Lambda function deletion. See AWS_STATE.md Section 5." >&2
    exit 2
fi

# Any command adding 0.0.0.0/0 (except reading existing ALB SG)
if echo "$COMMAND" | grep -q "0\.0\.0\.0/0" && ! echo "$COMMAND" | grep -qi "describe"; then
    echo "BLOCKED: Adding 0.0.0.0/0 to any resource except ALB is forbidden. See AWS_STATE.md Section 4." >&2
    exit 2
fi

# Secret exposure via shell
if echo "$COMMAND" | grep -qiE "(cat \.env|echo \\\$JWT_SECRET|echo \\\$DATABASE_URL|echo \\\$APP_PASSWORD|printenv)"; then
    echo "BLOCKED: Exposing secrets via shell output. Read .env.example for variable names." >&2
    exit 2
fi

# --- WARNINGS (exit 0 with stderr) ---

if echo "$COMMAND" | grep -qi "aws lambda update-function-code"; then
    echo "WARNING: Verify build artifacts match target platform (manylinux2014_x86_64, python 3.11) before deploying." >&2
    exit 0
fi

if echo "$COMMAND" | grep -qi "terraform apply" && ! echo "$COMMAND" | grep -qi "\-auto-approve"; then
    echo "WARNING: Review terraform plan output carefully. Check for SG/IAM changes." >&2
    exit 0
fi

if echo "$COMMAND" | grep -qi "aws ecs update-service" && ! echo "$COMMAND" | grep -qi "\-\-desired-count 0"; then
    echo "WARNING: Check current task count and image SHA before updating ECS service." >&2
    exit 0
fi

exit 0
