#!/bin/bash
# pre-commit-audit.sh — PreToolUse hook on Bash (git commit only)
# Runs automated security audit checks before allowing a commit.
# See CLAUDE.md Pre-Commit Security Audit for the full 16-point checklist.
#
# Exit 0 = allow, Exit 2 = block

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

# Only activate on git commit commands
if ! echo "$COMMAND" | grep -qi "git commit"; then
    exit 0
fi

# Skip if this is a git commit --amend or other non-standard commit
if echo "$COMMAND" | grep -qi "\-\-amend"; then
    echo "WARNING: Amending commits. Ensure security audit was run on the original commit." >&2
    exit 0
fi

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

FAILURES=""

# 1. Secrets scan
if command -v pre-commit &>/dev/null; then
    if ! pre-commit run detect-secrets --all-files >/dev/null 2>&1; then
        FAILURES="${FAILURES}\n  - detect-secrets: Found potential secrets in staged files"
    fi
fi

# 2. SQL injection scan
SQL_INJECT=$(grep -rn 'f".*SELECT\|f".*INSERT\|f".*UPDATE\|\.format.*SELECT' api/ local/ 2>/dev/null || true)
if [ -n "$SQL_INJECT" ]; then
    FAILURES="${FAILURES}\n  - SQL injection: Found f-string/format SQL patterns:\n${SQL_INJECT}"
fi

# 3. SOURCES.yaml validation
if [ -f SOURCES.yaml ]; then
    if ! python3 -c "import yaml; yaml.safe_load(open('SOURCES.yaml'))" 2>/dev/null; then
        FAILURES="${FAILURES}\n  - SOURCES.yaml: Invalid YAML syntax"
    fi
fi

# 4. TODO/FIXME scan (warning only, does not block)
TODO_COUNT=$(grep -rn "TODO\|FIXME\|HACK\|XXX" api/ local/ infra/ --include="*.py" --include="*.tf" 2>/dev/null | wc -l | tr -d ' ')
if [ "$TODO_COUNT" -gt 0 ]; then
    echo "NOTE: Found $TODO_COUNT TODO/FIXME/HACK markers. Review before claiming deployment-ready." >&2
fi

# 5. Test suite
if ! python -m pytest tests/ -v --tb=short -q 2>/dev/null | tail -1 | grep -q "passed"; then
    FAILURES="${FAILURES}\n  - pytest: Test suite has failures"
fi

# 6. Requirements comparison gate
# If agent code changed (graph.py, tools.py, main.py), REQUIREMENTS.md must also be staged.
AGENT_CHANGES=$(git diff --cached --name-only | grep -E "(agents/.*graph\.py|agents/.*tools\.py|api/main\.py|infra/schema\.sql)" || true)
if [ -n "$AGENT_CHANGES" ]; then
    REQ_STAGED=$(git diff --cached --name-only | grep "REQUIREMENTS.md" || true)
    if [ -z "$REQ_STAGED" ]; then
        FAILURES="${FAILURES}\n  - REQUIREMENTS.md: Agent/schema code changed but REQUIREMENTS.md not updated. Per CLAUDE.md Plan Acceptance: identify affected FR-*/NFR-* and update REQUIREMENTS.md."
    fi
fi

# 7. Auth coverage: every @app route (except health/login/root) must have require_auth or require_hmac_auth
# Use grep -A8 to check the function signature (auth is in Depends() on a subsequent line)
UNAUTHED=""
while IFS= read -r line; do
    lineno=$(echo "$line" | cut -d: -f1)
    # Skip health, login, and root redirect
    if echo "$line" | grep -qi "health\|login\|\"/\""; then
        continue
    fi
    # Check next 8 lines for require_auth or require_hmac_auth
    if ! sed -n "${lineno},$((lineno+8))p" api/main.py 2>/dev/null | grep -q "require_auth\|require_hmac_auth"; then
        UNAUTHED="${UNAUTHED}\n${line}"
    fi
done < <(grep -n "@app\.\(get\|post\|patch\|delete\)" api/main.py 2>/dev/null || true)
if [ -n "$UNAUTHED" ]; then
    FAILURES="${FAILURES}\n  - Auth coverage: Unprotected endpoints found:${UNAUTHED}"
fi

# 8. sanitize_for_prompt: every invoke_model call must use sanitize_for_prompt
UNSANITIZED=$(grep -rn "invoke_model" api/agents/ --include="*.py" 2>/dev/null | grep -v "sanitize_for_prompt\|__pycache__\|import\|def \|bedrock_client\.py\|async_invoke_model" || true)
if [ -n "$UNSANITIZED" ]; then
    FAILURES="${FAILURES}\n  - Prompt injection: invoke_model() calls without sanitize_for_prompt():\n${UNSANITIZED}"
fi

if [ -n "$FAILURES" ]; then
    echo "BLOCKED: Pre-commit security audit failed:" >&2
    echo -e "$FAILURES" >&2
    echo "" >&2
    echo "Fix all issues above before committing. See CLAUDE.md Pre-Commit Security Audit." >&2
    exit 2
fi

echo "Pre-commit audit: all automated checks passed." >&2
exit 0
