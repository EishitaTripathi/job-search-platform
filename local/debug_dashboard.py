"""Debug dashboard — runs locally on port 8002.

Uses boto3 with local AWS credentials for infrastructure checks.
Proxies RDS data through the cloud API endpoints.
No auth required — localhost only.

Usage:
    docker compose up debug
    # or directly:
    uvicorn local.debug_dashboard:app --host 0.0.0.0 --port 8002
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("debug_dashboard")

app = FastAPI(title="System Debug Dashboard", version="1.0.0")


@app.get("/")
async def root():
    return RedirectResponse(url="/static/debug_dashboard.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/debug/health")
async def debug_health():
    """Run all health checks: local pipeline + cloud boto3 + cloud API proxy."""
    from api.debug.health_checks import run_all_checks_local
    from local.debug.local_checks import run_local_checks

    # Run local and cloud checks in parallel
    local_result, cloud_result = await asyncio.gather(
        run_local_checks(),
        run_all_checks_local(),
    )

    # Merge components from both
    all_components = {}
    all_components.update(cloud_result.get("components", {}))
    all_components.update(local_result.get("components", {}))

    # Overall = worst of both
    statuses = [c.get("status", "red") for c in all_components.values()]
    order = {"green": 0, "yellow": 1, "red": 2}
    overall = max(statuses, key=lambda s: order.get(s, 2)) if statuses else "red"

    return {
        "components": all_components,
        "overall": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/debug/drift")
async def debug_drift():
    """Run infrastructure drift checks against live AWS."""
    from api.debug.drift_checks import run_drift_checks

    return await run_drift_checks()


@app.get("/api/debug/topology")
async def debug_topology():
    """Return static topology graph data."""
    from api.debug.topology import get_topology

    return get_topology()


@app.get("/api/debug/component/{component_id}")
async def debug_component(component_id: str):
    """Return detail panel data for a component."""
    from api.debug.topology import get_topology
    from local.debug.cloud_proxy import fetch_component_runs

    topology = get_topology()

    node = None
    for n in topology["nodes"]:
        if n["id"] == component_id:
            node = n
            break

    if node is None:
        raise HTTPException(404, f"Component '{component_id}' not found")

    # Fetch recent runs from cloud API
    recent_runs: list[dict[str, Any]] = []
    try:
        recent_runs = await fetch_component_runs(component_id)
    except Exception:
        logger.exception("Failed to fetch runs for %s", component_id)

    # Fetch filtered error logs from CloudWatch
    error_logs: list[dict[str, str]] = []
    try:
        from api.debug.health_checks import fetch_component_error_logs

        error_logs = await fetch_component_error_logs(component_id, limit=10)
    except Exception:
        logger.exception("Failed to fetch error logs for %s", component_id)

    connected_edges = [
        e
        for e in topology["edges"]
        if e["source"] == component_id or e["target"] == component_id
    ]

    return {
        "component": node,
        "recent_runs": recent_runs,
        "error_logs": error_logs,
        "metrics_24h": [],
        "connected_edges": connected_edges,
    }


@app.get("/api/debug/summary")
async def debug_summary():
    """Return aggregate system health for the summary bar."""
    from local.debug.cloud_proxy import fetch_summary

    return await fetch_summary()


@app.get("/api/debug/errors")
async def debug_errors():
    """Return only components with yellow/red status + their error logs."""
    from api.debug.health_checks import fetch_component_error_logs, run_all_checks_local
    from api.debug.topology import get_topology

    health = await run_all_checks_local()
    topology = get_topology()
    node_map = {n["id"]: n for n in topology["nodes"]}

    errors = []
    for comp_id, comp_health in health.get("components", {}).items():
        if comp_health.get("status") not in ("yellow", "red"):
            continue

        # Fetch error logs for this component
        error_logs: list[dict[str, str]] = []
        try:
            error_logs = await fetch_component_error_logs(comp_id, limit=10)
        except Exception:
            logger.exception("Failed to fetch error logs for %s", comp_id)

        # Find connected components
        connected = []
        for e in topology["edges"]:
            if e["source"] == comp_id:
                connected.append(
                    {
                        "direction": "to",
                        "id": e["target"],
                        "label": e.get("label", ""),
                        "name": node_map.get(e["target"], {}).get("label", e["target"]),
                    }
                )
            elif e["target"] == comp_id:
                connected.append(
                    {
                        "direction": "from",
                        "id": e["source"],
                        "label": e.get("label", ""),
                        "name": node_map.get(e["source"], {}).get("label", e["source"]),
                    }
                )

        # Failed checks only
        failed_checks = [
            c for c in comp_health.get("checks", []) if not c.get("passed")
        ]

        errors.append(
            {
                "component_id": comp_id,
                "label": node_map.get(comp_id, {}).get("label", comp_id),
                "status": comp_health["status"],
                "message": comp_health.get("message", ""),
                "key_metric": comp_health.get("key_metric", ""),
                "failed_checks": failed_checks,
                "error_logs": error_logs,
                "connected": connected,
            }
        )

    return {"errors": errors, "total": len(errors)}


# Serve frontend static files
_static_dir = Path(__file__).parent.parent / "api" / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
else:
    logger.warning("Static directory not found at %s", _static_dir)
