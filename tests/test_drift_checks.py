"""Tests for api.debug.drift_checks — drift detection and fix generation."""

from unittest.mock import MagicMock, patch

import pytest

from api.debug.drift_checks import (
    DriftFix,
    DriftResult,
    HealthStatus,
    check_drift_ecr,
    check_drift_ecs,
    check_drift_eventbridge,
    check_drift_iam,
    check_drift_lambda_orphans,
    check_drift_nat_instance,
    check_drift_rds,
    check_drift_s3,
    check_drift_security_groups,
    check_drift_sqs,
    check_drift_vpc_endpoints,
    run_drift_checks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mock_client(service_responses: dict):
    """Create a mock boto3 client that returns canned responses."""
    client = MagicMock()
    for method, response in service_responses.items():
        if isinstance(response, Exception):
            getattr(client, method).side_effect = response
        else:
            getattr(client, method).return_value = response
    return client


# ---------------------------------------------------------------------------
# ECS
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_ecs_green():
    client = _mock_client(
        {
            "describe_task_definition": {
                "taskDefinition": {
                    "revision": 5,
                    "cpu": "512",
                    "memory": "1024",
                    "containerDefinitions": [
                        {
                            "image": "123.dkr.ecr.us-east-2.amazonaws.com/job-search-platform:abc123"
                        }
                    ],
                }
            },
            "describe_services": {
                "services": [
                    {
                        "desiredCount": 1,
                        "runningCount": 1,
                    }
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_ecs()
    assert result.status == HealthStatus.GREEN
    assert all(c.passed for c in result.checks)
    assert len(result.fixes) == 0


@pytest.mark.asyncio
async def test_drift_ecs_red_cpu_mismatch():
    client = _mock_client(
        {
            "describe_task_definition": {
                "taskDefinition": {
                    "revision": 5,
                    "cpu": "256",
                    "memory": "512",
                    "containerDefinitions": [{"image": "img:latest"}],
                }
            },
            "describe_services": {
                "services": [
                    {
                        "desiredCount": 1,
                        "runningCount": 1,
                    }
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_ecs()
    assert result.status == HealthStatus.RED
    assert any(not c.passed and c.check == "CPU" for c in result.checks)
    assert any(f.requires_terraform for f in result.fixes)


@pytest.mark.asyncio
async def test_drift_ecs_red_desired_count_zero():
    client = _mock_client(
        {
            "describe_task_definition": {
                "taskDefinition": {
                    "revision": 5,
                    "cpu": "512",
                    "memory": "1024",
                    "containerDefinitions": [{"image": "img:latest"}],
                }
            },
            "describe_services": {
                "services": [
                    {
                        "desiredCount": 0,
                        "runningCount": 0,
                    }
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_ecs()
    assert result.status == HealthStatus.RED
    fix_descs = [f.description for f in result.fixes]
    assert any("desired count" in d for d in fix_descs)
    assert any(f.risk == "low" for f in result.fixes)


# ---------------------------------------------------------------------------
# SQS
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_sqs_green():
    client = _mock_client(
        {
            "get_queue_url": {
                "QueueUrl": "https://sqs.us-east-2.amazonaws.com/123/queue"
            },
            "get_queue_attributes": {
                "Attributes": {
                    "VisibilityTimeout": "300",
                    "MessageRetentionPeriod": "345600",
                    "RedrivePolicy": '{"maxReceiveCount": 3, "deadLetterTargetArn": "arn:dlq"}',
                }
            },
        }
    )

    # DLQ call
    def _get_queue_url(**kwargs):
        if "dlq" in kwargs.get("QueueName", ""):
            return {"QueueUrl": "https://sqs.us-east-2.amazonaws.com/123/dlq"}
        return {"QueueUrl": "https://sqs.us-east-2.amazonaws.com/123/queue"}

    client.get_queue_url.side_effect = _get_queue_url

    def _get_attrs(**kwargs):
        if "dlq" in kwargs.get("QueueUrl", ""):
            return {"Attributes": {"MessageRetentionPeriod": "1209600"}}
        return {
            "Attributes": {
                "VisibilityTimeout": "300",
                "MessageRetentionPeriod": "345600",
                "RedrivePolicy": '{"maxReceiveCount": 3, "deadLetterTargetArn": "arn:dlq"}',
            }
        }

    client.get_queue_attributes.side_effect = _get_attrs

    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_sqs()
    assert result.status == HealthStatus.GREEN
    assert len(result.fixes) == 0


@pytest.mark.asyncio
async def test_drift_sqs_red_visibility_mismatch():
    client = _mock_client(
        {
            "get_queue_url": {"QueueUrl": "https://sqs.example.com/queue"},
            "get_queue_attributes": {
                "Attributes": {
                    "VisibilityTimeout": "60",
                    "MessageRetentionPeriod": "345600",
                    "RedrivePolicy": '{"maxReceiveCount": 3}',
                }
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_sqs()
    assert result.status == HealthStatus.RED
    assert any(not c.passed and "Visibility" in c.check for c in result.checks)


# ---------------------------------------------------------------------------
# EventBridge
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_eventbridge_green():
    client = _mock_client(
        {
            "list_rules": {
                "Rules": [
                    {
                        "Name": "job-search-platform-monthly-hn",
                        "ScheduleExpression": "cron(0 9 1 * ? *)",
                        "State": "ENABLED",
                    },
                    {
                        "Name": "job-search-platform-daily-the_muse",
                        "ScheduleExpression": "cron(0 6 * * ? *)",
                        "State": "ENABLED",
                    },
                    {
                        "Name": "job-search-platform-daily-simplify",
                        "ScheduleExpression": "cron(0 6 * * ? *)",
                        "State": "ENABLED",
                    },
                ]
            }
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_eventbridge()
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_drift_eventbridge_red_disabled():
    client = _mock_client(
        {
            "list_rules": {
                "Rules": [
                    {
                        "Name": "job-search-platform-monthly-hn",
                        "ScheduleExpression": "cron(0 9 1 * ? *)",
                        "State": "DISABLED",
                    },
                    {
                        "Name": "job-search-platform-daily-the_muse",
                        "ScheduleExpression": "cron(0 6 * * ? *)",
                        "State": "ENABLED",
                    },
                    {
                        "Name": "job-search-platform-daily-simplify",
                        "ScheduleExpression": "cron(0 6 * * ? *)",
                        "State": "ENABLED",
                    },
                ]
            }
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_eventbridge()
    assert result.status == HealthStatus.RED
    assert any(f.risk == "low" and "enable-rule" in f.command for f in result.fixes)


# ---------------------------------------------------------------------------
# Security Groups
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_sg_green():
    sgs = [
        {
            "GroupName": "job-search-platform-alb-sg",
            "GroupId": "sg-1",
            "IpPermissions": [{"FromPort": 80}, {"FromPort": 443}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-ecs-sg",
            "GroupId": "sg-2",
            "IpPermissions": [{"FromPort": 8080}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-rds-sg",
            "GroupId": "sg-3",
            "IpPermissions": [{"FromPort": 5432}],
            "IpPermissionsEgress": [],
        },
        {
            "GroupName": "job-search-platform-nat-instance-sg",
            "GroupId": "sg-4",
            "IpPermissions": [{"FromPort": 0, "IpProtocol": "-1"}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-vpce-sg",
            "GroupId": "sg-5",
            "IpPermissions": [{"FromPort": 443}],
            "IpPermissionsEgress": [],
        },
    ]
    client = _mock_client(
        {
            "describe_security_groups": {"SecurityGroups": sgs},
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_security_groups()
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_drift_sg_red_rogue_public_ingress():
    """Non-ALB SG with 0.0.0.0/0 ingress should be RED."""
    sgs = [
        {
            "GroupName": "job-search-platform-alb-sg",
            "GroupId": "sg-1",
            "IpPermissions": [{"FromPort": 80}, {"FromPort": 443}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-ecs-sg",
            "GroupId": "sg-2",
            "IpPermissions": [
                {"FromPort": 8080, "IpRanges": []},
                {
                    "FromPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    "IpProtocol": "tcp",
                },
            ],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-rds-sg",
            "GroupId": "sg-3",
            "IpPermissions": [{"FromPort": 5432}],
            "IpPermissionsEgress": [],
        },
        {
            "GroupName": "job-search-platform-nat-instance-sg",
            "GroupId": "sg-4",
            "IpPermissions": [{"FromPort": 0, "IpProtocol": "-1"}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-vpce-sg",
            "GroupId": "sg-5",
            "IpPermissions": [{"FromPort": 443}],
            "IpPermissionsEgress": [],
        },
    ]
    client = _mock_client(
        {
            "describe_security_groups": {"SecurityGroups": sgs},
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_security_groups()
    assert result.status == HealthStatus.RED
    assert any("0.0.0.0/0" in c.actual for c in result.checks if not c.passed)
    assert any(f.risk == "high" and "revoke" in f.command for f in result.fixes)


@pytest.mark.asyncio
async def test_drift_sg_yellow_orphaned():
    """Orphaned Lambda SGs should be YELLOW."""
    sgs = [
        {
            "GroupName": "job-search-platform-alb-sg",
            "GroupId": "sg-1",
            "IpPermissions": [{"FromPort": 80}, {"FromPort": 443}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-ecs-sg",
            "GroupId": "sg-2",
            "IpPermissions": [{"FromPort": 8080}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-rds-sg",
            "GroupId": "sg-3",
            "IpPermissions": [{"FromPort": 5432}],
            "IpPermissionsEgress": [],
        },
        {
            "GroupName": "job-search-platform-nat-instance-sg",
            "GroupId": "sg-4",
            "IpPermissions": [{"FromPort": 0, "IpProtocol": "-1"}],
            "IpPermissionsEgress": [{}],
        },
        {
            "GroupName": "job-search-platform-vpce-sg",
            "GroupId": "sg-5",
            "IpPermissions": [{"FromPort": 443}],
            "IpPermissionsEgress": [],
        },
        {
            "GroupName": "job-search-platform-lambda-fetch-sg",
            "GroupId": "sg-6",
            "IpPermissions": [],
            "IpPermissionsEgress": [{}],
        },
    ]
    client = _mock_client(
        {
            "describe_security_groups": {"SecurityGroups": sgs},
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_security_groups()
    assert result.status == HealthStatus.YELLOW
    assert any("orphaned" in f.description.lower() for f in result.fixes)


# ---------------------------------------------------------------------------
# RDS
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_rds_green():
    client = _mock_client(
        {
            "describe_db_instances": {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "job-search-platform",
                        "DBInstanceClass": "db.t4g.micro",
                        "Engine": "postgres",
                        "EngineVersion": "17.6",
                        "PubliclyAccessible": False,
                        "Endpoint": {"Address": "rds.example.com", "Port": 5432},
                    }
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_rds()
    assert result.status == HealthStatus.GREEN
    assert result.details["endpoint"] == "rds.example.com:5432"


@pytest.mark.asyncio
async def test_drift_rds_red_publicly_accessible():
    """Publicly accessible RDS must be RED — security critical."""
    client = _mock_client(
        {
            "describe_db_instances": {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "job-search-platform",
                        "DBInstanceClass": "db.t3.micro",
                        "Engine": "postgres",
                        "EngineVersion": "15.4",
                        "PubliclyAccessible": True,
                        "Endpoint": {"Address": "rds.example.com", "Port": 5432},
                    }
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_rds()
    assert result.status == HealthStatus.RED
    assert any(
        f.risk == "high" and "no-publicly-accessible" in f.command for f in result.fixes
    )


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_s3_green():
    client = _mock_client(
        {
            "head_bucket": {},
            "list_objects_v2": {"KeyCount": 5},
            "get_bucket_versioning": {"Status": "Enabled"},
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_s3()
    assert result.status == HealthStatus.GREEN


# ---------------------------------------------------------------------------
# NAT Instance
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_nat_green():
    client = _mock_client(
        {
            "describe_instances": {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-123",
                                "State": {"Name": "running"},
                                "SourceDestCheck": False,
                                "MetadataOptions": {"HttpTokens": "required"},
                                "InstanceType": "t3.micro",
                                "PublicIpAddress": "1.2.3.4",
                            }
                        ]
                    }
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_nat_instance()
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_drift_nat_red_imdsv2_disabled():
    """IMDSv2 not enforced must be RED — SSRF vulnerability."""
    client = _mock_client(
        {
            "describe_instances": {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-123",
                                "State": {"Name": "running"},
                                "SourceDestCheck": False,
                                "MetadataOptions": {"HttpTokens": "optional"},
                                "InstanceType": "t3.micro",
                            }
                        ]
                    }
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_nat_instance()
    assert result.status == HealthStatus.RED
    assert any("IMDSv2" in f.description for f in result.fixes)


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_iam_green_no_wildcards():
    client = _mock_client(
        {
            "list_role_policies": {"PolicyNames": ["inline1"]},
            "get_role_policy": {
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Action": "sqs:SendMessage",
                            "Resource": "arn:aws:sqs:*:*:job-*",
                        }
                    ],
                }
            },
            "list_attached_role_policies": {"AttachedPolicies": []},
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_iam()
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_drift_iam_red_wildcard_action():
    client = _mock_client(
        {
            "list_role_policies": {"PolicyNames": ["inline1"]},
            "get_role_policy": {
                "PolicyDocument": {
                    "Statement": [{"Action": "*", "Resource": "arn:aws:s3:::bucket"}],
                }
            },
            "list_attached_role_policies": {"AttachedPolicies": []},
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_iam()
    assert result.status == HealthStatus.RED
    assert any(f.risk == "high" for f in result.fixes)


# ---------------------------------------------------------------------------
# VPC Endpoints
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_vpce_green():
    client = _mock_client(
        {
            "describe_vpc_endpoints": {
                "VpcEndpoints": [
                    {
                        "ServiceName": "com.amazonaws.us-east-2.secretsmanager",
                        "State": "available",
                        "PrivateDnsEnabled": True,
                    },
                    {
                        "ServiceName": "com.amazonaws.us-east-2.logs",
                        "State": "available",
                        "PrivateDnsEnabled": True,
                    },
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_vpc_endpoints()
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_drift_vpce_red_missing():
    client = _mock_client(
        {
            "describe_vpc_endpoints": {
                "VpcEndpoints": [
                    {
                        "ServiceName": "com.amazonaws.us-east-2.secretsmanager",
                        "State": "available",
                        "PrivateDnsEnabled": True,
                    },
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_vpc_endpoints()
    assert result.status == HealthStatus.RED
    assert any("logs" in f.description for f in result.fixes)


# ---------------------------------------------------------------------------
# Lambda Orphans
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_lambda_orphans_green():
    client = _mock_client(
        {
            "list_functions": {
                "Functions": [
                    {"FunctionName": "some-other-function"},
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_lambda_orphans()
    assert result.status == HealthStatus.GREEN


@pytest.mark.asyncio
async def test_drift_lambda_orphans_yellow():
    client = _mock_client(
        {
            "list_functions": {
                "Functions": [
                    {"FunctionName": "job-search-platform-fetch"},
                    {"FunctionName": "job-search-platform-persist"},
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_lambda_orphans()
    assert result.status == HealthStatus.YELLOW
    assert len(result.fixes) == 2
    assert all(f.risk == "low" for f in result.fixes)


# ---------------------------------------------------------------------------
# ECR
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_drift_ecr_green():
    client = _mock_client(
        {
            "describe_repositories": {
                "repositories": [
                    {
                        "repositoryUri": "123.dkr.ecr.us-east-2.amazonaws.com/job-search-platform",
                        "imageTagMutability": "MUTABLE",
                        "imageScanningConfiguration": {"scanOnPush": True},
                    }
                ]
            },
            "list_images": {
                "imageIds": [
                    {"imageDigest": "sha256:abc"},
                    {"imageDigest": "sha256:def"},
                ]
            },
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_ecr()
    assert result.status == HealthStatus.GREEN
    assert result.details["image_count"] == 2


@pytest.mark.asyncio
async def test_drift_ecr_yellow_no_scanning():
    client = _mock_client(
        {
            "describe_repositories": {
                "repositories": [
                    {
                        "repositoryUri": "123.dkr.ecr.us-east-2.amazonaws.com/job-search-platform",
                        "imageTagMutability": "MUTABLE",
                        "imageScanningConfiguration": {"scanOnPush": False},
                    }
                ]
            },
            "list_images": {"imageIds": []},
        }
    )
    with patch("api.debug.drift_checks._client", return_value=client):
        result = await check_drift_ecr()
    assert result.status == HealthStatus.YELLOW
    assert any("scanning" in f.description.lower() for f in result.fixes)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_drift_checks_returns_structure():
    """run_drift_checks should return dashboard-compatible structure."""
    with (
        patch("api.debug.drift_checks.check_drift_ecs") as m_ecs,
        patch("api.debug.drift_checks.check_drift_sqs") as m_sqs,
        patch("api.debug.drift_checks.check_drift_eventbridge") as m_eb,
        patch("api.debug.drift_checks.check_drift_security_groups") as m_sg,
        patch("api.debug.drift_checks.check_drift_rds") as m_rds,
        patch("api.debug.drift_checks.check_drift_s3") as m_s3,
        patch("api.debug.drift_checks.check_drift_nat_instance") as m_nat,
        patch("api.debug.drift_checks.check_drift_iam") as m_iam,
        patch("api.debug.drift_checks.check_drift_vpc_endpoints") as m_vpce,
        patch("api.debug.drift_checks.check_drift_lambda_orphans") as m_lambda,
        patch("api.debug.drift_checks.check_drift_ecr") as m_ecr,
    ):
        # All return GREEN
        for mock in [
            m_ecs,
            m_sqs,
            m_eb,
            m_sg,
            m_rds,
            m_s3,
            m_nat,
            m_iam,
            m_vpce,
            m_lambda,
            m_ecr,
        ]:
            mock.return_value = DriftResult(
                component="test",
                status=HealthStatus.GREEN,
                message="ok",
            )

        result = await run_drift_checks()

    assert result["overall"] == "green"
    assert result["mode"] == "drift"
    assert "components" in result
    assert "fixes" in result
    assert "fix_summary" in result
    assert result["fix_summary"]["total"] == 0


@pytest.mark.asyncio
async def test_run_drift_checks_never_raises():
    """Aggregation must never raise, even if individual checks blow up."""
    with (
        patch(
            "api.debug.drift_checks.check_drift_ecs", side_effect=RuntimeError("boom")
        ),
        patch(
            "api.debug.drift_checks.check_drift_sqs", side_effect=RuntimeError("boom")
        ),
        patch(
            "api.debug.drift_checks.check_drift_eventbridge",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "api.debug.drift_checks.check_drift_security_groups",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "api.debug.drift_checks.check_drift_rds", side_effect=RuntimeError("boom")
        ),
        patch(
            "api.debug.drift_checks.check_drift_s3", side_effect=RuntimeError("boom")
        ),
        patch(
            "api.debug.drift_checks.check_drift_nat_instance",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "api.debug.drift_checks.check_drift_iam", side_effect=RuntimeError("boom")
        ),
        patch(
            "api.debug.drift_checks.check_drift_vpc_endpoints",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "api.debug.drift_checks.check_drift_lambda_orphans",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "api.debug.drift_checks.check_drift_ecr", side_effect=RuntimeError("boom")
        ),
    ):
        result = await run_drift_checks()

    assert result["overall"] == "red"
    assert len(result["components"]) == 11


# ---------------------------------------------------------------------------
# DriftResult.to_dict
# ---------------------------------------------------------------------------
def test_drift_result_to_dict():
    r = DriftResult(
        component="test",
        status=HealthStatus.RED,
        message="drift found",
        fixes=[
            DriftFix(
                description="fix it",
                command="aws do-something",
                risk="low",
            )
        ],
    )
    d = r.to_dict()
    assert d["fixes"] == [
        {
            "description": "fix it",
            "command": "aws do-something",
            "risk": "low",
            "requires_terraform": False,
        }
    ]
    assert d["status"] == "red"


# ---------------------------------------------------------------------------
# Fix risk levels
# ---------------------------------------------------------------------------
def test_fix_risk_levels():
    """Verify security-critical fixes are always high risk."""
    # These should never be auto-applied
    high_risk_fixes = [
        DriftFix("RDS public", "aws rds modify...", "high"),
        DriftFix("SG 0.0.0.0/0", "aws ec2 revoke...", "high"),
        DriftFix("IAM wildcard", "terraform apply", "high", requires_terraform=True),
    ]
    for fix in high_risk_fixes:
        assert fix.risk == "high"
