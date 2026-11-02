"""Private file storage behind an interface (R2). Local volume now; an S3-compatible
implementation can replace it without touching callers. Keys are derived from office id and
content hash only — no user-supplied path ever reaches the filesystem.

Two kinds of key:

- a version's original file, ``<office>/<sha256>``: content-addressed, written once;
- a staged reading (KTD2), ``<office>/staging/<version>/<attempt>/<name>``: the per-window outputs of a long PDF's
  ingestion, rewritten as the reading advances and deleted by prefix when it is published, fails for good, is
  superseded or its document is deleted. ``attempt`` is the reading-attempt key (a sha256), ``name`` one of a few
  fixed shapes (``staged_key``). Every part is validated like ``storage_key``: nothing but a uuid, a hex digest and
  a short plain name ever reaches the filesystem, and a delete can never reach an original file."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.config import get_settings

logger = logging.getLogger(__name__)

STAGING = "staging"
_STAGED_NAME = re.compile(r"[a-z]{1,8}[0-9]{0,6}(?:\.json(?:\.gz)?)?")


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def replace(self, key: str, data: bytes) -> None:
        """Write ``data`` under ``key``, replacing what is there, atomically (a reader sees the old or the new)."""
    def delete_prefix(self, prefix: str) -> int:
        """Delete every staged file whose key starts with ``prefix`` (a staging prefix, ``staging_prefix``); the
        number of files removed. Files that are not staged outputs are out of reach."""


def _hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def storage_key(office_id: UUID, sha256: str) -> str:
    if not _hex64(sha256):
        raise ValueError("invalid sha256")
    return f"{office_id}/{sha256}"


def staging_prefix(office_id: UUID, version_id: UUID | None = None, attempt: str | None = None) -> str:
    """The prefix of a staged reading: all of an office's, a version's, or one attempt's (a trailing slash)."""
    parts = [str(office_id), STAGING]
    if version_id is not None:
        parts.append(str(version_id))
        if attempt is not None:
            if not _hex64(attempt):
                raise ValueError("invalid attempt key")
            parts.append(attempt)
    return "/".join(parts) + "/"


def staged_key(office_id: UUID, version_id: UUID, attempt: str, name: str) -> str:
    if not _STAGED_NAME.fullmatch(name):
        raise ValueError("invalid staged name")
    return staging_prefix(office_id, version_id, attempt) + name


class LocalStorage:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _inside(self, path: Path) -> Path:
        path = path.resolve()
        if self.root not in path.parents:
            raise ValueError("invalid storage key")
        return path

    def _path(self, key: str) -> Path:
        parts = key.split("/")
        UUID(parts[0])  # raises on anything that is not a uuid
        if len(parts) == 2:
            if len(parts[1]) != 64 or not parts[1].isalnum():
                raise ValueError("invalid storage key")
            return self._inside(self.root / parts[0] / parts[1])
        if len(parts) == 5 and parts[1] == STAGING and _hex64(parts[3]) and _STAGED_NAME.fullmatch(parts[4]):
            UUID(parts[2])
            return self._inside(self.root.joinpath(*parts))
        raise ValueError("invalid storage key")

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return
        self._write(path, data)

    def replace(self, key: str, data: bytes) -> None:
        self._write(self._path(key), data)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete_prefix(self, prefix: str) -> int:
        """``prefix`` is ``<office>/staging/``, ``<office>/staging/<version>/``, ``.../<attempt>/`` or
        ``.../<attempt>/<start of a name>``; anything else is refused."""
        parts = prefix.split("/")
        n = len(parts)
        if not (3 <= n <= 5 and parts[1] == STAGING and (parts[-1] == "" or n == 5)):
            raise ValueError("invalid staging prefix")
        UUID(parts[0])
        if n >= 4:
            UUID(parts[2])
        if n == 5 and not _hex64(parts[3]):
            raise ValueError("invalid staging prefix")
        if n == 5 and not re.fullmatch(r"[a-z0-9.]*", parts[4]):
            raise ValueError("invalid staging prefix")
        if parts[-1] == "":  # a directory: everything below it
            base = self._inside(self.root.joinpath(*parts[:-1]))
            if not base.is_dir():
                return 0
            removed = sum(1 for p in base.rglob("*") if p.is_file())
            shutil.rmtree(base, ignore_errors=True)
            return removed
        base = self._inside(self.root.joinpath(*parts[:-1]))
        if not base.is_dir():
            return 0
        removed = 0
        for entry in list(base.iterdir()):
            if entry.is_file() and entry.name.startswith(parts[-1]):
                entry.unlink(missing_ok=True)
                removed += 1
        return removed


@lru_cache
def get_storage() -> Storage:
    return LocalStorage(get_settings().file_storage_root)


def purge_staging(office_id: UUID, version_id: UUID, attempt: str | None = None) -> None:
    """Delete everything staged for a version (a published, failed or deleted one), or only one reading attempt's files
    (``attempt``: a superseded reading's key). Best effort: staged files are scratch, and a failure to delete them
    must not fail the job that is done with them."""
    try:
        get_storage().delete_prefix(staging_prefix(office_id, version_id, attempt))
    except (OSError, ValueError):
        logger.warning("staged files of version %s were not deleted", version_id, exc_info=True)
