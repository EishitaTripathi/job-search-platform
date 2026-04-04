"""Secret management: Secrets Manager in production, .env fallback for local dev.

Usage:
    db_password = get_secret("DB_PASSWORD")

In production (AWS): reads from Secrets Manager using boto3.
In local dev: reads from environment variables (loaded from .env by docker-compose).
"""

import json
import os


def get_secret(key: str) -> str:
    """Retrieve a secret by key.

    Checks environment first (.env/docker-compose), falls back to
    AWS Secrets Manager if AWS_SECRET_ARN is set.
    """
    # Local dev: environment variable
    value = os.environ.get(key)
    if value is not None:
        return value

    # Production: AWS Secrets Manager
    secret_arn = os.environ.get("AWS_SECRET_ARN")
    if secret_arn:
        return _get_from_secrets_manager(secret_arn, key)

    raise ValueError(
        f"Secret '{key}' not found in environment. "
        "Set it in .env (local) or configure AWS_SECRET_ARN (production)."
    )


def _get_from_secrets_manager(secret_arn: str, key: str) -> str:
    """Fetch a specific key from an AWS Secrets Manager JSON secret."""
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secrets = json.loads(response["SecretString"])
    if key not in secrets:
        raise ValueError(f"Key '{key}' not found in Secrets Manager secret.")
    return secrets[key]
