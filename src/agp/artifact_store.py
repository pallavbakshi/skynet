"""Artifact store boundary for AGP."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote
from uuid import uuid4
from typing import Protocol


def _sanitize_name(name: str) -> str:
    """Validate an artifact name — must be a single path-safe component.

    Rejects names containing path separators or ``..`` to prevent traversal.
    """
    if not name or name in (".", ".."):
        raise ValueError(f"invalid artifact name: {name!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"artifact name must not contain path separators: {name!r}")
    if PurePosixPath(name).name in ("", ".", ".."):
        raise ValueError(f"invalid artifact name: {name!r}")
    return name


try:
    from botocore.exceptions import ClientError as _BotoClientError
except ImportError:  # pragma: no cover
    _BotoClientError = Exception  # type: ignore[assignment,misc]

_S3_NOT_FOUND_CODES = frozenset(("404", "NoSuchBucket", "NoSuchKey"))


def _load_boto3():
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise RuntimeError("s3 artifact backend requires the 'boto3' package") from exc
    return boto3


def _load_botocore_config():
    try:
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise RuntimeError("s3 artifact backend requires botocore") from exc
    return Config


def _checksum_text(content: str) -> tuple[str, int]:
    payload = content.encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), len(payload)


@dataclass(slots=True)
class StoredArtifact:
    role: str
    storage_ref: str
    content_type: str
    checksum: str
    size_bytes: int


class ArtifactStore(Protocol):
    """Abstract durable artifact storage."""

    name: str

    def write_text(
        self,
        *,
        namespace: str,
        job_id: str,
        name: str,
        content: str,
        role: str,
        content_type: str = "text/plain",
    ) -> StoredArtifact:
        """Persist textual artifact content and return storage metadata."""

    def read_text(self, *, storage_ref: str) -> str | None:
        """Read textual artifact content if supported by the backend."""

    def exists(self, *, storage_ref: str) -> bool:
        """Return whether the referenced artifact payload exists."""


class LocalFsArtifactStore:
    """Local filesystem artifact store used for the MVP."""

    name = "localfs"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write_text(
        self,
        *,
        namespace: str,
        job_id: str,
        name: str,
        content: str,
        role: str,
        content_type: str = "text/plain",
    ) -> StoredArtifact:
        target = self.root / namespace / job_id
        target.mkdir(parents=True, exist_ok=True)
        path = target / _sanitize_name(name)
        path.write_text(content, encoding="utf-8")
        checksum, size_bytes = _checksum_text(content)
        return StoredArtifact(
            role=role,
            storage_ref=path.resolve().as_uri(),
            content_type=content_type,
            checksum=checksum,
            size_bytes=size_bytes,
        )

    def read_text(self, *, storage_ref: str) -> str | None:
        if not storage_ref.startswith("file://"):
            return None
        path = Path(storage_ref.removeprefix("file://"))
        if not path.exists() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def exists(self, *, storage_ref: str) -> bool:
        if not storage_ref.startswith("file://"):
            return False
        path = Path(storage_ref.removeprefix("file://"))
        return path.exists() and path.is_file()


class SharedFsArtifactStore:
    """Shared-root filesystem backend with stable non-file storage refs."""

    name = "sharedfs"
    _scheme = "agpfs://"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path_for_ref(self, storage_ref: str) -> Path | None:
        if not storage_ref.startswith(self._scheme):
            return None
        relative = storage_ref.removeprefix(self._scheme)
        if not relative:
            return None
        return self.root / unquote(relative)

    def write_text(
        self,
        *,
        namespace: str,
        job_id: str,
        name: str,
        content: str,
        role: str,
        content_type: str = "text/plain",
    ) -> StoredArtifact:
        relative = Path(namespace) / job_id / uuid4().hex[:12] / _sanitize_name(name)
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        checksum, size_bytes = _checksum_text(content)
        return StoredArtifact(
            role=role,
            storage_ref=f"{self._scheme}{quote(relative.as_posix())}",
            content_type=content_type,
            checksum=checksum,
            size_bytes=size_bytes,
        )

    def read_text(self, *, storage_ref: str) -> str | None:
        path = self._path_for_ref(storage_ref)
        if path is None or not path.exists() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def exists(self, *, storage_ref: str) -> bool:
        path = self._path_for_ref(storage_ref)
        return path is not None and path.exists() and path.is_file()


class InMemoryArtifactStore:
    """In-process artifact store for backend portability tests."""

    name = "inmemory"

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def reset(self) -> None:
        self._items.clear()

    def write_text(
        self,
        *,
        namespace: str,
        job_id: str,
        name: str,
        content: str,
        role: str,
        content_type: str = "text/plain",
    ) -> StoredArtifact:
        storage_ref = f"mem://{namespace}/{job_id}/{uuid4().hex[:12]}/{name}"
        self._items[storage_ref] = content
        checksum, size_bytes = _checksum_text(content)
        return StoredArtifact(
            role=role,
            storage_ref=storage_ref,
            content_type=content_type,
            checksum=checksum,
            size_bytes=size_bytes,
        )

    def read_text(self, *, storage_ref: str) -> str | None:
        return self._items.get(storage_ref)

    def exists(self, *, storage_ref: str) -> bool:
        return storage_ref in self._items


class RegistryFsArtifactStore:
    """Filesystem-backed store with explicit registry metadata sidecars."""

    name = "registryfs"
    _scheme = "agpr://"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects_root = self.root / "objects"
        self.registry_root = self.root / "registry"

    def _ref_to_relative(self, storage_ref: str) -> Path | None:
        if not storage_ref.startswith(self._scheme):
            return None
        relative = storage_ref.removeprefix(self._scheme)
        if not relative:
            return None
        return Path(unquote(relative))

    def _object_path(self, relative: Path) -> Path:
        return self.objects_root / relative

    def _metadata_path(self, relative: Path) -> Path:
        return self.registry_root / relative.parent / f"{relative.name}.json"

    def write_text(
        self,
        *,
        namespace: str,
        job_id: str,
        name: str,
        content: str,
        role: str,
        content_type: str = "text/plain",
    ) -> StoredArtifact:
        relative = Path(namespace) / job_id / uuid4().hex[:12] / _sanitize_name(name)
        object_path = self._object_path(relative)
        metadata_path = self._metadata_path(relative)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_text(content, encoding="utf-8")
        checksum, size_bytes = _checksum_text(content)
        storage_ref = f"{self._scheme}{quote(relative.as_posix())}"
        metadata_path.write_text(
            json.dumps(
                {
                    "storage_ref": storage_ref,
                    "relative_path": relative.as_posix(),
                    "checksum": checksum,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                    "role": role,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return StoredArtifact(
            role=role,
            storage_ref=storage_ref,
            content_type=content_type,
            checksum=checksum,
            size_bytes=size_bytes,
        )

    def read_text(self, *, storage_ref: str) -> str | None:
        relative = self._ref_to_relative(storage_ref)
        if relative is None:
            return None
        metadata_path = self._metadata_path(relative)
        object_path = self._object_path(relative)
        if not metadata_path.exists() or not object_path.exists() or not object_path.is_file():
            return None
        return object_path.read_text(encoding="utf-8")

    def exists(self, *, storage_ref: str) -> bool:
        relative = self._ref_to_relative(storage_ref)
        if relative is None:
            return False
        metadata_path = self._metadata_path(relative)
        object_path = self._object_path(relative)
        return metadata_path.exists() and object_path.exists() and object_path.is_file()


class S3ArtifactStore:
    """S3-compatible artifact store suitable for MinIO/local object-store testing."""

    name = "s3"
    _scheme = "s3://"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        force_path_style: bool,
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region = region
        self.force_path_style = force_path_style
        self._client = self._make_client()

    def _make_client(self):
        boto3 = _load_boto3()
        Config = _load_botocore_config()
        session = boto3.session.Session()
        return session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region,
            use_ssl=bool(self.endpoint_url and self.endpoint_url.startswith("https://")),
            config=Config(s3={"addressing_style": "path" if self.force_path_style else "auto"}),
        )

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return
        except _BotoClientError as exc:
            if exc.response["Error"]["Code"] not in _S3_NOT_FOUND_CODES:
                raise
        kwargs: dict[str, object] = {"Bucket": self.bucket}
        if self.region and self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        self._client.create_bucket(**kwargs)

    def _key_for(self, *, namespace: str, job_id: str, name: str) -> str:
        return f"{namespace}/{job_id}/{uuid4().hex[:12]}/{_sanitize_name(name)}"

    def _parse_ref(self, storage_ref: str) -> tuple[str, str] | None:
        if not storage_ref.startswith(self._scheme):
            return None
        bucket_and_key = storage_ref.removeprefix(self._scheme)
        if "/" not in bucket_and_key:
            return None
        bucket, key = bucket_and_key.split("/", 1)
        if not bucket or not key:
            return None
        return bucket, key

    def write_text(
        self,
        *,
        namespace: str,
        job_id: str,
        name: str,
        content: str,
        role: str,
        content_type: str = "text/plain",
    ) -> StoredArtifact:
        self._ensure_bucket()
        key = self._key_for(namespace=namespace, job_id=job_id, name=name)
        checksum, size_bytes = _checksum_text(content)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
            Metadata={"role": role, "checksum": checksum},
        )
        return StoredArtifact(
            role=role,
            storage_ref=f"{self._scheme}{self.bucket}/{key}",
            content_type=content_type,
            checksum=checksum,
            size_bytes=size_bytes,
        )

    def read_text(self, *, storage_ref: str) -> str | None:
        parsed = self._parse_ref(storage_ref)
        if parsed is None:
            return None
        bucket, key = parsed
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except _BotoClientError as exc:
            if exc.response["Error"]["Code"] in _S3_NOT_FOUND_CODES:
                return None
            raise
        body = response["Body"].read()
        return body.decode("utf-8")

    def exists(self, *, storage_ref: str) -> bool:
        parsed = self._parse_ref(storage_ref)
        if parsed is None:
            return False
        bucket, key = parsed
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except _BotoClientError as exc:
            if exc.response["Error"]["Code"] in _S3_NOT_FOUND_CODES:
                return False
            raise


class HttpProxyArtifactStore:
    """Artifact store that uploads through the control plane API.

    Remote runtimes use this so they only need ``--server-url`` —
    no S3 credentials required.  The CP writes to its own storage backend.
    """

    name = "http"

    def __init__(self, server_url: str, timeout: float = 30.0) -> None:
        import httpx
        self._server_url = server_url.rstrip("/")
        self._client = httpx.Client(base_url=self._server_url, timeout=timeout)

    def write_text(
        self,
        *,
        namespace: str,
        job_id: str,
        name: str,
        content: str,
        role: str,
        content_type: str = "text/plain",
    ) -> StoredArtifact:
        checksum, size_bytes = _checksum_text(content)
        response = self._client.post(
            "/artifacts/upload",
            json={
                "namespace": namespace,
                "job_id": job_id,
                "name": name,
                "content": content,
                "role": role,
                "content_type": content_type,
            },
        )
        response.raise_for_status()
        data = response.json()["data"]
        return StoredArtifact(
            role=data["role"],
            storage_ref=data["storage_ref"],
            content_type=data["content_type"],
            checksum=data["checksum"],
            size_bytes=data["size_bytes"],
        )

    def read_text(self, *, storage_ref: str) -> str | None:
        # Reading is done through the CP's /artifacts/{id}/content endpoint,
        # not through the store directly.
        return None

    def exists(self, *, storage_ref: str) -> bool:
        return True  # Trust the CP — it wrote the ref


_INMEMORY_ARTIFACT_STORE = InMemoryArtifactStore()


def reset_artifact_store_state(name: str | None = None) -> None:
    if name is None or name == "inmemory":
        _INMEMORY_ARTIFACT_STORE.reset()


def get_artifact_store(name: str, root: str | Path, *, server_url: str | None = None) -> ArtifactStore:
    if name == "localfs":
        return LocalFsArtifactStore(root)
    if name == "sharedfs":
        return SharedFsArtifactStore(root)
    if name == "registryfs":
        return RegistryFsArtifactStore(root)
    if name == "http":
        if not server_url:
            raise ValueError("http artifact backend requires server_url")
        return HttpProxyArtifactStore(server_url)
    if name == "s3":
        from agp.config import settings

        return S3ArtifactStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            region=settings.s3_region,
            force_path_style=settings.s3_force_path_style,
        )
    if name == "inmemory":
        return _INMEMORY_ARTIFACT_STORE
    raise ValueError(f"unsupported artifact backend: {name}")
