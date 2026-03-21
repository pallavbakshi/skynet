# Smoke test script used by compose, k8s, and CI.
# For interactive use prefer `skyops smoke`.
from __future__ import annotations

import os
import sys
import time
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

from agp.client import AgpClient, AgpProfile


def _minio_url() -> str:
    return os.environ.get("AGP_S3_ENDPOINT_URL", "http://127.0.0.1:9000")


def _s3_bucket() -> str:
    return os.environ.get("AGP_S3_BUCKET", "agp-artifacts")


def _assert_bucket_not_public() -> None:
    """Verify the S3 bucket denies unauthenticated requests."""
    bucket = _s3_bucket()
    url = f"{_minio_url()}/{bucket}/"
    try:
        urlopen(url, timeout=5)
        raise RuntimeError(f"bucket {bucket} is publicly accessible — policy not enforced")
    except HTTPError as e:
        if e.code in (403, 401):
            return  # expected: access denied
        raise RuntimeError(f"unexpected HTTP {e.code} checking bucket policy") from e
    except URLError as e:
        raise RuntimeError(
            f"MinIO unreachable at {url} — cannot verify bucket policy: {e}"
        ) from e


def _agent_id() -> str:
    return os.environ.get("AGP_SMOKE_AGENT_ID", "agt_local")


def _wait_for_health(client: AgpClient, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            client.health()
            return
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError("control plane did not become healthy in time")


def main() -> int:
    profile = AgpProfile.load()
    with AgpClient(profile=profile) as client:
        _wait_for_health(client)
        payload = client.send(
            "agent",
            _agent_id(),
            "local deployment smoke test",
            metadata={"kind": "smoke"},
            idempotency_key=f"smoke-{int(time.time())}",
        )
        if payload.get("kind") == "inline_result":
            artifact_id = payload["result_artifact_id"]
        else:
            snapshots = client.watch_job(
                payload["job_id"],
                poll_interval=1.0,
                max_polls=60,
            )
            job = snapshots[-1]["job"]
            if job["status"] != "completed":
                raise RuntimeError(f"smoke job finished in unexpected state: {job['status']}")
            artifact_id = job["result_artifact_id"]

        artifact = client.fetch_artifact(artifact_id, content=True)
        content = artifact.get("content", "")
        if "local deployment smoke test" not in content:
            raise RuntimeError("smoke artifact content did not include expected payload")

    _assert_bucket_not_public()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
