"""Object-storage abstraction. LocalStorage is the development backend; an S3-compatible backend only
needs to implement the same five methods (`local_path` would download to a cache)."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO, Protocol

from .config import settings


class Storage(Protocol):
    def put_file(self, key: str, src: Path) -> None: ...
    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> tuple[int, str]: ...
    def local_path(self, key: str) -> Path: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> None: ...


class UploadTooLarge(Exception):
    pass


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("storage key escapes storage root")
        return p

    def put_file(self, key: str, src: Path) -> None:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> tuple[int, str]:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        size = 0
        try:
            with open(dst, "wb") as f:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLarge()
                    h.update(chunk)
                    f.write(chunk)
        except UploadTooLarge:
            dst.unlink(missing_ok=True)
            raise
        return size, h.hexdigest()

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        p = self._path(prefix)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


storage: Storage = LocalStorage(settings.data_dir / "objects")
