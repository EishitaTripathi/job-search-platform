"""Validates dashboard topology stays in sync with the codebase.

When agents are added/removed, Lambda functions renamed, or tables changed,
the dashboard must be updated. This test catches drift.

See DASHBOARD.md for the component-to-system mapping.
No database or network required — pure file parsing.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# Make api/ importable for topology
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Load topology
# ---------------------------------------------------------------------------


def _get_topology_node_ids() -> set[str]:
    """Load topology node IDs from api/debug/topology.py."""
    from api.debug.topology import NODES

    return {n["id"] for n in NODES}


def _get_topology_edges() -> list[dict]:
    """Load topology edges."""
    from api.debug.topology import EDGES

    return EDGES


# ---------------------------------------------------------------------------
# Codebase scanners
# ---------------------------------------------------------------------------


def _get_agent_directories() -> set[str]:
    """Find all agent directories (api/agents/* and local/agents/*)."""
    agents = set()

    # Cloud agents
    cloud_agents_dir = ROOT / "api" / "agents"
    if cloud_agents_dir.exists():
        for d in cloud_agents_dir.iterdir():
            if d.is_dir() and d.name != "__pycache__" and (d / "graph.py").exists():
                agents.add(d.name)

    # Local agents
    local_agents_dir = ROOT / "local" / "agents"
    if local_agents_dir.exists():
        for d in local_agents_dir.iterdir():
            if (
                d.is_dir()
                and d.name != "__pycache__"
                and d.name != "shared"
                and (d / "graph.py").exists()
            ):
                agents.add(d.name)

    return agents


def _get_schema_tables() -> set[str]:
    """Parse table names from infra/schema.sql."""
    schema_path = ROOT / "infra" / "schema.sql"
    if not schema_path.exists():
        return set()
    ddl = schema_path.read_text()
    tables = set()
    for m in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
        ddl,
        re.IGNORECASE,
    ):
        tables.add(m.group(1).lower())
    return tables


def _get_lambda_function_names_from_terraform() -> set[str]:
    """Parse Lambda function name patterns from infra/lambda.tf."""
    lambda_tf = ROOT / "infra" / "lambda.tf"
    if not lambda_tf.exists():
        return set()
    source = lambda_tf.read_text()
    names = set()
    for m in re.finditer(r'function_name\s*=\s*"([^"]+)"', source):
        names.add(m.group(1))
    return names


def _get_eventbridge_rule_count_from_terraform() -> int:
    """Count EventBridge rules in infra/eventbridge.tf."""
    eb_tf = ROOT / "infra" / "eventbridge.tf"
    if not eb_tf.exists():
        return 0
    source = eb_tf.read_text()
    # Count aws_cloudwatch_event_rule resources (including for_each)
    static_rules = len(re.findall(r'resource\s+"aws_cloudwatch_event_rule"', source))
    # Check for for_each with daily_adapters
    for_each_adapters = re.findall(r"daily_adapters\s*=\s*\{([^}]*)\}", source)
    dynamic_count = 0
    if for_each_adapters:
        # Count non-commented entries
        for block in for_each_adapters:
            for line in block.split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    dynamic_count += 1
    return (
        static_rules + dynamic_count - 1
    )  # -1 because for_each resource counted in static


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_agent_has_topology_node():
    """Every agent directory must have a corresponding node in topology.py."""
    node_ids = _get_topology_node_ids()
    agent_dirs = _get_agent_directories()

    missing = []
    for agent in agent_dirs:
        if agent not in node_ids:
            missing.append(agent)

    if missing:
        pytest.fail(
            f"Agent directories without topology nodes: {missing}. "
            f"Add nodes to api/debug/topology.py. See DASHBOARD.md."
        )


def test_topology_nodes_reference_existing_agents():
    """Topology nodes with category 'local_agent' or 'cloud_agent' must have
    a corresponding agent directory."""
    from api.debug.topology import NODES

    agent_dirs = _get_agent_directories()

    orphaned = []
    for node in NODES:
        if node.get("category") in ("local_agent", "cloud_agent"):
            node_id = node["id"]
            if node_id not in agent_dirs:
                orphaned.append(node_id)

    if orphaned:
        pytest.fail(
            f"Topology agent nodes without agent directories: {orphaned}. "
            f"Remove from api/debug/topology.py or create the agent directory."
        )


def test_topology_node_count():
    """Topology should have the expected number of nodes.

    Update this count when adding/removing system components.
    """
    node_ids = _get_topology_node_ids()
    # 14 local + 14 cloud = 28 nodes (lambda_fetch, lambda_persist, sponsorship_screener
    # replaced by jd_ingestion: -3 +1 = 28)
    expected = 28
    actual = len(node_ids)

    assert actual == expected, (
        f"Topology has {actual} nodes, expected {expected}. "
        f"If you added/removed a component, update this test AND "
        f"api/debug/topology.py. See DASHBOARD.md."
    )


def test_topology_edges_reference_valid_nodes():
    """Every edge source and target must be a valid node ID."""
    node_ids = _get_topology_node_ids()
    edges = _get_topology_edges()

    invalid = []
    for edge in edges:
        if edge["source"] not in node_ids:
            invalid.append(f"edge source '{edge['source']}' not a valid node")
        if edge["target"] not in node_ids:
            invalid.append(f"edge target '{edge['target']}' not a valid node")

    assert not invalid, "Topology edges reference invalid nodes:\n" + "\n".join(invalid)


def test_lambda_names_match_terraform():
    """Lambda function name patterns in health checks should match Terraform."""
    health_checks_path = ROOT / "api" / "debug" / "health_checks.py"
    if not health_checks_path.exists():
        pytest.skip("health_checks.py not found")

    source = health_checks_path.read_text()
    tf_names = _get_lambda_function_names_from_terraform()

    # Extract function name patterns from health check calls
    # Look for check_lambda("...") or function_name patterns
    hc_patterns = set()
    for m in re.finditer(r'check_lambda\(["\']([^"\']+)', source):
        hc_patterns.add(m.group(1))

    # Also check for string patterns like "{project}-fetch"
    for m in re.finditer(r'["\'].*?-(?:fetch|persist)["\']', source):
        hc_patterns.add(m.group().strip("\"'"))

    # If we found patterns, verify they have corresponding TF resources
    # (This is a loose check — TF names use interpolation ${var.project_name})
    if hc_patterns and tf_names:
        for pattern in hc_patterns:
            # Extract the suffix (fetch/persist)
            for suffix in ("fetch", "persist"):
                if suffix in pattern:
                    tf_match = any(suffix in name for name in tf_names)
                    assert tf_match, (
                        f"Health check references Lambda '{pattern}' but "
                        f"no matching function in lambda.tf: {tf_names}"
                    )


def test_cloud_infra_nodes_match_terraform():
    """Every cloud infrastructure/data_store node with a health_check must
    correspond to a real Terraform resource or data source.

    Catches: adding a Terraform resource without a dashboard node, or leaving
    a dashboard node after the resource is removed.
    """
    from api.debug.topology import NODES

    # Map topology node IDs to the Terraform files that define them
    # Update this mapping when adding new cloud infrastructure nodes
    # Map cloud topology node IDs (that have health checks) to their
    # Terraform source files. Update when adding/removing cloud resources.
    EXPECTED_CLOUD_INFRA = {
        "sqs": "infra/data.tf",
        "s3": "infra/data.tf",
        "eventbridge": "infra/eventbridge.tf",
        "rds": "infra/data.tf",  # data source (console-created)
        "bedrock_kb": "infra/bedrock.tf",
        "analysis_poller": "infra/ecs.tf",  # background task in ECS
    }

    cloud_infra_nodes = {
        n["id"]
        for n in NODES
        if n.get("group") == "cloud"
        and n.get("health_check")  # only nodes with active health checks
    }

    # Every health-checked infra node must be in the mapping
    unmapped = cloud_infra_nodes - set(EXPECTED_CLOUD_INFRA)
    assert not unmapped, (
        f"Cloud infra nodes with health checks not in EXPECTED_CLOUD_INFRA: {unmapped}. "
        f"Add them to this test mapping."
    )

    # Every mapped node must exist in topology
    missing_nodes = set(EXPECTED_CLOUD_INFRA) - cloud_infra_nodes
    assert not missing_nodes, (
        f"EXPECTED_CLOUD_INFRA entries without topology nodes: {missing_nodes}. "
        f"Add nodes to api/debug/topology.py or remove from this mapping."
    )

    # Every mapped Terraform file must exist
    for node_id, tf_file in EXPECTED_CLOUD_INFRA.items():
        tf_path = ROOT / tf_file
        assert tf_path.exists(), (
            f"Topology node '{node_id}' references {tf_file} but file doesn't exist. "
            f"Was the resource moved or removed?"
        )


def test_health_check_components_have_topology_nodes():
    """Every component ID returned by health checks must have a topology node.

    Catches: adding a new health check without updating the dashboard.
    """
    from api.debug.topology import NODES

    node_ids = {n["id"] for n in NODES}

    # Parse component names from run_all_checks_local() in health_checks.py
    health_checks_path = ROOT / "api" / "debug" / "health_checks.py"
    source = health_checks_path.read_text()

    # Extract _safe(..., "component_name") patterns
    health_components = set()
    for m in re.finditer(r'_safe\(.+?,\s*["\'](\w+)["\']\)', source):
        component = m.group(1)
        # Skip lambda_fetch/lambda_persist — Lambda is removed
        if "lambda" in component:
            continue
        health_components.add(component)

    # cross_boundary is a virtual check (validates the local→cloud edge),
    # not a standalone infrastructure component needing its own node
    health_components.discard("cross_boundary")

    missing = health_components - node_ids
    assert not missing, (
        f"Health check components without topology nodes: {missing}. "
        f"Add nodes to api/debug/topology.py. See DASHBOARD.md."
    )


def test_topology_health_check_ids_are_valid():
    """Every topology node with health_check set must reference a real check
    function that exists in health_checks.py or local_checks.py.

    Catches: renaming a health check without updating topology.
    """
    from api.debug.topology import NODES

    # Collect all check function names from health_checks.py and local_checks.py
    valid_checks = set()

    health_path = ROOT / "api" / "debug" / "health_checks.py"
    if health_path.exists():
        source = health_path.read_text()
        for m in re.finditer(r'_safe\(.+?,\s*["\'](\w+)["\']\)', source):
            valid_checks.add(m.group(1))

    local_path = ROOT / "local" / "debug" / "local_checks.py"
    if local_path.exists():
        source = local_path.read_text()
        for m in re.finditer(r'_safe\(.+?,\s*["\'](\w+)["\']\)', source):
            valid_checks.add(m.group(1))

    # Check each topology node
    broken = []
    for node in NODES:
        hc = node.get("health_check")
        if hc and hc not in valid_checks:
            broken.append(
                f"Node '{node['id']}' references health_check '{hc}' which doesn't exist"
            )

    assert not broken, (
        "Topology nodes reference non-existent health checks:\n"
        + "\n".join(broken)
        + "\nUpdate api/debug/topology.py or add the health check."
    )


def test_schema_table_count_matches_rds_check():
    """The number of tables in schema.sql should be reflected in the dashboard."""
    tables = _get_schema_tables()
    expected_table_count = 13  # As of schema.sql

    assert len(tables) == expected_table_count, (
        f"schema.sql has {len(tables)} tables, expected {expected_table_count}. "
        f"If you added/removed a table, update this test AND "
        f"api/debug/health_checks.py check_rds() expected tables."
    )


# ---------------------------------------------------------------------------
# Terraform ↔ topology description sync
# ---------------------------------------------------------------------------


def _get_ecs_cpu_memory_from_terraform() -> tuple[str, str]:
    """Parse ECS CPU and memory from infra/ecs.tf."""
    ecs_tf = ROOT / "infra" / "ecs.tf"
    if not ecs_tf.exists():
        return ("", "")
    source = ecs_tf.read_text()
    cpu_match = re.search(r"cpu\s*=\s*(\d+)", source)
    mem_match = re.search(r"memory\s*=\s*(\d+)", source)
    cpu = cpu_match.group(1) if cpu_match else ""
    memory = mem_match.group(1) if mem_match else ""
    return (cpu, memory)


def test_ecs_topology_matches_terraform_cpu_memory():
    """ECS node description must reflect the actual CPU/memory from ecs.tf."""
    from api.debug.topology import NODES

    cpu, memory = _get_ecs_cpu_memory_from_terraform()
    if not cpu or not memory:
        pytest.skip("Could not parse CPU/memory from ecs.tf")

    vcpu = int(cpu) / 1024
    memory_mb = int(memory)

    ecs_node = next((n for n in NODES if n["id"] == "ecs"), None)
    assert ecs_node is not None, "No 'ecs' node in topology"

    desc = ecs_node["description"]
    assert f"{vcpu}" in desc and f"{memory_mb}" in desc, (
        f"ECS topology description '{desc}' does not match Terraform "
        f"values: {vcpu} vCPU, {memory_mb}MB. Update api/debug/topology.py."
    )


def test_topology_descriptions_no_stale_lambda_references():
    """No topology description should reference Lambda if Lambda is removed."""
    lambda_tf = ROOT / "infra" / "lambda.tf"
    if not lambda_tf.exists():
        pytest.skip("lambda.tf not found")

    source = lambda_tf.read_text()
    lambda_removed = "REMOVED" in source.split("\n")[0]

    if not lambda_removed:
        pytest.skip("Lambda functions still active")

    from api.debug.topology import NODES

    stale = []
    for node in NODES:
        desc = node.get("description", "")
        why = node.get("why", "")
        text = f"{desc} {why}".lower()
        # Allow references that explicitly say "replaces Lambda" (historical context)
        if "lambda" in text and "replaces lambda" not in text:
            stale.append(
                f"Node '{node['id']}' references Lambda in "
                f"description/why but Lambda is removed"
            )

    assert not stale, (
        "Stale Lambda references in topology:\n"
        + "\n".join(stale)
        + "\nUpdate api/debug/topology.py."
    )
