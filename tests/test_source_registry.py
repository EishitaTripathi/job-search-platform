"""Validates SOURCES.yaml against the codebase.

Ensures:
1. Every adapter in adapter_registry.py ADAPTERS dict has a SOURCES.yaml entry
2. No source with tos_status: non_compliant is in active ADAPTERS
3. Bedrock model IDs in SOURCES.yaml match bedrock_client.py constants
4. EventBridge rule names in SOURCES.yaml match eventbridge.tf resources

No database or network required — pure file parsing.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
SOURCES_PATH = ROOT / "SOURCES.yaml"
ADAPTER_REGISTRY_PATH = ROOT / "lambda" / "fetch" / "adapter_registry.py"
BEDROCK_CLIENT_PATH = ROOT / "api" / "agents" / "bedrock_client.py"
EVENTBRIDGE_PATH = ROOT / "infra" / "eventbridge.tf"

# Ensure lambda/fetch is importable for ADAPTERS dict
FETCH_DIR = ROOT / "lambda" / "fetch"
sys.path.insert(0, str(FETCH_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_sources() -> dict:
    """Load and parse SOURCES.yaml."""
    return yaml.safe_load(SOURCES_PATH.read_text())


def _get_active_adapters() -> set[str]:
    """Parse adapter_registry.py to get active ADAPTERS keys."""
    from adapter_registry import ADAPTERS

    return set(ADAPTERS.keys())


def _get_bedrock_constants() -> dict[str, str]:
    """Parse bedrock_client.py for HAIKU and SONNET model IDs."""
    source = BEDROCK_CLIENT_PATH.read_text()
    constants = {}
    for m in re.finditer(
        r'^(HAIKU|SONNET)\s*=\s*["\'](.+?)["\']', source, re.MULTILINE
    ):
        constants[m.group(1).lower()] = m.group(2)
    return constants


def _get_eventbridge_rule_names() -> set[str]:
    """Parse eventbridge.tf for rule name patterns."""
    source = EVENTBRIDGE_PATH.read_text()
    names = set()
    for m in re.finditer(r'name\s*=\s*"([^"]+)"', source):
        name = m.group(1)
        # Resolve ${var.project_name} to the pattern
        if "${var.project_name}" in name:
            # Just extract the suffix pattern
            names.add(name)
        else:
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sources_yaml_is_valid():
    """SOURCES.yaml must be valid YAML and parseable."""
    assert SOURCES_PATH.exists(), "SOURCES.yaml not found at project root"
    data = _load_sources()
    assert "sources" in data, "SOURCES.yaml must have a 'sources' key"
    assert isinstance(data["sources"], dict), "sources must be a dict"


def test_every_active_adapter_has_sources_entry():
    """Every adapter in ADAPTERS dict must have a SOURCES.yaml entry."""
    sources = _load_sources()
    source_names = set(sources["sources"].keys())
    active_adapters = _get_active_adapters()

    missing = active_adapters - source_names
    assert not missing, (
        f"Active adapters missing from SOURCES.yaml: {missing}. "
        f"Add entries for these sources before enabling them."
    )


def test_no_non_compliant_source_is_active():
    """No source with tos_status: non_compliant may be in active ADAPTERS."""
    sources = _load_sources()
    active_adapters = _get_active_adapters()

    non_compliant_active = []
    for name in active_adapters:
        entry = sources["sources"].get(name, {})
        tos_status = entry.get("tos_status", "unknown")
        if tos_status == "non_compliant":
            non_compliant_active.append(f"{name} (tos_status={tos_status})")

    assert not non_compliant_active, (
        f"Non-compliant sources are active in ADAPTERS: {non_compliant_active}. "
        f"Remove from adapter_registry.py ADAPTERS or update SOURCES.yaml tos_status."
    )


def test_no_review_needed_source_is_scheduled():
    """Sources with tos_status: review_needed should not have active EventBridge schedules."""
    sources = _load_sources()

    review_needed_scheduled = []
    for name, entry in sources["sources"].items():
        if entry.get("tos_status") == "review_needed":
            schedule = entry.get("schedule", "disabled")
            if schedule not in ("disabled", "on_demand"):
                review_needed_scheduled.append(
                    f"{name} (schedule={schedule}, tos_status=review_needed)"
                )

    if review_needed_scheduled:
        pytest.fail(
            f"Sources with review_needed TOS are scheduled: {review_needed_scheduled}. "
            f"Disable their EventBridge rules until TOS is reviewed."
        )


def test_bedrock_model_ids_match():
    """Bedrock model IDs in SOURCES.yaml must match bedrock_client.py constants."""
    sources = _load_sources()
    code_constants = _get_bedrock_constants()

    aws = sources.get("aws_services", {}).get("bedrock", {}).get("models", {})

    for role, entry in aws.items():
        if role in ("titan_embeddings",):
            continue  # Not a code constant
        expected_id = entry.get("model_id")
        constant_name = entry.get("constant_name", "").lower()

        if constant_name in code_constants:
            actual_id = code_constants[constant_name]
            assert actual_id == expected_id, (
                f"SOURCES.yaml bedrock.models.{role}.model_id = '{expected_id}' "
                f"but {entry.get('constant_file')}:{entry.get('constant_line')} "
                f"has {constant_name.upper()} = '{actual_id}'. "
                f"Update SOURCES.yaml or bedrock_client.py to match."
            )


def test_eventbridge_rules_exist():
    """Every non-null eventbridge_rule in SOURCES.yaml should have a matching Terraform resource."""
    sources = _load_sources()
    tf_source = EVENTBRIDGE_PATH.read_text()

    missing_rules = []
    for name, entry in sources["sources"].items():
        rule = entry.get("eventbridge_rule")
        if rule is None:
            continue

        # The rule name in TF uses ${var.project_name} interpolation.
        # Extract the suffix after project_name and check if it appears in TF.
        # Rule pattern: job-search-platform-{type}-{adapter}
        # In TF: "${var.project_name}-{type}-{adapter}"
        parts = rule.split("-", 3)
        if len(parts) >= 4:
            suffix = parts[3]  # e.g., "the_muse", "simplify", "hn"
        else:
            suffix = rule

        # Check if this suffix appears in eventbridge.tf
        if suffix not in tf_source and name not in tf_source:
            missing_rules.append(f"{name} (eventbridge_rule={rule})")

    if missing_rules:
        pytest.fail(
            f"SOURCES.yaml eventbridge_rules not found in eventbridge.tf: {missing_rules}"
        )


def test_all_sources_have_required_fields():
    """Every source entry must have the minimum required fields."""
    sources = _load_sources()
    required_fields = {"type", "tos_status", "schedule", "tier"}

    incomplete = []
    for name, entry in sources["sources"].items():
        missing = required_fields - set(entry.keys())
        if missing:
            incomplete.append(f"{name} missing: {missing}")

    assert not incomplete, (
        "SOURCES.yaml entries missing required fields:\n" + "\n".join(incomplete)
    )


def test_blacklisted_sources_have_reason():
    """Blacklisted sources must have a reason_disabled field."""
    sources = _load_sources()

    missing_reason = []
    for name, entry in sources["sources"].items():
        if entry.get("tier") == "blacklisted" and not entry.get("reason_disabled"):
            missing_reason.append(name)

    assert (
        not missing_reason
    ), f"Blacklisted sources missing reason_disabled: {missing_reason}"
