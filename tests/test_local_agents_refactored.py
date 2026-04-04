"""Tests for refactored local agents — graph compilation and state types.

Tests Stage Classifier, Recommendation Parser, Deadline Tracker,
Follow-up Advisor (simplified), and Coordinator (local-only chains).
No Ollama/DB calls — only verifies graph structure.

Heavy dependencies (onnxruntime, presidio, chromadb) are mocked so
these tests run in CI without the full local stack.
"""

import sys
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Mock heavy local dependencies before importing any local agent modules.
# These are needed because tools.py files import at module level.
# ---------------------------------------------------------------------------


def _ensure_mock_modules():
    """Install mock modules for heavy deps that aren't in CI."""
    stubs = {}

    # onnxruntime (used by embedder)
    if "onnxruntime" not in sys.modules:
        stubs["onnxruntime"] = MagicMock()

    # tokenizers (used by embedder)
    if "tokenizers" not in sys.modules:
        stubs["tokenizers"] = MagicMock()

    # numpy — keep real if available, mock otherwise
    if "numpy" not in sys.modules:
        try:
            import numpy  # noqa: F401
        except ImportError:
            stubs["numpy"] = MagicMock()

    # chromadb (used by memory)
    if "chromadb" not in sys.modules:
        stubs["chromadb"] = MagicMock()

    # presidio (used by redactor)
    if "presidio_analyzer" not in sys.modules:
        stubs["presidio_analyzer"] = MagicMock()
    if "presidio_anonymizer" not in sys.modules:
        stubs["presidio_anonymizer"] = MagicMock()
    if "presidio_anonymizer.entities" not in sys.modules:
        stubs["presidio_anonymizer.entities"] = MagicMock()

    # mlflow (used by tracking)
    if "mlflow" not in sys.modules:
        mock_mlflow = MagicMock()
        mock_mlflow.start_run = MagicMock(
            return_value=MagicMock(
                __enter__=MagicMock(),
                __exit__=MagicMock(return_value=False),
            )
        )
        stubs["mlflow"] = mock_mlflow

    sys.modules.update(stubs)


_ensure_mock_modules()


# ---------------------------------------------------------------------------
# Stage Classifier
# ---------------------------------------------------------------------------


class TestStageClassifierGraph:
    def test_compiles(self):
        from local.agents.stage_classifier.graph import build_graph

        compiled = build_graph()
        assert compiled is not None

    def test_has_expected_nodes(self):
        from local.agents.stage_classifier.graph import build_graph

        compiled = build_graph()
        node_names = set(compiled.get_graph().nodes.keys())
        assert "classify" in node_names
        assert "route_by_confidence" in node_names
        assert "send_to_pipeline" in node_names

    def test_state_type_fields(self):
        from local.agents.stage_classifier.graph import StageClassifierState

        annotations = StageClassifierState.__annotations__
        assert "email_id" in annotations
        assert "stage" in annotations
        assert "confidence" in annotations
        assert "job_id" in annotations


# ---------------------------------------------------------------------------
# Recommendation Parser
# ---------------------------------------------------------------------------


class TestRecommendationParserGraph:
    def test_compiles(self):
        from local.agents.recommendation_parser.graph import build_graph

        compiled = build_graph()
        assert compiled is not None

    def test_has_expected_nodes(self):
        from local.agents.recommendation_parser.graph import build_graph

        compiled = build_graph()
        node_names = set(compiled.get_graph().nodes.keys())
        assert "extract_entities" in node_names
        assert "validate_and_send" in node_names

    def test_state_type_fields(self):
        from local.agents.recommendation_parser.graph import RecommendationParserState

        annotations = RecommendationParserState.__annotations__
        assert "email_id" in annotations
        assert "companies" in annotations
        assert "roles" in annotations
        assert "sent_count" in annotations


# ---------------------------------------------------------------------------
# Deadline Tracker
# ---------------------------------------------------------------------------


class TestDeadlineTrackerGraph:
    def test_compiles(self):
        from local.agents.deadline_tracker.graph import build_graph

        compiled = build_graph()
        assert compiled is not None

    def test_has_expected_nodes(self):
        from local.agents.deadline_tracker.graph import build_graph

        compiled = build_graph()
        node_names = set(compiled.get_graph().nodes.keys())
        assert "parse_deadlines" in node_names
        assert "send_to_pipeline" in node_names

    def test_state_type_fields(self):
        from local.agents.deadline_tracker.graph import DeadlineTrackerState

        annotations = DeadlineTrackerState.__annotations__
        assert "email_id" in annotations
        assert "body" in annotations
        assert "job_id" in annotations
        assert "deadlines_found" in annotations


# ---------------------------------------------------------------------------
# Follow-up Advisor (simplified)
# ---------------------------------------------------------------------------


class TestFollowupAdvisorGraph:
    def test_compiles(self):
        from local.agents.followup_advisor.graph import build_graph

        compiled = build_graph()
        assert compiled is not None

    def test_has_expected_nodes(self):
        from local.agents.followup_advisor.graph import build_graph

        compiled = build_graph()
        node_names = set(compiled.get_graph().nodes.keys())
        assert "daily_check" in node_names
        assert "send_to_pipeline" in node_names

    def test_state_type_fields(self):
        from local.agents.followup_advisor.graph import FollowupState

        annotations = FollowupState.__annotations__
        assert "recommendations" in annotations
        assert "sent_count" in annotations


# ---------------------------------------------------------------------------
# Dispatch helpers (moved from coordinator)
# ---------------------------------------------------------------------------


class TestDispatchModule:
    def test_dispatch_functions_importable(self):
        from local.agents.shared.dispatch import (
            dispatch_status_update,
            dispatch_recommendation,
        )

        assert callable(dispatch_status_update)
        assert callable(dispatch_recommendation)

    def test_deadline_stages_constant(self):
        from local.agents.shared.dispatch import DEADLINE_STAGES

        assert DEADLINE_STAGES == {"assessment", "assignment", "interview"}


# ---------------------------------------------------------------------------
# Orchestration run tracking (moved from coordinator to shared/tracking.py)
# ---------------------------------------------------------------------------


class TestOrchestrationTracking:
    def test_tracking_functions_importable(self):
        from local.agents.shared.tracking import (
            create_orchestration_run,
            update_orchestration_run,
        )

        assert callable(create_orchestration_run)
        assert callable(update_orchestration_run)

    def test_tracking_functions_are_async(self):
        import inspect
        from local.agents.shared.tracking import (
            create_orchestration_run,
            update_orchestration_run,
        )

        assert inspect.iscoroutinefunction(create_orchestration_run)
        assert inspect.iscoroutinefunction(update_orchestration_run)
