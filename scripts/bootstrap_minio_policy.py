"""Apply a private bucket policy to the AGP artifacts bucket in MinIO.

Disables anonymous/public access so only authenticated requests are accepted.
Idempotent: safe to run multiple times.
"""
from __future__ import annotations

import json
import os
import time

import boto3
from botocore.exceptions import ClientError


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AGP_S3_ENDPOINT_URL", "http://127.0.0.1:9000"),
        aws_access_key_id=os.environ.get("AGP_S3_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AGP_S3_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.environ.get("AGP_S3_REGION", "us-east-1"),
    )


def _wait_for_minio(client, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            client.list_buckets()
            return
        except Exception:
            time.sleep(1.0)
    raise RuntimeError("MinIO did not become reachable before timeout")


def apply_bucket_policy(endpoint_url: str | None = None, bucket: str | None = None) -> dict:
    client = _s3_client()
    _wait_for_minio(client)

    bucket = bucket or os.environ.get("AGP_S3_BUCKET", "agp-artifacts")
    region = os.environ.get("AGP_S3_REGION", "us-east-1")

    # Ensure bucket exists
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        if region == "us-east-1":
            client.create_bucket(Bucket=bucket)
        else:
            client.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": region},
            )

    # Deny all public (unauthenticated) access
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyPublicAccess",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": "s3:*",
                "Resource": [
                    f"arn:aws:s3:::{bucket}",
                    f"arn:aws:s3:::{bucket}/*",
                ],
                "Condition": {
                    "StringEquals": {"s3:authType": "REST-HEADER"},
                    "Bool": {"aws:SecureTransport": "false"},
                },
            }
        ],
    }

    # MinIO enforces anonymous-deny via a simpler approach: just block public reads.
    # Use the well-known MinIO "private" policy which denies all anonymous access.
    private_policy = {
        "Version": "2012-10-17",
        "Statement": [],  # Empty = deny all anonymous access (MinIO default for non-public buckets)
    }

    client.put_bucket_policy(Bucket=bucket, Policy=json.dumps(private_policy))
    return {"bucket": bucket, "policy": "private", "ok": True}


def main() -> int:
    result = apply_bucket_policy()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
