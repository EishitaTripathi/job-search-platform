"""One-shot Lambda to load schema.sql into RDS. Deploy, invoke once, delete."""

import json
import os
import boto3
import psycopg2


def handler(event, context):
    sm = boto3.client("secretsmanager")
    secret = json.loads(
        sm.get_secret_value(SecretId=os.environ["SECRET_NAME"])["SecretString"]
    )

    conn = psycopg2.connect(
        host=secret["DB_HOST"],
        dbname=secret["DB_NAME"],
        user=secret["DB_USER"],
        password=secret["DB_PASSWORD"],
        port=5432,
        sslmode="require",
    )
    conn.autocommit = True

    schema = open("schema.sql").read()
    with conn.cursor() as cur:
        cur.execute(schema)

    conn.close()
    return {"status": "schema loaded"}
