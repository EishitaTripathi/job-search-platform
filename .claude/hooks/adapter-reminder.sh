#!/bin/bash
# adapter-reminder.sh — PostToolUse hook on Edit/Write
# Reminds to update SOURCES.yaml when adapter files are modified.
# Non-blocking (always exit 0).

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# Only remind for adapter files
if echo "$FILE_PATH" | grep -q "lambda/fetch/adapters/"; then
    echo "REMINDER: Adapter file changed. Update SOURCES.yaml if you added/modified a source." >&2
fi

exit 0
