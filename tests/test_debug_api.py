"""Tests for api.debug.router — debug dashboard API endpoints."""

from api.debug.topology import EDGES, GROUPS, NODES, get_topology


# ---------------------------------------------------------------------------
# Topology data integrity
# ---------------------------------------------------------------------------


def test_topology_has_nodes_edges_groups():
    topo = get_topology()
    assert "nodes" in topo
    assert "edges" in topo
    assert "groups" in topo
    assert len(topo["nodes"]) > 0
    assert len(topo["edges"]) > 0
    assert len(topo["groups"]) == 2


def test_topology_node_ids_unique():
    ids = [n["id"] for n in NODES]
    assert len(ids) == len(
        set(ids)
    ), f"Duplicate node IDs: {[x for x in ids if ids.count(x) > 1]}"


def test_topology_all_nodes_have_required_fields():
    required = {"id", "label", "group", "category", "description", "why"}
    for node in NODES:
        missing = required - set(node.keys())
        assert not missing, f"Node '{node['id']}' missing fields: {missing}"


def test_topology_edge_sources_exist():
    """Every edge source must reference an existing node."""
    node_ids = {n["id"] for n in NODES}
    for edge in EDGES:
        assert (
            edge["source"] in node_ids
        ), f"Edge source '{edge['source']}' not found in nodes"


def test_topology_edge_targets_exist():
    """Every edge target must reference an existing node."""
    node_ids = {n["id"] for n in NODES}
    for edge in EDGES:
        assert (
            edge["target"] in node_ids
        ), f"Edge target '{edge['target']}' not found in nodes"


def test_topology_edges_have_labels():
    """Every edge should have a human-readable label."""
    for i, edge in enumerate(EDGES):
        assert edge.get(
            "label"
        ), f"Edge {i} ({edge['source']} → {edge['target']}) has no label"


def test_topology_groups():
    assert any(g["id"] == "local" for g in GROUPS)
    assert any(g["id"] == "cloud" for g in GROUPS)


def test_topology_health_check_field_valid():
    """Nodes with health_check must have a string value or None."""
    for node in NODES:
        hc = node.get("health_check")
        assert hc is None or isinstance(
            hc, str
        ), f"Node '{node['id']}' has invalid health_check: {hc}"


def test_topology_local_infra_nodes_have_health_checks():
    """Key local infrastructure nodes should have health checks."""
    expected_checked = {
        "ollama",
        "chromadb",
        "local_postgres",
        "gmail",
        "onnx_embedder",
        "mlflow",
    }
    for node in NODES:
        if node["id"] in expected_checked:
            assert (
                node.get("health_check") is not None
            ), f"Local node '{node['id']}' should have a health_check"


def test_topology_node_count():
    """Should have ~29 nodes (13 local + 16 cloud)."""
    local_count = sum(1 for n in NODES if n["group"] == "local")
    cloud_count = sum(1 for n in NODES if n["group"] == "cloud")
    assert local_count >= 10, f"Expected >=10 local nodes, got {local_count}"
    assert cloud_count >= 10, f"Expected >=10 cloud nodes, got {cloud_count}"


def test_topology_categories():
    """All categories should be from the expected set."""
    valid_cats = {
        "data_source",
        "scheduler",
        "local_agent",
        "cloud_agent",
        "infrastructure",
        "data_store",
        "serverless",
        "boundary",
    }
    for node in NODES:
        assert (
            node["category"] in valid_cats
        ), f"Node '{node['id']}' has invalid category '{node['category']}'"
