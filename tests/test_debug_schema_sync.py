"""Tests for api.debug.schema_sync — DDL parser + live schema comparison."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from api.debug.schema_sync import check_schema_match, parse_schema_file

SCHEMA_PATH = Path(__file__).parent.parent / "infra" / "schema.sql"


# ---------------------------------------------------------------------------
# parse_schema_file
# ---------------------------------------------------------------------------


def test_parse_schema_finds_all_13_tables():
    tables = parse_schema_file(SCHEMA_PATH)
    assert len(tables) == 13
    expected = {
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
    assert set(tables.keys()) == expected


def test_parse_schema_jobs_columns():
    tables = parse_schema_file(SCHEMA_PATH)
    jobs_cols = tables["jobs"]
    assert "id" in jobs_cols
    assert "company" in jobs_cols
    assert "role" in jobs_cols
    assert "status" in jobs_cols
    assert "jd_s3_key" in jobs_cols


def test_parse_schema_no_constraint_columns():
    """Constraint lines (PRIMARY KEY, UNIQUE, etc.) should not appear as columns."""
    tables = parse_schema_file(SCHEMA_PATH)
    for table, cols in tables.items():
        for col in cols:
            assert col.upper() not in (
                "PRIMARY",
                "UNIQUE",
                "CHECK",
                "FOREIGN",
                "CONSTRAINT",
            ), f"Constraint keyword '{col}' parsed as column in {table}"


# ---------------------------------------------------------------------------
# check_schema_match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_match_all_green():
    """When DB has all tables and columns, match returns True."""
    expected = parse_schema_file(SCHEMA_PATH)

    conn = AsyncMock()
    # Return all expected tables
    conn.fetch = AsyncMock(
        side_effect=_build_schema_responses(expected, missing_tables=set())
    )

    result = await check_schema_match(conn, SCHEMA_PATH)
    assert result["match"] is True
    assert result["missing_tables"] == []
    assert result["column_mismatches"] == {}


@pytest.mark.asyncio
async def test_schema_match_missing_table():
    """When DB is missing a table, match reports it."""
    expected = parse_schema_file(SCHEMA_PATH)

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=_build_schema_responses(expected, missing_tables={"deadlines"})
    )

    result = await check_schema_match(conn, SCHEMA_PATH)
    assert result["match"] is False
    assert "deadlines" in result["missing_tables"]


@pytest.mark.asyncio
async def test_schema_match_missing_column():
    """When DB is missing a column, match reports it."""
    expected = parse_schema_file(SCHEMA_PATH)

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=_build_schema_responses(
            expected, missing_cols={"jobs": {"jd_s3_key"}}
        )
    )

    result = await check_schema_match(conn, SCHEMA_PATH)
    assert result["match"] is False
    assert "jobs" in result["column_mismatches"]
    assert "jd_s3_key" in result["column_mismatches"]["jobs"]["missing"]


# ---------------------------------------------------------------------------
# Helper: simulate information_schema responses
# ---------------------------------------------------------------------------

_call_count = 0


def _build_schema_responses(
    expected: dict[str, set[str]],
    missing_tables: set[str] | None = None,
    missing_cols: dict[str, set[str]] | None = None,
):
    """Return a side_effect function that simulates information_schema queries."""
    missing_tables = missing_tables or set()
    missing_cols = missing_cols or {}

    call_idx = {"n": 0}

    async def _side_effect(*args, **kwargs):
        n = call_idx["n"]
        call_idx["n"] += 1

        if n == 0:
            # First call: information_schema.tables
            return [
                {"table_name": t} for t in expected.keys() if t not in missing_tables
            ]
        else:
            # Subsequent calls: information_schema.columns for a specific table
            table_name = args[1] if len(args) > 1 else kwargs.get("table", "")
            cols = expected.get(table_name, set())
            drop = missing_cols.get(table_name, set())
            return [{"column_name": c} for c in cols if c not in drop]

    return _side_effect
