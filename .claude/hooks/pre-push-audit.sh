#!/bin/bash
# pre-push-audit.sh — PreToolUse hook on Bash (git push only)
# Runs deployment audit checks before allowing a push.
# See DEPLOYMENT_AUDIT.md for the full checklist.
#
# Exit 0 = allow, Exit 2 = block

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

# Only activate on git push commands
if ! echo "$COMMAND" | grep -qi "git push"; then
    exit 0
fi

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

FAILURES=""

# 1. Terraform validate
if [ -d infra ] && command -v terraform &>/dev/null; then
    if ! (cd infra && terraform validate) >/dev/null 2>&1; then
        FAILURES="${FAILURES}\n  - terraform validate: Failed. Fix Terraform syntax before pushing."
    fi
fi

# 2. Test suite (quick re-check)
if ! python -m pytest tests/ -q --tb=line 2>/dev/null | tail -1 | grep -q "passed"; then
    FAILURES="${FAILURES}\n  - pytest: Test suite has failures. Fix before pushing."
fi

if [ -n "$FAILURES" ]; then
    echo "BLOCKED: Pre-push deployment audit failed:" >&2
    echo -e "$FAILURES" >&2
    echo "" >&2
    echo "Fix all issues above before pushing. See DEPLOYMENT_AUDIT.md." >&2
    exit 2
fi

echo "Pre-push audit: all checks passed." >&2
exit 0
