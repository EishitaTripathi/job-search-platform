"""Infrastructure drift detection for AWS resources.

Each ``check_drift_*`` function compares live AWS state against expected values
derived from the Terraform source files (infra/*.tf).  Results use the same
:class:`HealthResult` / :class:`ExpectedActual` dataclasses as the health
dashboard so they render identically in the UI.

Expected values are hard-coded from the .tf files (ground truth).  If a
Terraform variable changes, update the corresponding ``EXPECTED_*`` constant
here.

Usage (programmatic)::

    from api.debug.drift_checks import run_drift_checks
    results = await run_drift_checks()

Usage (CLI)::

    python scripts/drift_check.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3

from api.debug.health_checks import (
    ExpectedActual,
    HealthResult,
    HealthStatus,
    _worst,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — expected values from infra/*.tf (ground truth)
# ---------------------------------------------------------------------------
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
PROJECT_NAME = "job-search-platform"

# ECS (from ecs.tf lines 133-138)
EXPECTED_ECS_CLUSTER = f"{PROJECT_NAME}-retried"
EXPECTED_ECS_SERVICE = PROJECT_NAME
EXPECTED_ECS_TASK_FAMILY = PROJECT_NAME
EXPECTED_ECS_CPU = "512"
EXPECTED_ECS_MEMORY = "1024"
EXPECTED_ECS_DESIRED_COUNT = 1

# SQS (from data.tf lines 10-19)
EXPECTED_SQS_QUEUE_NAME = f"{PROJECT_NAME}-jd-scrape-queue"
EXPECTED_SQS_VISIBILITY = "300"
EXPECTED_SQS_RETENTION = "345600"
EXPECTED_SQS_MAX_RECEIVE = 3
EXPECTED_SQS_DLQ_NAME = f"{PROJECT_NAME}-jd-scrape-dlq"
EXPECTED_SQS_DLQ_RETENTION = "1209600"

# EventBridge (from eventbridge.tf)
EXPECTED_EVENTBRIDGE_RULES = {
    f"{PROJECT_NAME}-monthly-hn": "cron(0 9 1 * ? *)",
    f"{PROJECT_NAME}-daily-the_muse": "cron(0 6 * * ? *)",
    f"{PROJECT_NAME}-daily-simplify": "cron(0 6 * * ? *)",
}

# Security Groups (from main.tf + vpc_endpoints.tf — Lambda SGs removed)
EXPECTED_SG_NAMES = {
    f"{PROJECT_NAME}-alb-sg",
    f"{PROJECT_NAME}-ecs-sg",
    f"{PROJECT_NAME}-rds-sg",
    f"{PROJECT_NAME}-nat-instance-sg",
    f"{PROJECT_NAME}-vpce-sg",
}
# Expected ingress rule counts per SG name
EXPECTED_SG_INGRESS_COUNTS = {
    f"{PROJECT_NAME}-alb-sg": 2,  # HTTP 80, HTTPS 443
    f"{PROJECT_NAME}-ecs-sg": 1,  # 8080 from ALB
    f"{PROJECT_NAME}-rds-sg": 1,  # 5432 from ECS
    f"{PROJECT_NAME}-nat-instance-sg": 1,  # all from private-fetch CIDR
    f"{PROJECT_NAME}-vpce-sg": 1,  # 443 from ECS
}

# RDS (console-created, but we know the expected config)
# Updated 2026-04-04: RDS was upgraded in console to Graviton + pg17
EXPECTED_RDS_INSTANCE_CLASS = "db.t4g.micro"
EXPECTED_RDS_ENGINE = "postgres"
EXPECTED_RDS_ENGINE_VERSION_PREFIX = "17"

# S3 (console-created)
EXPECTED_S3_BUCKET = os.environ.get(
    "S3_BUCKET", "job-search-platform-750702271770-us-east-2-an"
)

# VPC Endpoints (from vpc_endpoints.tf)
EXPECTED_VPCE_SERVICES = [
    f"com.amazonaws.{AWS_REGION}.secretsmanager",
    f"com.amazonaws.{AWS_REGION}.logs",
]

# IAM roles (from iam.tf)
EXPECTED_IAM_ROLES = [
    f"{PROJECT_NAME}-ecs-execution",
    f"{PROJECT_NAME}-ecs-task",
]

_CHECK_TIMEOUT = 15  # seconds — slightly longer than health checks (more API calls)


# ---------------------------------------------------------------------------
# Data model — extends HealthResult with fix recommendations
# ---------------------------------------------------------------------------
@dataclass
class DriftFix:
    """A recommended fix for detected drift."""

    description: str
    command: str
    risk: str  # "low", "medium", "high"
    requires_terraform: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DriftResult(HealthResult):
    """HealthResult extended with actionable fix commands."""

    fixes: list[DriftFix] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["fixes"] = [f.to_dict() for f in self.fixes]
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _client(service: str):
    """Create a boto3 client for the given service."""
    return boto3.client(service, region_name=AWS_REGION)


def _ea(check: str, expected: str, actual: str) -> ExpectedActual:
    """Shorthand for ExpectedActual with auto-computed passed flag."""
    return ExpectedActual(
        check=check,
        expected=expected,
        actual=actual,
        passed=str(expected) == str(actual),
    )


def _ea_bool(check: str, expected: bool, actual: bool) -> ExpectedActual:
    return ExpectedActual(
        check=check,
        expected=str(expected),
        actual=str(actual),
        passed=expected == actual,
    )


# ---------------------------------------------------------------------------
# Check 1: ECS — task def, running tasks, image, CPU/memory
# ---------------------------------------------------------------------------
async def check_drift_ecs() -> DriftResult:
    def _check():
        ecs = _client("ecs")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        # Task definition
        td = ecs.describe_task_definition(taskDefinition=EXPECTED_ECS_TASK_FAMILY)
        td_info = td["taskDefinition"]
        revision = td_info["revision"]
        cpu = td_info["cpu"]
        memory = td_info["memory"]
        image = (
            td_info["containerDefinitions"][0]["image"]
            if td_info["containerDefinitions"]
            else "unknown"
        )

        details["task_def_revision"] = revision
        details["image"] = image
        details["cpu"] = cpu
        details["memory"] = memory

        checks.append(_ea("CPU", EXPECTED_ECS_CPU, cpu))
        checks.append(_ea("Memory", EXPECTED_ECS_MEMORY, memory))

        if cpu != EXPECTED_ECS_CPU or memory != EXPECTED_ECS_MEMORY:
            fixes.append(
                DriftFix(
                    description=f"ECS CPU/memory mismatch: {cpu}/{memory} vs expected {EXPECTED_ECS_CPU}/{EXPECTED_ECS_MEMORY}",
                    command="cd infra && terraform plan && terraform apply",
                    risk="high",
                    requires_terraform=True,
                )
            )

        # Service state
        svc = ecs.describe_services(
            cluster=EXPECTED_ECS_CLUSTER, services=[EXPECTED_ECS_SERVICE]
        )
        if svc["services"]:
            service = svc["services"][0]
            desired = service["desiredCount"]
            running = service["runningCount"]
            details["desired_count"] = desired
            details["running_count"] = running

            checks.append(
                _ea("Desired count", str(EXPECTED_ECS_DESIRED_COUNT), str(desired))
            )
            checks.append(_ea("Running count", str(desired), str(running)))

            if desired != EXPECTED_ECS_DESIRED_COUNT:
                fixes.append(
                    DriftFix(
                        description=f"ECS desired count is {desired}, expected {EXPECTED_ECS_DESIRED_COUNT}",
                        command=f"aws ecs update-service --cluster {EXPECTED_ECS_CLUSTER} --service {EXPECTED_ECS_SERVICE} --desired-count {EXPECTED_ECS_DESIRED_COUNT}",
                        risk="low",
                    )
                )
            if running != desired:
                fixes.append(
                    DriftFix(
                        description=f"ECS running count ({running}) != desired ({desired})",
                        command=f"aws ecs update-service --cluster {EXPECTED_ECS_CLUSTER} --service {EXPECTED_ECS_SERVICE} --force-new-deployment",
                        risk="low",
                    )
                )
        else:
            checks.append(ExpectedActual("Service exists", "true", "false", False))
            fixes.append(
                DriftFix(
                    description="ECS service not found",
                    command="cd infra && terraform apply",
                    risk="high",
                    requires_terraform=True,
                )
            )

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.RED
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_ecs",
            status=status,
            message=f"ECS: {passed}/{len(checks)} checks passed",
            key_metric=f"rev {revision} · {cpu}/{memory}",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 2: SQS — queue configuration
# ---------------------------------------------------------------------------
async def check_drift_sqs() -> DriftResult:
    def _check():
        sqs = _client("sqs")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        # Get queue URL
        try:
            url_resp = sqs.get_queue_url(QueueName=EXPECTED_SQS_QUEUE_NAME)
            queue_url = url_resp["QueueUrl"]
        except sqs.exceptions.QueueDoesNotExist:
            return DriftResult(
                component="drift_sqs",
                status=HealthStatus.RED,
                message="SQS queue not found",
                checks=[ExpectedActual("Queue exists", "true", "false", False)],
                fixes=[
                    DriftFix(
                        description="Main SQS queue missing",
                        command="cd infra && terraform apply",
                        risk="high",
                        requires_terraform=True,
                    )
                ],
            )

        attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])[
            "Attributes"
        ]
        details["queue_url"] = queue_url

        vis = attrs.get("VisibilityTimeout", "")
        ret = attrs.get("MessageRetentionPeriod", "")
        checks.append(_ea("Visibility timeout", EXPECTED_SQS_VISIBILITY, vis))
        checks.append(_ea("Retention period", EXPECTED_SQS_RETENTION, ret))

        if vis != EXPECTED_SQS_VISIBILITY or ret != EXPECTED_SQS_RETENTION:
            fixes.append(
                DriftFix(
                    description=f"SQS config mismatch: visibility={vis}, retention={ret}",
                    command="cd infra && terraform plan && terraform apply",
                    risk="high",
                    requires_terraform=True,
                )
            )

        # Redrive policy
        redrive_raw = attrs.get("RedrivePolicy", "")
        if redrive_raw:
            redrive = json.loads(redrive_raw)
            max_receive = redrive.get("maxReceiveCount", 0)
            checks.append(
                _ea(
                    "DLQ maxReceiveCount",
                    str(EXPECTED_SQS_MAX_RECEIVE),
                    str(max_receive),
                )
            )
        else:
            checks.append(
                ExpectedActual("Redrive policy", "configured", "missing", False)
            )

        # DLQ
        try:
            dlq_url_resp = sqs.get_queue_url(QueueName=EXPECTED_SQS_DLQ_NAME)
            dlq_url = dlq_url_resp["QueueUrl"]
            dlq_attrs = sqs.get_queue_attributes(
                QueueUrl=dlq_url, AttributeNames=["MessageRetentionPeriod"]
            )["Attributes"]
            dlq_ret = dlq_attrs.get("MessageRetentionPeriod", "")
            checks.append(_ea("DLQ retention", EXPECTED_SQS_DLQ_RETENTION, dlq_ret))
            details["dlq_url"] = dlq_url
        except Exception:
            checks.append(ExpectedActual("DLQ exists", "true", "false", False))

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.RED
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_sqs",
            status=status,
            message=f"SQS: {passed}/{len(checks)} checks passed",
            key_metric=f"vis={vis}s ret={ret}s",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 3: EventBridge — rules existence and schedules
# ---------------------------------------------------------------------------
async def check_drift_eventbridge() -> DriftResult:
    def _check():
        events = _client("events")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        rules_resp = events.list_rules(NamePrefix=PROJECT_NAME)
        live_rules = {r["Name"]: r for r in rules_resp.get("Rules", [])}
        details["live_rules"] = list(live_rules.keys())

        for expected_name, expected_schedule in EXPECTED_EVENTBRIDGE_RULES.items():
            if expected_name in live_rules:
                rule = live_rules[expected_name]
                actual_schedule = rule.get("ScheduleExpression", "")
                actual_state = rule.get("State", "DISABLED")

                checks.append(
                    _ea(f"{expected_name} schedule", expected_schedule, actual_schedule)
                )
                checks.append(_ea(f"{expected_name} state", "ENABLED", actual_state))

                if actual_state != "ENABLED":
                    fixes.append(
                        DriftFix(
                            description=f"EventBridge rule '{expected_name}' is {actual_state}",
                            command=f"aws events enable-rule --name {expected_name}",
                            risk="low",
                        )
                    )
                if actual_schedule != expected_schedule:
                    fixes.append(
                        DriftFix(
                            description=f"Schedule mismatch for '{expected_name}': {actual_schedule} vs {expected_schedule}",
                            command="cd infra && terraform plan && terraform apply",
                            risk="high",
                            requires_terraform=True,
                        )
                    )
            else:
                checks.append(
                    ExpectedActual(f"{expected_name} exists", "true", "false", False)
                )
                fixes.append(
                    DriftFix(
                        description=f"EventBridge rule '{expected_name}' missing",
                        command="cd infra && terraform apply",
                        risk="high",
                        requires_terraform=True,
                    )
                )

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.RED
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_eventbridge",
            status=status,
            message=f"EventBridge: {passed}/{len(checks)} checks passed",
            key_metric=f"{len(live_rules)} rules",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 4: Security Groups — existence, rule counts, no rogue 0.0.0.0/0
# ---------------------------------------------------------------------------
async def check_drift_security_groups() -> DriftResult:
    def _check():
        ec2 = _client("ec2")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        # Fetch all SGs in the VPC tagged with our project
        resp = ec2.describe_security_groups(
            Filters=[{"Name": "tag:Project", "Values": [PROJECT_NAME]}]
        )
        live_sgs = {sg["GroupName"]: sg for sg in resp["SecurityGroups"]}
        details["live_sg_names"] = sorted(live_sgs.keys())

        checks.append(_ea("SG count", str(len(EXPECTED_SG_NAMES)), str(len(live_sgs))))

        # Check each expected SG exists with correct ingress rule count
        for sg_name in sorted(EXPECTED_SG_NAMES):
            if sg_name in live_sgs:
                sg = live_sgs[sg_name]
                actual_ingress = len(sg.get("IpPermissions", []))
                expected_ingress = EXPECTED_SG_INGRESS_COUNTS.get(sg_name, 0)
                checks.append(
                    _ea(
                        f"{sg_name} ingress rules",
                        str(expected_ingress),
                        str(actual_ingress),
                    )
                )

                if actual_ingress != expected_ingress:
                    fixes.append(
                        DriftFix(
                            description=f"SG '{sg_name}' has {actual_ingress} ingress rules, expected {expected_ingress}",
                            command=f"aws ec2 describe-security-group-rules --filter Name=group-id,Values={sg['GroupId']} --query 'SecurityGroupRules[?IsEgress==`false`]'",
                            risk="high",
                        )
                    )
            else:
                checks.append(
                    ExpectedActual(f"{sg_name} exists", "true", "false", False)
                )

        # CRITICAL: check for unauthorized 0.0.0.0/0 ingress on non-ALB SGs
        alb_sg_name = f"{PROJECT_NAME}-alb-sg"
        for sg_name, sg in live_sgs.items():
            if sg_name == alb_sg_name:
                continue  # ALB is allowed to have 0.0.0.0/0 ingress
            for perm in sg.get("IpPermissions", []):
                for ip_range in perm.get("IpRanges", []):
                    if ip_range.get("CidrIp") == "0.0.0.0/0":
                        checks.append(
                            ExpectedActual(
                                f"{sg_name} no public ingress",
                                "no 0.0.0.0/0",
                                "0.0.0.0/0 FOUND",
                                False,
                            )
                        )
                        fixes.append(
                            DriftFix(
                                description=f"SECURITY: {sg_name} has 0.0.0.0/0 ingress on port {perm.get('FromPort', 'all')}",
                                command=f"aws ec2 revoke-security-group-ingress --group-id {sg['GroupId']} --protocol {perm.get('IpProtocol', '-1')} --port {perm.get('FromPort', 0)} --cidr 0.0.0.0/0",
                                risk="high",
                            )
                        )

        # Check for unexpected SGs (orphaned Lambda SGs, etc.)
        extra_sgs = set(live_sgs.keys()) - EXPECTED_SG_NAMES
        if extra_sgs:
            for extra in sorted(extra_sgs):
                checks.append(
                    ExpectedActual(
                        f"{extra} expected", "false (orphaned)", "exists", False
                    )
                )
                fixes.append(
                    DriftFix(
                        description=f"Orphaned security group '{extra}' — may be from removed Lambda",
                        command=f"aws ec2 delete-security-group --group-id {live_sgs[extra]['GroupId']}",
                        risk="medium",
                    )
                )

        status = (
            HealthStatus.GREEN
            if all(c.passed for c in checks)
            else (
                HealthStatus.RED
                if any(
                    "0.0.0.0/0 FOUND" in c.actual
                    or c.check.endswith("exists")
                    and c.actual == "false"
                    for c in checks
                    if not c.passed
                )
                else HealthStatus.YELLOW
            )
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_security_groups",
            status=status,
            message=f"SGs: {passed}/{len(checks)} checks passed",
            key_metric=f"{len(live_sgs)} SGs",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 5: RDS — instance config, publicly accessible
# ---------------------------------------------------------------------------
async def check_drift_rds() -> DriftResult:
    def _check():
        rds = _client("rds")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        # Try to find the RDS instance (console-created, name may vary)
        try:
            resp = rds.describe_db_instances(DBInstanceIdentifier=PROJECT_NAME)
            instances = resp["DBInstances"]
        except rds.exceptions.DBInstanceNotFoundFault:
            # Fallback: search all instances in the region
            resp = rds.describe_db_instances()
            instances = [
                i
                for i in resp["DBInstances"]
                if PROJECT_NAME in i.get("DBInstanceIdentifier", "")
            ]

        if not instances:
            return DriftResult(
                component="drift_rds",
                status=HealthStatus.RED,
                message="RDS instance not found",
                checks=[ExpectedActual("Instance exists", "true", "false", False)],
            )

        db = instances[0]
        instance_id = db["DBInstanceIdentifier"]
        instance_class = db["DBInstanceClass"]
        engine = db["Engine"]
        engine_version = db["EngineVersion"]
        publicly_accessible = db["PubliclyAccessible"]
        endpoint = db.get("Endpoint", {})

        details["instance_id"] = instance_id
        details["instance_class"] = instance_class
        details["engine"] = engine
        details["engine_version"] = engine_version
        details["endpoint"] = (
            f"{endpoint.get('Address', 'unknown')}:{endpoint.get('Port', 'unknown')}"
        )
        details["publicly_accessible"] = publicly_accessible

        checks.append(
            _ea("Instance class", EXPECTED_RDS_INSTANCE_CLASS, instance_class)
        )
        checks.append(_ea("Engine", EXPECTED_RDS_ENGINE, engine))
        checks.append(
            ExpectedActual(
                "Engine version",
                f"starts with {EXPECTED_RDS_ENGINE_VERSION_PREFIX}",
                engine_version,
                engine_version.startswith(EXPECTED_RDS_ENGINE_VERSION_PREFIX),
            )
        )
        checks.append(_ea_bool("Publicly accessible", False, publicly_accessible))

        if publicly_accessible:
            fixes.append(
                DriftFix(
                    description="SECURITY: RDS is publicly accessible",
                    command=f"aws rds modify-db-instance --db-instance-identifier {instance_id} --no-publicly-accessible --apply-immediately",
                    risk="high",
                )
            )

        if instance_class != EXPECTED_RDS_INSTANCE_CLASS:
            fixes.append(
                DriftFix(
                    description=f"RDS instance class is {instance_class}, expected {EXPECTED_RDS_INSTANCE_CLASS}",
                    command=f"aws rds modify-db-instance --db-instance-identifier {instance_id} --db-instance-class {EXPECTED_RDS_INSTANCE_CLASS} --apply-immediately",
                    risk="high",
                )
            )

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.RED
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_rds",
            status=status,
            message=f"RDS: {passed}/{len(checks)} checks passed",
            key_metric=f"{instance_class} · pg{engine_version}",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 6: S3 — bucket exists, jds/ prefix
# ---------------------------------------------------------------------------
async def check_drift_s3() -> DriftResult:
    def _check():
        s3 = _client("s3")
        checks: list[ExpectedActual] = []
        details: dict[str, Any] = {}

        try:
            s3.head_bucket(Bucket=EXPECTED_S3_BUCKET)
            checks.append(ExpectedActual("Bucket exists", "true", "true", True))
        except Exception as exc:
            return DriftResult(
                component="drift_s3",
                status=HealthStatus.RED,
                message=f"S3 bucket not accessible: {exc}",
                checks=[ExpectedActual("Bucket exists", "true", "false", False)],
            )

        # Check jds/ prefix has objects
        resp = s3.list_objects_v2(Bucket=EXPECTED_S3_BUCKET, Prefix="jds/", MaxKeys=1)
        has_jds = resp.get("KeyCount", 0) > 0
        checks.append(_ea_bool("jds/ prefix has objects", True, has_jds))
        details["jds_prefix_has_objects"] = has_jds

        # Versioning
        try:
            ver = s3.get_bucket_versioning(Bucket=EXPECTED_S3_BUCKET)
            versioning_status = ver.get("Status", "Disabled")
            details["versioning"] = versioning_status
        except Exception:
            details["versioning"] = "unknown"

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.YELLOW
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_s3",
            status=status,
            message=f"S3: {passed}/{len(checks)} checks passed",
            key_metric=EXPECTED_S3_BUCKET[:30],
            checks=checks,
            details=details,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 7: NAT instance — running, IMDSv2, source/dest check
# ---------------------------------------------------------------------------
async def check_drift_nat_instance() -> DriftResult:
    def _check():
        ec2 = _client("ec2")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        resp = ec2.describe_instances(
            Filters=[{"Name": "tag:Name", "Values": [f"{PROJECT_NAME}-nat-instance"]}]
        )
        instances = [
            i
            for r in resp["Reservations"]
            for i in r["Instances"]
            if i["State"]["Name"] != "terminated"
        ]

        if not instances:
            return DriftResult(
                component="drift_nat",
                status=HealthStatus.RED,
                message="NAT instance not found",
                checks=[ExpectedActual("Instance exists", "true", "false", False)],
                fixes=[
                    DriftFix(
                        description="NAT instance missing",
                        command="cd infra && terraform apply",
                        risk="high",
                        requires_terraform=True,
                    )
                ],
            )

        inst = instances[0]
        instance_id = inst["InstanceId"]
        state = inst["State"]["Name"]
        source_dest = inst.get("SourceDestCheck", True)
        metadata = inst.get("MetadataOptions", {})
        http_tokens = metadata.get("HttpTokens", "optional")

        details["instance_id"] = instance_id
        details["state"] = state
        details["instance_type"] = inst.get("InstanceType", "unknown")
        details["public_ip"] = inst.get("PublicIpAddress", "none")

        checks.append(_ea("State", "running", state))
        checks.append(_ea_bool("SourceDestCheck disabled", False, source_dest))
        checks.append(_ea("IMDSv2 (HttpTokens)", "required", http_tokens))

        if state != "running":
            fixes.append(
                DriftFix(
                    description=f"NAT instance is {state}",
                    command=f"aws ec2 start-instances --instance-ids {instance_id}",
                    risk="low",
                )
            )

        if source_dest:
            fixes.append(
                DriftFix(
                    description="NAT instance has SourceDestCheck enabled (must be disabled for NAT)",
                    command=f"aws ec2 modify-instance-attribute --instance-id {instance_id} --no-source-dest-check",
                    risk="medium",
                )
            )

        if http_tokens != "required":
            fixes.append(
                DriftFix(
                    description="SECURITY: NAT instance IMDSv2 not enforced (SSRF vulnerability)",
                    command=f"aws ec2 modify-instance-metadata-options --instance-id {instance_id} --http-tokens required --http-endpoint enabled",
                    risk="medium",
                )
            )

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.RED
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_nat",
            status=status,
            message=f"NAT: {passed}/{len(checks)} checks passed",
            key_metric=f"{state} · IMDSv2={http_tokens}",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 8: IAM — no wildcard actions/resources
# ---------------------------------------------------------------------------
async def check_drift_iam() -> DriftResult:
    def _check():
        iam = _client("iam")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        for role_name in EXPECTED_IAM_ROLES:
            try:
                # Check inline policies
                inline = iam.list_role_policies(RoleName=role_name)
                for policy_name in inline.get("PolicyNames", []):
                    policy_doc = iam.get_role_policy(
                        RoleName=role_name, PolicyName=policy_name
                    )
                    doc = policy_doc["PolicyDocument"]
                    _audit_policy_doc(
                        doc, f"{role_name}/{policy_name}", checks, fixes, details
                    )

                # Check attached managed policies
                attached = iam.list_attached_role_policies(RoleName=role_name)
                for ap in attached.get("AttachedPolicies", []):
                    policy_arn = ap["PolicyArn"]
                    # Only audit custom policies (not AWS managed ones)
                    if ":aws:policy/" not in policy_arn:
                        policy_resp = iam.get_policy(PolicyArn=policy_arn)
                        version_id = policy_resp["Policy"]["DefaultVersionId"]
                        version_resp = iam.get_policy_version(
                            PolicyArn=policy_arn, VersionId=version_id
                        )
                        doc = version_resp["PolicyVersion"]["Document"]
                        _audit_policy_doc(
                            doc,
                            f"{role_name}/{ap['PolicyName']}",
                            checks,
                            fixes,
                            details,
                        )

            except iam.exceptions.NoSuchEntityException:
                checks.append(
                    ExpectedActual(f"Role {role_name} exists", "true", "false", False)
                )
                fixes.append(
                    DriftFix(
                        description=f"IAM role '{role_name}' not found",
                        command="cd infra && terraform apply",
                        risk="high",
                        requires_terraform=True,
                    )
                )

        if not checks:
            checks.append(
                ExpectedActual("IAM audit", "completed", "no policies found", False)
            )

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.RED
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_iam",
            status=status,
            message=f"IAM: {passed}/{len(checks)} checks passed",
            key_metric=f"{len(EXPECTED_IAM_ROLES)} roles audited",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


def _audit_policy_doc(
    doc: dict,
    policy_label: str,
    checks: list[ExpectedActual],
    fixes: list[DriftFix],
    details: dict[str, Any],
) -> None:
    """Scan a policy document for wildcard actions/resources."""
    statements = doc.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]

    wildcards_found: list[str] = []
    for stmt in statements:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        resources = stmt.get("Resource", [])
        if isinstance(resources, str):
            resources = [resources]

        for action in actions:
            if action == "*":
                wildcards_found.append(f"Action:* in {policy_label}")
        for resource in resources:
            if resource == "*":
                # EC2 ENI is a documented exception (AWS requires Resource:* for ENI management)
                action_str = (
                    ",".join(actions) if isinstance(actions, list) else str(actions)
                )
                if (
                    "ec2:CreateNetworkInterface" in action_str
                    or "ec2:DescribeNetworkInterfaces" in action_str
                ):
                    continue
                wildcards_found.append(f"Resource:* in {policy_label}")

    if wildcards_found:
        for w in wildcards_found:
            checks.append(
                ExpectedActual(
                    f"No wildcards in {policy_label}", "no wildcards", w, False
                )
            )
            fixes.append(
                DriftFix(
                    description=f"IAM wildcard found: {w}",
                    command="cd infra && terraform plan  # Review and scope down IAM policies in iam.tf",
                    risk="high",
                    requires_terraform=True,
                )
            )
        details.setdefault("wildcards", []).extend(wildcards_found)
    else:
        checks.append(
            ExpectedActual(
                f"No wildcards in {policy_label}", "no wildcards", "no wildcards", True
            )
        )


# ---------------------------------------------------------------------------
# Check 9: VPC Endpoints — existence, private DNS
# ---------------------------------------------------------------------------
async def check_drift_vpc_endpoints() -> DriftResult:
    def _check():
        ec2 = _client("ec2")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        resp = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "tag:Project", "Values": [PROJECT_NAME]}]
        )
        live_endpoints = {
            ep["ServiceName"]: ep
            for ep in resp["VpcEndpoints"]
            if ep["State"] != "deleted"
        }
        details["live_endpoints"] = list(live_endpoints.keys())

        for expected_service in EXPECTED_VPCE_SERVICES:
            if expected_service in live_endpoints:
                ep = live_endpoints[expected_service]
                state = ep["State"]
                private_dns = ep.get("PrivateDnsEnabled", False)

                checks.append(
                    _ea(f"{expected_service.split('.')[-1]} state", "available", state)
                )
                checks.append(
                    _ea_bool(
                        f"{expected_service.split('.')[-1]} private DNS",
                        True,
                        private_dns,
                    )
                )

                if not private_dns:
                    fixes.append(
                        DriftFix(
                            description=f"VPC endpoint {expected_service} has private DNS disabled",
                            command="cd infra && terraform apply",
                            risk="high",
                            requires_terraform=True,
                        )
                    )
            else:
                service_short = expected_service.split(".")[-1]
                checks.append(
                    ExpectedActual(
                        f"{service_short} endpoint exists", "true", "false", False
                    )
                )
                fixes.append(
                    DriftFix(
                        description=f"VPC endpoint for {expected_service} missing",
                        command="cd infra && terraform apply",
                        risk="high",
                        requires_terraform=True,
                    )
                )

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.RED
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_vpce",
            status=status,
            message=f"VPC Endpoints: {passed}/{len(checks)} checks passed",
            key_metric=f"{len(live_endpoints)} endpoints",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 10: Lambda orphans — detect functions that should have been removed
# ---------------------------------------------------------------------------
async def check_drift_lambda_orphans() -> DriftResult:
    def _check():
        lam = _client("lambda")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        resp = lam.list_functions(MaxItems=50)
        orphaned = [
            f
            for f in resp.get("Functions", [])
            if f["FunctionName"].startswith(f"{PROJECT_NAME}-")
        ]
        details["orphaned_functions"] = [f["FunctionName"] for f in orphaned]

        if orphaned:
            for f in orphaned:
                fname = f["FunctionName"]
                checks.append(
                    ExpectedActual(
                        f"Lambda {fname}",
                        "should not exist (removed)",
                        "exists",
                        False,
                    )
                )
                fixes.append(
                    DriftFix(
                        description=f"Orphaned Lambda function '{fname}' — Lambda was removed from Terraform",
                        command=f"aws lambda delete-function --function-name {fname}",
                        risk="low",
                    )
                )
            status = HealthStatus.YELLOW
        else:
            checks.append(ExpectedActual("No orphaned Lambdas", "none", "none", True))
            status = HealthStatus.GREEN

        return DriftResult(
            component="drift_lambda_orphans",
            status=status,
            message=f"Lambda: {len(orphaned)} orphaned function(s)",
            key_metric="clean" if not orphaned else f"{len(orphaned)} orphaned",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Check 11: ECR — repository exists, scanning
# ---------------------------------------------------------------------------
async def check_drift_ecr() -> DriftResult:
    def _check():
        ecr = _client("ecr")
        checks: list[ExpectedActual] = []
        fixes: list[DriftFix] = []
        details: dict[str, Any] = {}

        try:
            resp = ecr.describe_repositories(repositoryNames=[PROJECT_NAME])
            repo = resp["repositories"][0]
        except Exception:
            return DriftResult(
                component="drift_ecr",
                status=HealthStatus.RED,
                message="ECR repository not found",
                checks=[ExpectedActual("Repository exists", "true", "false", False)],
            )

        details["repository_uri"] = repo.get("repositoryUri", "")
        details["image_tag_mutability"] = repo.get("imageTagMutability", "unknown")

        scanning = repo.get("imageScanningConfiguration", {}).get("scanOnPush", False)
        checks.append(ExpectedActual("Repository exists", "true", "true", True))
        checks.append(_ea_bool("Scan on push", True, scanning))

        if not scanning:
            fixes.append(
                DriftFix(
                    description="ECR image scanning on push is disabled",
                    command=f"aws ecr put-image-scanning-configuration --repository-name {PROJECT_NAME} --image-scanning-configuration scanOnPush=true",
                    risk="medium",
                )
            )

        # Count images
        try:
            images = ecr.list_images(repositoryName=PROJECT_NAME, maxResults=100)
            image_count = len(images.get("imageIds", []))
            details["image_count"] = image_count
        except Exception:
            image_count = -1

        status = (
            HealthStatus.GREEN if all(c.passed for c in checks) else HealthStatus.YELLOW
        )
        passed = sum(1 for c in checks if c.passed)
        return DriftResult(
            component="drift_ecr",
            status=status,
            message=f"ECR: {passed}/{len(checks)} checks passed",
            key_metric=f"{image_count} images" if image_count >= 0 else "exists",
            checks=checks,
            details=details,
            fixes=fixes,
        )

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Aggregation — run all drift checks concurrently
# ---------------------------------------------------------------------------
async def run_drift_checks() -> dict[str, Any]:
    """Run all drift checks concurrently with per-check timeouts.

    Returns the same structure as ``run_all_checks_local()`` for dashboard
    compatibility.
    """

    async def _safe(coro, component_name: str) -> DriftResult:
        try:
            return await asyncio.wait_for(coro, timeout=_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            return DriftResult(
                component=component_name,
                status=HealthStatus.RED,
                message=f"Timed out after {_CHECK_TIMEOUT}s",
                key_metric="timeout",
                checks=[
                    ExpectedActual(
                        "Timeout", f"< {_CHECK_TIMEOUT}s", "timed out", False
                    )
                ],
            )
        except Exception as exc:
            logger.exception("Drift check %s failed", component_name)
            return DriftResult(
                component=component_name,
                status=HealthStatus.RED,
                message=f"Error: {exc}",
                key_metric="error",
                checks=[ExpectedActual("Check", "no errors", str(exc), False)],
            )

    try:
        results: list[DriftResult] = await asyncio.gather(
            _safe(check_drift_ecs(), "drift_ecs"),
            _safe(check_drift_sqs(), "drift_sqs"),
            _safe(check_drift_eventbridge(), "drift_eventbridge"),
            _safe(check_drift_security_groups(), "drift_security_groups"),
            _safe(check_drift_rds(), "drift_rds"),
            _safe(check_drift_s3(), "drift_s3"),
            _safe(check_drift_nat_instance(), "drift_nat"),
            _safe(check_drift_iam(), "drift_iam"),
            _safe(check_drift_vpc_endpoints(), "drift_vpce"),
            _safe(check_drift_lambda_orphans(), "drift_lambda_orphans"),
            _safe(check_drift_ecr(), "drift_ecr"),
        )

        components = {r.component: r.to_dict() for r in results}
        overall = _worst([r.status for r in results])
        all_fixes = []
        for r in results:
            all_fixes.extend(f.to_dict() for f in r.fixes)

        return {
            "components": components,
            "overall": overall.value,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "mode": "drift",
            "fixes": all_fixes,
            "fix_summary": {
                "total": len(all_fixes),
                "low": sum(1 for f in all_fixes if f["risk"] == "low"),
                "medium": sum(1 for f in all_fixes if f["risk"] == "medium"),
                "high": sum(1 for f in all_fixes if f["risk"] == "high"),
            },
        }
    except Exception as exc:
        logger.exception("run_drift_checks failed")
        return {
            "components": {},
            "overall": HealthStatus.RED.value,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
