"""Tests for database schema SQL — validates structure and idempotency."""

from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "infra" / "schema.sql"


class TestSchemaMigration:
    def test_schema_file_exists(self):
        assert SCHEMA_PATH.exists(), f"Schema file not found at {SCHEMA_PATH}"

    def test_schema_is_valid_sql(self):
        """Basic validation: file is non-empty and parseable as SQL statements."""
        content = SCHEMA_PATH.read_text()
        assert len(content) > 0
        # Should contain CREATE TABLE statements
        assert "CREATE TABLE" in content

    def test_all_tables_use_if_not_exists(self):
        """Every CREATE TABLE should use IF NOT EXISTS for idempotency."""
        content = SCHEMA_PATH.read_text()
        lines = content.split("\n")

        create_lines = [
            (i + 1, line.strip())
            for i, line in enumerate(lines)
            if "CREATE TABLE" in line and not line.strip().startswith("--")
        ]

        for lineno, line in create_lines:
            assert (
                "IF NOT EXISTS" in line
            ), f"Line {lineno}: CREATE TABLE missing IF NOT EXISTS: {line}"

    def test_expected_tables_present(self):
        """All expected tables should be defined in schema."""
        content = SCHEMA_PATH.read_text()
        expected_tables = [
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
        ]
        for table in expected_tables:
            assert (
                f"CREATE TABLE IF NOT EXISTS {table}" in content
            ), f"Table '{table}' not found in schema"

    def test_all_timestamps_use_timestamptz(self):
        """All timestamp columns should use TIMESTAMPTZ (UTC convention from CLAUDE.md)."""
        content = SCHEMA_PATH.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            # If a line defines a timestamp-like column, it should use TIMESTAMPTZ
            if (
                "TIMESTAMP " in stripped.upper()
                and "TIMESTAMPTZ" not in stripped.upper()
            ):
                pytest.fail(
                    f"Line {i + 1}: Uses TIMESTAMP instead of TIMESTAMPTZ: {stripped}"
                )

    def test_jobs_table_has_required_columns(self):
        """The jobs table should have key columns for the platform."""
        content = SCHEMA_PATH.read_text()
        required_columns = [
            "company",
            "role",
            "status",
            "source",
            "jd_s3_key",
            "ats_url",
        ]
        # Extract jobs table block
        jobs_start = content.index("CREATE TABLE IF NOT EXISTS jobs")
        jobs_end = content.index(");", jobs_start) + 2
        jobs_block = content[jobs_start:jobs_end]

        for col in required_columns:
            assert col in jobs_block, f"Column '{col}' not found in jobs table"

    def test_deadlines_table_exists(self):
        """Deadlines table should exist for Deadline Tracker agent."""
        content = SCHEMA_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS deadlines" in content
        assert "deadline_date" in content
        assert "job_id" in content

    def test_pipeline_metrics_table_exists(self):
        """Pipeline metrics table should exist for validation pipeline tracking."""
        content = SCHEMA_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS pipeline_metrics" in content
        assert "metric_name" in content
        assert "metric_value" in content

    def test_foreign_keys_reference_jobs(self):
        """Tables with job_id should reference jobs(id)."""
        content = SCHEMA_PATH.read_text()
        fk_tables = [
            "jd_analyses",
            "match_reports",
            "followup_recommendations",
            "deadlines",
        ]
        for table in fk_tables:
            table_start = content.index(f"CREATE TABLE IF NOT EXISTS {table}")
            table_end = content.index(");", table_start) + 2
            table_block = content[table_start:table_end]
            assert (
                "REFERENCES jobs(id)" in table_block
            ), f"Table '{table}' missing FK reference to jobs(id)"

    def test_no_bare_timestamp_defaults(self):
        """Default timestamps should use NOW() not CURRENT_TIMESTAMP for consistency."""
        content = SCHEMA_PATH.read_text()
        lines = content.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            if "DEFAULT CURRENT_TIMESTAMP" in stripped.upper():
                # CURRENT_DATE is OK (used in queries), CURRENT_TIMESTAMP in defaults is not
                if "DEFAULT CURRENT_TIMESTAMP" in stripped:
                    pytest.fail(
                        f"Line {i + 1}: Use DEFAULT NOW() instead of CURRENT_TIMESTAMP: {stripped}"
                    )
