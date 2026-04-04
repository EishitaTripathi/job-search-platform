#!/bin/bash
# block-sensitive-reads.sh — PreToolUse hook on Read
# Blocks Claude Code from reading sensitive files.
# See AWS_STATE.md Section 6 for the full list.
#
# Exit 0 = allow, Exit 2 = block

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

BASENAME=$(basename "$FILE_PATH")
DIRNAME=$(dirname "$FILE_PATH")

# Block .env files (contain JWT_SECRET, DATABASE_URL, API keys)
if [[ "$BASENAME" == ".env" || "$BASENAME" == .env.* ]] && [[ "$BASENAME" != ".env.example" ]]; then
    echo "BLOCKED: Cannot read $BASENAME (contains secrets). Use .env.example for reference." >&2
    exit 2
fi

# Block Gmail OAuth credentials
if [[ "$BASENAME" == "credentials.json" || "$BASENAME" == "token.json" ]]; then
    echo "BLOCKED: Cannot read $BASENAME (Gmail OAuth credentials)." >&2
    exit 2
fi

# Block TLS keys/certificates
if [[ "$BASENAME" == *.pem || "$BASENAME" == *.key ]]; then
    echo "BLOCKED: Cannot read $BASENAME (TLS certificate/key)." >&2
    exit 2
fi

# Block Terraform state (contains RDS passwords, resource secrets)
if [[ "$BASENAME" == *.tfstate || "$BASENAME" == *.tfstate.backup ]]; then
    echo "BLOCKED: Cannot read $BASENAME (Terraform state contains secrets). Use 'terraform output' for values." >&2
    exit 2
fi

# Block Terraform variable files (may contain secrets)
if [[ "$BASENAME" == *.tfvars || "$BASENAME" == *.tfvars.json ]]; then
    echo "BLOCKED: Cannot read $BASENAME (may contain secret variable values)." >&2
    exit 2
fi

# Block AWS credentials
if [[ "$FILE_PATH" == *"/.aws/credentials"* || "$FILE_PATH" == *"/.aws/config"* ]]; then
    echo "BLOCKED: Cannot read AWS credentials file." >&2
    exit 2
fi

exit 0
