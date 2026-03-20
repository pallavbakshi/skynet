"""Artifact store boundary for AGP."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import uuid4
from typing import Protocol


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
        path = target / name
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
        relative = Path(namespace) / job_id / uuid4().hex[:12] / name
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
        relative = Path(namespace) / job_id / uuid4().hex[:12] / name
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


_INMEMORY_ARTIFACT_STORE = InMemoryArtifactStore()


def reset_artifact_store_state(name: str | None = None) -> None:
    if name is None or name == "inmemory":
        _INMEMORY_ARTIFACT_STORE.reset()


def get_artifact_store(name: str, root: str | Path) -> ArtifactStore:
    if name == "localfs":
        return LocalFsArtifactStore(root)
    if name == "sharedfs":
        return SharedFsArtifactStore(root)
    if name == "registryfs":
        return RegistryFsArtifactStore(root)
    if name == "inmemory":
        return _INMEMORY_ARTIFACT_STORE
    raise ValueError(f"unsupported artifact backend: {name}")
