"""Private file storage behind an interface (R2). Local volume now; an S3-compatible
implementation can replace it without touching callers. Keys are derived from office id and
content hash only — no user-supplied path ever reaches the filesystem."""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.config import get_settings


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


def storage_key(office_id: UUID, sha256: str) -> str:
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid sha256")
    return f"{office_id}/{sha256}"


class LocalStorage:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        office, _, digest = key.partition("/")
        UUID(office)  # raises on anything that is not a uuid
        if len(digest) != 64 or not digest.isalnum():
            raise ValueError("invalid storage key")
        path = (self.root / office / digest).resolve()
        if self.root not in path.parents:
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


@lru_cache
def get_storage() -> Storage:
    return LocalStorage(get_settings().file_storage_root)
