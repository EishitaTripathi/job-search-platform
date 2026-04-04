#!/bin/bash
# schema-type-warning.sh — PreToolUse hook on Edit
# Warns when editing agent tool files about schema type mappings.
# Non-blocking (always exit 0).

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# Only warn for agent tool files
if echo "$FILE_PATH" | grep -qE "agents/.*/tools\.py$"; then
    cat >&2 <<'EOF'

SCHEMA TYPE REMINDER (infra/SCHEMA_TYPES.md):
  TEXT[]    -> Python list[str], NEVER json.dumps()
  JSONB    -> Python dict, pass directly
  INT4RANGE -> asyncpg.Range(lo, hi), NEVER string "[lo,hi)"
  TEXT[] concat -> array_cat(COALESCE(col, '{}'::text[]), $N::text[])

EOF
fi

exit 0
