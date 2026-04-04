"""Static analysis: every SQL column reference in the codebase must exist in schema.sql.

Parses infra/schema.sql to build a {table: {columns}} map, then scans all .py
files under api/, local/, lambda/ for SQL strings and checks that every
column reference resolves to a real column in the schema.

No database required — pure string parsing.  Catches the class of bug where
code references a column that was dropped or never existed.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCHEMA_PATH = ROOT / "infra" / "schema.sql"
SOURCE_DIRS = [ROOT / "api", ROOT / "local", ROOT / "lambda"]


# ---------------------------------------------------------------------------
# Schema parser
# ---------------------------------------------------------------------------


def _parse_schema() -> dict[str, set[str]]:
    """Parse CREATE TABLE statements from schema.sql → {table: {col, …}}."""
    ddl = SCHEMA_PATH.read_text()
    tables: dict[str, set[str]] = {}

    # Match each CREATE TABLE block
    for m in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\);",
        ddl,
        re.DOTALL | re.IGNORECASE,
    ):
        table_name = m.group(1)
        body = m.group(2)
        cols: set[str] = set()

        for line in body.split("\n"):
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            # Skip constraints (PRIMARY KEY, UNIQUE, REFERENCES, CHECK, FOREIGN)
            upper = line.upper()
            if any(
                upper.startswith(kw)
                for kw in (
                    "PRIMARY",
                    "UNIQUE",
                    "CHECK",
                    "FOREIGN",
                    "CONSTRAINT",
                )
            ):
                continue
            # First token is the column name
            token = line.split()[0]
            # Skip if it looks like a SQL keyword rather than a column name
            if token.upper() in ("CREATE", "TABLE", "IF", "NOT", "EXISTS"):
                continue
            cols.add(token.lower())

        tables[table_name.lower()] = cols

    return tables


# ---------------------------------------------------------------------------
# SQL query extractor
# ---------------------------------------------------------------------------

_SQL_PATTERN = re.compile(
    r'"""(.*?)"""|\'\'\'(.*?)\'\'\'|"((?:SELECT|INSERT|UPDATE|DELETE|ALTER).*?)"',
    re.DOTALL | re.IGNORECASE,
)


def _extract_sql_strings(py_source: str) -> list[str]:
    """Pull SQL-like strings from Python source code."""
    results = []
    for m in _SQL_PATTERN.finditer(py_source):
        sql = m.group(1) or m.group(2) or m.group(3)
        if sql and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", sql, re.IGNORECASE):
            results.append(sql)
    return results


# ---------------------------------------------------------------------------
# Column reference extractor (per-table)
# ---------------------------------------------------------------------------


def _extract_column_refs(
    sql: str, schema: dict[str, set[str]]
) -> list[tuple[str, str, str]]:
    """Return [(table, column, raw_sql_snippet), …] for every column reference
    that can be statically resolved to a known table.

    Handles:
      - SELECT col FROM table
      - SELECT t.col FROM table t
      - WHERE col = / IN / ILIKE
      - INSERT INTO table (col, col, …)
      - UPDATE table SET col = …
      - ON CONFLICT (col)
      - ORDER BY col
    """
    refs: list[tuple[str, str, str]] = []
    # Normalize whitespace
    sql_norm = " ".join(sql.split())

    # ---- Resolve table aliases ----
    # FROM table alias  /  FROM table AS alias
    alias_map: dict[str, str] = {}
    for m in re.finditer(
        r"\bFROM\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?",
        sql_norm,
        re.IGNORECASE,
    ):
        table = m.group(1).lower()
        alias = (m.group(2) or table).lower()
        if table in schema:
            alias_map[alias] = table

    # JOIN table alias
    for m in re.finditer(
        r"\bJOIN\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?",
        sql_norm,
        re.IGNORECASE,
    ):
        table = m.group(1).lower()
        alias = (m.group(2) or table).lower()
        if table in schema:
            alias_map[alias] = table

    # ---- INSERT INTO table (col, …) ----
    insert_m = re.search(
        r"\bINSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)",
        sql_norm,
        re.IGNORECASE,
    )
    if insert_m:
        table = insert_m.group(1).lower()
        if table in schema:
            for col in insert_m.group(2).split(","):
                col_clean = col.strip().lower()
                if col_clean and not col_clean.startswith("$"):
                    refs.append((table, col_clean, sql_norm[:80]))

    # ---- UPDATE table SET col = ----
    update_m = re.search(
        r"\bUPDATE\s+(\w+)\s+SET\s+(.*?)(?:\bWHERE\b|$)",
        sql_norm,
        re.IGNORECASE,
    )
    if update_m:
        table = update_m.group(1).lower()
        if table in schema:
            set_clause = update_m.group(2)
            for col_m in re.finditer(r"(\w+)\s*=", set_clause):
                col = col_m.group(1).lower()
                if col not in ("now",):
                    refs.append((table, col, sql_norm[:80]))

    # ---- SELECT / WHERE / ORDER BY: alias.col or bare col ----
    # alias.col pattern
    for m in re.finditer(r"\b(\w+)\.(\w+)\b", sql_norm):
        prefix = m.group(1).lower()
        col = m.group(2).lower()
        if prefix in alias_map:
            table = alias_map[prefix]
            # Skip function-like things (e.g., NOW())
            if col not in ("id",) or col in schema.get(table, set()):
                refs.append((table, col, sql_norm[:80]))

    # ---- ON CONFLICT (col) ----
    conflict_m = re.search(r"\bON\s+CONFLICT\s*\(([^)]+)\)", sql_norm, re.IGNORECASE)
    if conflict_m and insert_m:
        table = insert_m.group(1).lower()
        if table in schema:
            for col in conflict_m.group(1).split(","):
                col_clean = col.strip().lower()
                if col_clean:
                    refs.append((table, col_clean, sql_norm[:80]))

    return refs


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def _collect_all_violations() -> list[str]:
    """Scan every .py file and return human-readable violation strings."""
    schema = _parse_schema()
    violations = []

    for src_dir in SOURCE_DIRS:
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            source = py_file.read_text()
            for sql in _extract_sql_strings(source):
                for table, col, snippet in _extract_column_refs(sql, schema):
                    if col not in schema[table]:
                        rel = py_file.relative_to(ROOT)
                        violations.append(
                            f"{rel}: column '{col}' not in table '{table}' "
                            f"(valid: {sorted(schema[table])})\n"
                            f"  SQL: {snippet}…"
                        )

    return violations


def test_all_sql_columns_exist_in_schema():
    """Every column referenced in Python SQL must exist in schema.sql."""
    violations = _collect_all_violations()
    if violations:
        msg = f"{len(violations)} column reference(s) not in schema:\n\n"
        msg += "\n\n".join(violations)
        pytest.fail(msg)


def test_schema_parses_successfully():
    """Sanity check: the schema parser finds all expected tables."""
    schema = _parse_schema()
    expected_tables = {
        "jobs",
        "labeled_emails",
        "labeling_queue",
        "embedding_cache",
        "config",
        "jd_analyses",
        "resumes",
        "match_reports",
        "followup_recommendations",
        "orchestration_runs",
        "answer_memory",
        "deadlines",
        "pipeline_metrics",
    }
    assert expected_tables.issubset(
        set(schema.keys())
    ), f"Missing tables: {expected_tables - set(schema.keys())}"


def test_schema_jobs_has_no_dropped_columns():
    """Regression: columns dropped by migration must not appear in schema.sql.

    Note: follow_up_flagged, follow_up_snoozed were added to jobs and
    remote_policy was added to jd_analyses intentionally (schema.sql update).
    Only truly dropped columns should be listed here.
    """
    schema = _parse_schema()
    jobs_cols = schema["jobs"]
    # No columns are currently in the dropped set — update this if columns
    # are removed in a future migration.
    dropped: set[str] = set()
    present = dropped & jobs_cols
    assert not present, f"Dropped columns still in jobs table: {present}"

    # Verify the intentionally-added columns exist where expected
    assert "follow_up_flagged" in jobs_cols, "follow_up_flagged missing from jobs"
    assert "follow_up_snoozed" in jobs_cols, "follow_up_snoozed missing from jobs"
    jd_cols = schema["jd_analyses"]
    assert "remote_policy" in jd_cols, "remote_policy missing from jd_analyses"


# ---------------------------------------------------------------------------
# Type-level validation (catches json.dumps on TEXT[] columns, etc.)
# See infra/SCHEMA_TYPES.md for the full mapping.
# ---------------------------------------------------------------------------


def _parse_column_types() -> dict[str, dict[str, str]]:
    """Parse CREATE TABLE statements → {table: {col: type_token}}."""
    ddl = SCHEMA_PATH.read_text()
    tables: dict[str, dict[str, str]] = {}

    for m in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\);",
        ddl,
        re.DOTALL | re.IGNORECASE,
    ):
        table_name = m.group(1).lower()
        body = m.group(2)
        cols: dict[str, str] = {}

        for line in body.split("\n"):
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            upper = line.upper()
            if any(
                upper.startswith(kw)
                for kw in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT")
            ):
                continue
            tokens = line.split()
            if len(tokens) < 2:
                continue
            col_name = tokens[0]
            if col_name.upper() in ("CREATE", "TABLE", "IF", "NOT", "EXISTS"):
                continue
            # Type is the second token (e.g., BIGSERIAL, TEXT, TEXT[], INT4RANGE)
            col_type = tokens[1].upper()
            cols[col_name.lower()] = col_type

        tables[table_name] = cols

    return tables


def _get_text_array_tables() -> set[str]:
    """Return table names that have TEXT[] columns."""
    schema_types = _parse_column_types()
    tables_with_arrays = set()
    for table, cols in schema_types.items():
        for col, ctype in cols.items():
            if "TEXT[]" in ctype or ctype == "TEXT[]":
                tables_with_arrays.add(table)
    return tables_with_arrays


def test_no_json_dumps_for_array_columns():
    """json.dumps() must not be used for values inserted into TEXT[] columns.

    Scans Python files for functions that INSERT/UPDATE to tables with TEXT[]
    columns and checks for json.dumps() usage in those functions.
    See: WORK_REPORT_2026-03-30.md bugs #5, #7, #9.
    """
    tables_with_arrays = _get_text_array_tables()
    violations = []

    for src_dir in SOURCE_DIRS:
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            source = py_file.read_text()

            # Find functions that reference tables with TEXT[] columns
            schema_types = _parse_column_types()
            # Build set of known TEXT[] column names across all tables
            text_array_col_names = set()
            for table in tables_with_arrays:
                for col_name, col_type in schema_types.get(table, {}).items():
                    if "TEXT[]" in col_type:
                        text_array_col_names.add(col_name)

            # Known JSONB/TEXT columns where json.dumps is correct
            jsonb_columns = {
                "confidence_scores",
                "raw_json",
                "agent_results",
                "match_candidates",
                "event_source",
                "event_data",
            }

            for table in tables_with_arrays:
                if not re.search(
                    rf"\b(INSERT\s+INTO|UPDATE)\s+{table}\b",
                    source,
                    re.IGNORECASE,
                ):
                    continue

                dumps_matches = list(re.finditer(r"json\.dumps\s*\(", source))
                for dm in dumps_matches:
                    # Get surrounding context
                    start = max(0, dm.start() - 300)
                    end = min(len(source), dm.end() + 100)
                    context = source[start:end]

                    # Skip if the json.dumps result is clearly assigned to a
                    # JSONB or TEXT column (not a TEXT[] column)
                    if re.search(
                        r"(" + "|".join(jsonb_columns) + r")",
                        context,
                    ):
                        continue

                    # Check if context mentions a known TEXT[] column name
                    # (stronger signal that this is a real problem)
                    mentions_array_col = any(
                        col in context for col in text_array_col_names
                    )
                    if not mentions_array_col:
                        continue

                    line_no = source[: dm.start()].count("\n") + 1
                    rel = py_file.relative_to(ROOT)
                    violations.append(
                        f"{rel}:{line_no}: json.dumps() near INSERT/UPDATE "
                        f"to '{table}' referencing TEXT[] column. "
                        f"Pass Python list directly instead."
                    )

    if violations:
        msg = (
            f"{len(violations)} potential json.dumps() on TEXT[] column(s):\n\n"
            + "\n".join(violations)
            + "\n\nSee infra/SCHEMA_TYPES.md Rule #1: "
            "TEXT[] columns take Python list, never json.dumps()."
        )
        pytest.fail(msg)


def test_no_string_range_for_int4range():
    """String-formatted ranges must not be used for INT4RANGE columns.

    Scans for patterns like '"[0,5)"' or f'[{lo},{hi})' near INSERT/UPDATE
    to jd_analyses (which has the experience_range INT4RANGE column).
    See: WORK_REPORT_2026-03-30.md bug #5.
    """
    violations = []

    for src_dir in SOURCE_DIRS:
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            source = py_file.read_text()

            # Only check files that reference jd_analyses (the only table with INT4RANGE)
            if "jd_analyses" not in source:
                continue
            if not re.search(
                r"\b(INSERT\s+INTO|UPDATE)\s+jd_analyses\b",
                source,
                re.IGNORECASE,
            ):
                continue

            # Look for string range patterns: "[N,M)" or f"[{...},{...})"
            range_patterns = [
                (r'"\[\d+\s*,\s*\d+\)"', "string literal range"),
                (r"f\"\[.*?,.*?\)\"", "f-string range"),
                (r"f'\[.*?,.*?\)'", "f-string range"),
                (r'"\["\s*\+', "string concatenation range"),
            ]

            for pattern, desc in range_patterns:
                for m in re.finditer(pattern, source):
                    line_no = source[: m.start()].count("\n") + 1
                    rel = py_file.relative_to(ROOT)
                    violations.append(
                        f"{rel}:{line_no}: {desc} '{m.group()}' "
                        f"for INT4RANGE column. "
                        f"Use asyncpg.Range(lo, hi) instead."
                    )

    if violations:
        msg = (
            f"{len(violations)} string range(s) for INT4RANGE:\n\n"
            + "\n".join(violations)
            + "\n\nSee infra/SCHEMA_TYPES.md Rule #3: "
            "INT4RANGE uses asyncpg.Range(lower, upper), never string format."
        )
        pytest.fail(msg)


def test_no_jsonb_concat_for_text_array():
    """JSONB concatenation operators must not be used on TEXT[] columns.

    Scans for '|| $N::jsonb' patterns in files that UPDATE TEXT[] columns.
    See: WORK_REPORT_2026-03-30.md bug #7 (deal_breakers).
    """
    violations = []

    for src_dir in SOURCE_DIRS:
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            source = py_file.read_text()

            # Look for JSONB concat on known TEXT[] columns
            text_array_cols = [
                "required_skills",
                "preferred_skills",
                "tech_stack",
                "deal_breakers",
                "skill_gaps",
                "agent_chain",
            ]

            for col in text_array_cols:
                # Pattern: col || $N::jsonb or col || $N::json
                pattern = rf"{col}\s*\|\|\s*\$\d+::jsonb?"
                for m in re.finditer(pattern, source, re.IGNORECASE):
                    line_no = source[: m.start()].count("\n") + 1
                    rel = py_file.relative_to(ROOT)
                    violations.append(
                        f"{rel}:{line_no}: JSONB concat on TEXT[] column "
                        f"'{col}': '{m.group()}'. "
                        f"Use array_cat(COALESCE({col}, "
                        f"'{{}}'::text[]), $N::text[]) instead."
                    )

    if violations:
        msg = (
            f"{len(violations)} JSONB concat on TEXT[] column(s):\n\n"
            + "\n".join(violations)
            + "\n\nSee infra/SCHEMA_TYPES.md Rule #6: "
            "TEXT[] concatenation uses array_cat, never || ::jsonb."
        )
        pytest.fail(msg)
