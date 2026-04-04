"""Runtime schema comparison — verifies live DB matches infra/schema.sql.

Reuses the regex-based DDL parser from tests/test_sql_schema_sync.py and
adds an async function that compares the parsed DDL against the live
information_schema in PostgreSQL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def parse_schema_file(schema_path: Path) -> dict[str, set[str]]:
    """Parse CREATE TABLE statements from schema.sql -> {table: {col, ...}}.

    This is the same logic as ``_parse_schema()`` in
    ``tests/test_sql_schema_sync.py`` but accepts an explicit *schema_path*
    so it can be called from the runtime health-check layer.
    """
    ddl = schema_path.read_text()
    tables: dict[str, set[str]] = {}

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
            token = line.split()[0]
            if token.upper() in (
                "PRIMARY",
                "UNIQUE",
                "CHECK",
                "FOREIGN",
                "CONSTRAINT",
                "CREATE",
                "INDEX",
            ):
                continue
            cols.add(token.lower())

        tables[table_name.lower()] = cols

    return tables


async def check_schema_match(
    conn,
    schema_path: Path,
) -> dict[str, Any]:
    """Compare live DB tables/columns against *schema_path* DDL.

    Parameters
    ----------
    conn : asyncpg.Connection
        An already-acquired asyncpg connection.
    schema_path : Path
        Path to ``infra/schema.sql``.

    Returns
    -------
    dict
        ``{"match": bool,
           "missing_tables": [...],
           "extra_tables": [...],
           "column_mismatches": {table: {"missing": [...], "extra": [...]}}}``
    """
    expected = parse_schema_file(schema_path)
    expected_tables = set(expected.keys())

    # --- Live tables ---
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    live_tables = {r["table_name"] for r in rows}

    missing_tables = sorted(expected_tables - live_tables)
    extra_tables = sorted(live_tables - expected_tables)

    # --- Per-table column diff (only for tables that exist in both) ---
    column_mismatches: dict[str, dict[str, list[str]]] = {}
    for table in sorted(expected_tables & live_tables):
        col_rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1",
            table,
        )
        live_cols = {r["column_name"] for r in col_rows}
        expected_cols = expected[table]

        missing_cols = sorted(expected_cols - live_cols)
        extra_cols = sorted(live_cols - expected_cols)

        if missing_cols or extra_cols:
            column_mismatches[table] = {
                "missing": missing_cols,
                "extra": extra_cols,
            }

    match = not missing_tables and not extra_tables and not column_mismatches

    return {
        "match": match,
        "missing_tables": missing_tables,
        "extra_tables": extra_tables,
        "column_mismatches": column_mismatches,
    }
